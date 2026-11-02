// THE AGENTS PAGE'S LOAD SPEED (load-speed investigation, the owner's pick
// 2026-11-01; part of #2688).
//
// Measured in the owner's browser: the list's `GET /v1/tasks?limit=200&
// view=summary` took 5.3 s on first load and 1.1-2.1 s on every 5 s poll, and
// only once it had answered did the list read the live and waiting states, one
// `?state=` read each. Each live row read its own `/attempts`. Opening one
// agent at its own address queued the agent's reads behind all of that.
//
// Now:
//   * a 5 s poll is ONE request: the live and waiting states as one `state=`
//     list, merged into the page held; the 200-row page is re-read every 30 s;
//   * when the page is read, the state read starts at the same instant;
//   * only the open row reads `/attempts`; every other row takes its total
//     from the row;
//   * an open agent's list starts only after the agent's panel has rendered
//     (never on a phone, where the list is hidden), shows loading meanwhile,
//     and polls at the idle 30 s while the agent is open.
//
// MUTATIONS, one per block: read the page on every poll (`pageDue` -> true);
// await the page before starting the state read; `useRuns(task, live || ...)`
// without `open`; drop the `listMayStart` wait from `AgentsScreen`'s load;
// drop `phoneWidth()` from `listMayStart`; use `pollInterval` for `pollMs`.

import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Task, TaskPage, TaskState } from '../types'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTasks: vi.fn(),
  loadTasksInStates: vi.fn(),
  loadAttempts: vi.fn(),
  loadLiveSession: vi.fn(),
  loadTask: vi.fn(),
  loadChildren: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadAgentRun: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

import { AgentsScreen, IDLE_POLL_MS, LIVE_POLL_MS, PAGE_REREAD_MS, agentPanelGone, agentPanelOpening, agentPanelShown } from '../Agents'
import { AgentSplit } from '../AgentSplit'

function tid(n: number): string {
  return `task_${n.toString(16).padStart(20, '0')}`
}

function row(n: number, state: TaskState, over: Partial<Task> = {}): Task {
  return runTask({
    id: tid(n),
    state,
    runner_profile: 'claude-code',
    created_at: new Date(Date.UTC(2026, 10, 1, 10, 0, n)).toISOString(),
    updated_at: new Date(Date.UTC(2026, 10, 1, 11, 0, n)).toISOString(),
    started_at: new Date(Date.UTC(2026, 10, 1, 10, 0, n)).toISOString(),
    completed_at: ['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(state) ? new Date(Date.UTC(2026, 10, 1, 11, 0, n)).toISOString() : null,
    attempt_count: 1,
    max_attempts: 3,
    ...over,
  })
}

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function page(tasks: Task[], token: string | null): Result<TaskPage> {
  return ok({ tasks, next_page_token: token, tenant_id: 'eng' })
}

/** Every `?state=` read answers from `all()`, whole: every state asked for, as one `in` query. */
function statesFrom(all: () => Task[]) {
  api.loadTasksInStates.mockImplementation(async (states: readonly TaskState[]) => {
    const asked = new Set<TaskState>(states)
    const tasks = all().filter((t) => asked.has(t.state))
    return tasks.length === 0 ? ({ status: 'empty', fetchedAt: Date.now() } satisfies Result<TaskPage>) : page(tasks, null)
  })
}

function media(phone: boolean): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: phone && query.includes('560'),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }))
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

const rowEl = (id: string) => document.querySelector<HTMLElement>(`.row.is-compact[data-task-id="${id}"]`)
const liveBadge = () => screen.queryByRole('tab', { name: /^Live/ })?.querySelector('.badge')?.textContent

const live = [row(1, 'RUNNING'), row(2, 'RUNNING'), row(3, 'LEASED')]
const waiting = [row(4, 'READY')]
const finished = Array.from({ length: 5 }, (_, i) => row(100 + i, 'SUCCEEDED'))

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
  media(false)
  for (let n = 0; n < 8; n++) agentPanelGone(tid(n))
  api.loadAttempts.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadLiveSession.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('the poll plan: one state read per 5 s poll, the page every 30 s', () => {
  it('reads the page and the state list together, then only the state list on each 5 s poll', async () => {
    api.loadTasks.mockImplementation(async () => page([...live, ...waiting, ...finished], 'next-1'))
    statesFrom(() => [...live, ...waiting])
    render(<AgentsScreen onOpen={() => {}} />)
    await advance(0)
    expect(liveBadge()).toBe('3')
    expect(api.loadTasks).toHaveBeenCalledTimes(1)
    expect(api.loadTasksInStates).toHaveBeenCalledTimes(1)

    // Each 5 s poll while Live holds a row: exactly one request, the state list.
    for (let poll = 1; poll <= 4; poll++) {
      await advance(LIVE_POLL_MS)
      expect(api.loadTasks, `poll ${poll} re-read the 200-row page`).toHaveBeenCalledTimes(1)
      expect(api.loadTasksInStates, `poll ${poll} made other than one state read`).toHaveBeenCalledTimes(1 + poll)
    }
    for (const [states, opts] of api.loadTasksInStates.mock.calls) {
      expect([...states].sort()).toEqual(['DISPATCHED', 'LEASED', 'PARKED', 'READY', 'RUNNING', 'STARTING'])
      expect(opts).toEqual({ view: 'summary' })
    }

    // The page falls due 30 s after it was read, and is read with a state read beside it.
    await advance(PAGE_REREAD_MS - 4 * LIVE_POLL_MS)
    await advance(LIVE_POLL_MS)
    expect(api.loadTasks).toHaveBeenCalledTimes(2)
    expect(liveBadge()).toBe('3')
  })

  it('takes the state read\'s rows, and re-reads the page on the next poll when a live row has left', async () => {
    let rows = [...live, ...waiting]
    api.loadTasks.mockImplementation(async () => page([...rows, ...finished], 'next-1'))
    statesFrom(() => rows.filter((t) => t.state !== 'SUCCEEDED'))
    render(<AgentsScreen onOpen={() => {}} />)
    await advance(0)
    expect(liveBadge()).toBe('3')

    // A leased agent starts running: the poll's state read brings its new row.
    rows = rows.map((t) => (t.id === tid(3) ? { ...t, state: 'RUNNING' as TaskState } : t))
    await advance(LIVE_POLL_MS)
    expect(api.loadTasks).toHaveBeenCalledTimes(1)
    expect(rowEl(tid(3))?.getAttribute('aria-label') ?? rowEl(tid(3))?.textContent).toMatch(/running/i)

    // One finishes: it leaves Live at once, and the next poll reads the page.
    rows = rows.map((t) => (t.id === tid(1) ? { ...t, state: 'SUCCEEDED' as TaskState, completed_at: new Date().toISOString() } : t))
    await advance(LIVE_POLL_MS)
    expect(liveBadge()).toBe('2')
    expect(api.loadTasks).toHaveBeenCalledTimes(1)
    await advance(LIVE_POLL_MS)
    expect(api.loadTasks, 'the page was not re-read after a row left the live states').toHaveBeenCalledTimes(2)
    expect(screen.queryByRole('tab', { name: /^Recent/ })?.querySelector('.badge')?.textContent).toBe('6+')
  })

  it('keeps the rows on screen when a poll\'s state read fails', async () => {
    api.loadTasks.mockImplementation(async () => page([...live, ...finished], 'next-1'))
    statesFrom(() => live)
    render(<AgentsScreen onOpen={() => {}} />)
    await advance(0)
    api.loadTasksInStates.mockResolvedValue({ status: 'error', error: { kind: 'unreachable', httpStatus: null, code: null, message: 'no' } })
    await advance(LIVE_POLL_MS)
    expect(api.loadTasksInStates).toHaveBeenCalledTimes(2)
    expect(rowEl(tid(1))).not.toBeNull()
    expect(liveBadge()).toBe('3')
  })
})

describe('the page read and the state read start together', () => {
  it('starts the state list read before the page read has answered', async () => {
    let answer: (r: Result<TaskPage>) => void = () => {}
    api.loadTasks.mockImplementation(() => new Promise((r) => (answer = r)))
    statesFrom(() => live)
    render(<AgentsScreen onOpen={() => {}} />)
    await advance(0)
    expect(api.loadTasks).toHaveBeenCalledTimes(1)
    // MUTATION: await the page, then read the states -- this is 0.
    expect(api.loadTasksInStates, 'the state read waited for the page').toHaveBeenCalledTimes(1)
    await act(async () => answer(page([...live, ...finished], 'next-1')))
    await advance(0)
    expect(liveBadge()).toBe('3')
  })
})

describe('attempt totals come from the rows; only the open row reads /attempts', () => {
  it('reads no /attempts for live rows that are not open, and one for the open row', async () => {
    // With no split beside it, an open agent's list reads at once (`listMayStart`).
    api.loadTasks.mockImplementation(async () => page([...live, ...finished], null))
    statesFrom(() => live)
    const { rerender } = render(<AgentsScreen onOpen={() => {}} />)
    await advance(0)
    await advance(0)
    expect(rowEl(tid(1))?.querySelector('.cr-try')?.textContent).toBe('attempt 1 of 3')
    expect(api.loadAttempts, 'a row that is not open read its attempts').not.toHaveBeenCalled()

    rerender(<AgentsScreen onOpen={() => {}} taskId={tid(2)} />)
    await advance(0)
    await advance(0)
    expect(api.loadAttempts.mock.calls.map((c) => c[0] as unknown)).toEqual([tid(2)])
  })
})

describe('an open agent is read first, and the list after its panel', () => {
  it('reads at once when no agent panel is coming beside it', async () => {
    api.loadTasks.mockImplementation(async () => page([...live, ...finished], 'next-1'))
    statesFrom(() => live)
    render(<AgentsScreen onOpen={() => {}} taskId={tid(1)} />)
    await advance(0)
    expect(api.loadTasks).toHaveBeenCalledTimes(1)
  })

  it('shows loading, not an empty list, until the panel has rendered, then reads the list', async () => {
    api.loadTasks.mockImplementation(async () => page([...live, ...finished], 'next-1'))
    statesFrom(() => live)
    // AgentSplit says so during its render; this list has no split beside it.
    agentPanelOpening(tid(1))
    const { container } = render(<AgentsScreen onOpen={() => {}} taskId={tid(1)} />)
    await advance(0)
    await advance(1_000)
    expect(api.loadTasks, 'the list read before the agent panel rendered').not.toHaveBeenCalled()
    expect(api.loadTasksInStates).not.toHaveBeenCalled()
    expect(container.querySelector('.ag-list-tabs')).toBeNull()
    expect(container.querySelector('[aria-busy="true"], .skel, .skeleton, .ctl-skel')).not.toBeNull()

    await act(async () => agentPanelShown(tid(1)))
    await advance(0)
    expect(api.loadTasks).toHaveBeenCalledTimes(1)
    expect(api.loadTasksInStates).toHaveBeenCalledTimes(1)
    expect(liveBadge()).toBe('3')
  })

  it('polls the list at the idle 30 s while an agent is open, and at 5 s once it closes', async () => {
    api.loadTasks.mockImplementation(async () => page([...live, ...finished], 'next-1'))
    statesFrom(() => live)
    agentPanelShown(tid(1))
    const { rerender } = render(<AgentsScreen onOpen={() => {}} taskId={tid(1)} />)
    await advance(0)
    const reads = () => api.loadTasks.mock.calls.length + api.loadTasksInStates.mock.calls.length
    expect(reads()).toBe(2)
    await advance(LIVE_POLL_MS)
    await advance(LIVE_POLL_MS)
    expect(reads(), 'the list polled at 5 s under an open agent').toBe(2)
    await advance(IDLE_POLL_MS - 2 * LIVE_POLL_MS)
    expect(api.loadTasks).toHaveBeenCalledTimes(2)
    expect(api.loadTasksInStates).toHaveBeenCalledTimes(2)

    rerender(<AgentsScreen onOpen={() => {}} taskId={null} />)
    await advance(IDLE_POLL_MS)
    const at = reads()
    await advance(LIVE_POLL_MS)
    expect(reads()).toBe(at + 1)
  })

  it('does not start the list on a phone while the agent covers it, and starts it when it closes', async () => {
    media(true)
    api.loadTasks.mockImplementation(async () => page([...live, ...finished], 'next-1'))
    statesFrom(() => live)
    agentPanelShown(tid(1))
    const { rerender } = render(<AgentsScreen onOpen={() => {}} taskId={tid(1)} />)
    await advance(0)
    await advance(IDLE_POLL_MS)
    expect(api.loadTasks, 'the phone read a list it does not show').not.toHaveBeenCalled()
    rerender(<AgentsScreen onOpen={() => {}} taskId={null} />)
    await advance(0)
    expect(api.loadTasks).toHaveBeenCalledTimes(1)
  })

  it('beside the split, the agent\'s task and attempts are read before the list', async () => {
    const open = row(1, 'RUNNING')
    let land: (r: Result<Task>) => void = () => {}
    api.loadTask.mockImplementation(() => new Promise((r) => (land = r)))
    api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
    api.loadChildren.mockResolvedValue(ok({ tasks: [] } satisfies TaskPage))
    api.loadAgentRun.mockReturnValue(new Promise(() => {}))
    api.loadTranscript.mockReturnValue(new Promise(() => {}))
    api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
    api.loadTasks.mockImplementation(async () => page([...live, ...finished], 'next-1'))
    statesFrom(() => live)
    render(
      <div className="app has-inspector">
        <AgentsScreen onOpen={() => {}} taskId={open.id} />
        <AgentSplit taskId={open.id} pane="detail" artifact={null} closeTo="work/running/live" go={() => {}} base={`work/task/${open.id}`} />
      </div>,
    )
    await advance(0)
    expect(api.loadTask).toHaveBeenCalledWith(open.id)
    expect(api.loadTasks, 'the list read before the agent').not.toHaveBeenCalled()
    await act(async () => land(ok(open)))
    await advance(0)
    await advance(0)
    expect(api.loadAttempts).toHaveBeenCalledWith(open.id)
    expect(api.loadTasks).toHaveBeenCalledTimes(1)
  })
})
