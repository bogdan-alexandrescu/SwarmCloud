// LIVE-BROWSER QA PASS p12 (2026-10-13, task_4defa61c5eea4a5c87f5), THE AGENTS
// LIST (lane AGENTS-LIST; part of #1030).
//
// QA12-12: on a resumed attempt the list row said `try 1/3` while the task
// header said `attempt 2 of 3`. The header counts the attempt documents
// (`timesRun`, live QA WA-3); the row printed the task's charged counter. The
// row now reads the same documents and says it in the header's words.
//
// QA12-20: the Live tab's copy. The `All | Live N` switch said `Live` inside
// the Live tab; `Live 5+` showed with exactly 5 live; `200 loaded · more
// beyond` and `Load 200 more` showed under 5 live rows; a browser agent that
// was STARTING was left out of the browser filter. The live states are now
// read in full (they are bounded by concurrency, not by the 200-row page), so
// Live's count is exact and offers no "more" when there is none; the switch
// says `Browsers`; a starting browser agent is listed with a `starting` mark.
//
// QA12-29: the row's device outline was read once and went stale after a
// device switch. It is re-read with the list's poll.
//
// MUTATIONS, one per block: print `task.attempt_count` in the row again
// (QA12-12 red); label the switch's second option `Live` (QA12-20 switch
// red); drop the live-state reads from `loadAgentsPage` (QA12-20 count red);
// filter the browser rows to RUNNING only (QA12-20 starting red); drop the
// poll's read time from `DeviceThumb`'s effect (QA12-29 red).

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { LiveRead, LiveSession } from '../api'
import type { Result } from '../fetch'
import type { AttemptRow, Task, TaskPage, TaskState } from '../types'
import { attempt, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTasks: vi.fn(),
  loadTasksInStates: vi.fn(),
  loadAttempts: vi.fn(),
  loadLiveSession: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

import { AgentsScreen, LIVE_POLL_MS } from '../Agents'

const BROWSER = 'claude-code-browser'

/** A task id of the real shape, built at runtime. */
function tid(n: number): string {
  return `task_${n.toString(16).padStart(20, '0')}`
}

function row(n: number, state: TaskState, over: Partial<Task> = {}): Task {
  return runTask({
    id: tid(n),
    state,
    runner_profile: 'claude-code',
    created_at: new Date(Date.UTC(2026, 9, 13, 10, 0, n)).toISOString(),
    updated_at: new Date(Date.UTC(2026, 9, 13, 11, 0, n)).toISOString(),
    started_at: new Date(Date.UTC(2026, 9, 13, 10, 0, n)).toISOString(),
    completed_at: ['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(state) ? new Date(Date.UTC(2026, 9, 13, 11, 0, n)).toISOString() : null,
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

/**
 * Every `?state=` read answers from `all`, whole, as the server would: every
 * state asked for, as one `in` query (#2998).
 */
function statesFrom(all: Task[]) {
  api.loadTasksInStates.mockImplementation(async (states: readonly TaskState[]) => {
    const asked = new Set<TaskState>(states)
    const tasks = all.filter((t) => asked.has(t.state))
    return tasks.length === 0 ? ({ status: 'empty', fetchedAt: Date.now() } satisfies Result<TaskPage>) : page(tasks, null)
  })
}

function session(device: LiveSession['device']): Result<LiveRead> {
  return ok({
    live_browser: true,
    session: {
      device,
      presets: [
        { name: 'iphone-15', label: 'iPhone 15', width: 393, height: 852, mobile: true },
        { name: 'ipad-7', label: 'iPad', width: 810, height: 1080, mobile: true },
      ],
    } as unknown as LiveSession,
  })
}

const rowEl = (id: string) => document.querySelector<HTMLElement>(`.row.is-compact[data-task-id="${id}"]`)

async function mount(open: string | null = null): Promise<void> {
  render(<AgentsScreen onOpen={() => {}} taskId={open} />)
  await waitFor(() => expect(document.querySelector('.ag-list-tabs [role="tab"]')).not.toBeNull())
}

afterEach(() => {
  vi.useRealTimers()
})

describe('QA12-12: the row says the attempt the header says', () => {
  it('a resumed attempt reads `attempt 2 of 3`, from the attempt documents, not the charged counter', async () => {
    const resumed = row(1, 'RUNNING', { attempt_count: 1, current_generation: 2 })
    api.loadTasks.mockResolvedValue(page([resumed], null))
    statesFrom([resumed])
    api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1), attempt(2)] as AttemptRow[] }))
    // The OPEN row reads the documents; every other row its own counter.
    await mount(resumed.id)
    await waitFor(() => expect(rowEl(resumed.id)?.querySelector('.cr-try')?.textContent).toBe('attempt 2 of 3'))
    expect(api.loadAttempts).toHaveBeenCalledWith(resumed.id)
    expect(rowEl(resumed.id)!.textContent).not.toMatch(/try \d/)
    // The difference from the charged counter is said, not hidden.
    expect(rowEl(resumed.id)!.querySelector('.cr-try')!.getAttribute('aria-label')).toMatch(/attempt 2 of 3.*1 counted against the cap/)
  })

  it('falls back to the task counter, in the same words, when the documents are not read', async () => {
    const first = row(2, 'RUNNING')
    api.loadTasks.mockResolvedValue(page([first], null))
    statesFrom([first])
    api.loadAttempts.mockResolvedValue({ status: 'error', error: { kind: 'unreachable', httpStatus: null, code: null, message: 'no' } })
    await mount(first.id)
    await waitFor(() => expect(api.loadAttempts).toHaveBeenCalled())
    await waitFor(() => expect(rowEl(first.id)?.querySelector('.cr-try')?.textContent).toBe('attempt 1 of 3'))
  })
})

describe('QA12-20: the Live tab says exactly what it holds', () => {
  // A full first page of finished rows with more beyond it, five live agents
  // on it, and one more live agent OLDER than the page (a resumed task).
  const finished = Array.from({ length: 6 }, (_, i) => row(100 + i, 'SUCCEEDED'))
  const live = [row(10, 'RUNNING'), row(11, 'RUNNING'), row(12, 'LEASED'), row(13, 'DISPATCHED')]
  const old = row(3, 'RUNNING', { created_at: new Date(Date.UTC(2026, 9, 1)).toISOString() })

  it('counts Live exactly, lists a live agent older than the page, and offers no "more" on Live', async () => {
    api.loadTasks.mockResolvedValue(page([...live, ...finished], 'next-1'))
    statesFrom([...live, old, ...finished])
    api.loadAttempts.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    await mount()
    const liveTab = screen.getByRole('tab', { name: /^Live/ })
    await waitFor(() => expect(liveTab.querySelector('.badge')?.textContent).toBe('5'))
    fireEvent.click(liveTab)
    await waitFor(() => expect(rowEl(old.id)).not.toBeNull())
    expect(document.querySelectorAll('.rows .row.is-compact')).toHaveLength(5)
    expect(document.body.textContent).not.toMatch(/more beyond/)
    expect(screen.queryByRole('button', { name: /^Load \d+ more$/ })).toBeNull()
    // Every live state was read, and the waiting states beside them (VQ-012),
    // nothing else -- as ONE `?state=` list read (load-speed, #2688).
    expect(api.loadTasksInStates).toHaveBeenCalledTimes(1)
    expect([...api.loadTasksInStates.mock.calls[0]![0]].sort()).toEqual(['DISPATCHED', 'LEASED', 'PARKED', 'READY', 'RUNNING', 'STARTING'])

    // CONTROL: Recent is still a capped page, and still says so.
    fireEvent.click(screen.getByRole('tab', { name: /^Recent/ }))
    expect(screen.getByRole('tab', { name: /^Recent/ }).querySelector('.badge')?.textContent).toBe('6+')
    expect(screen.getByRole('button', { name: /^Load \d+ more$/ })).toBeTruthy()
  })

  it('keeps the lower bound when a live-state read fails', async () => {
    api.loadTasks.mockResolvedValue(page([...live, ...finished], 'next-1'))
    api.loadTasksInStates.mockResolvedValue({ status: 'error', error: { kind: 'unreachable', httpStatus: null, code: null, message: 'no' } })
    api.loadAttempts.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    await mount()
    await waitFor(() => expect(api.loadTasksInStates).toHaveBeenCalled())
    expect(screen.getByRole('tab', { name: /^Live/ }).querySelector('.badge')?.textContent).toBe('4+')
  })
})

describe('QA12-20: the browser switch inside Live', () => {
  const rows = (): Task[] => [
    row(20, 'RUNNING', { runner_profile: BROWSER }),
    row(21, 'RUNNING'),
    row(22, 'STARTING', { runner_profile: BROWSER }),
  ]

  async function mountBrowsers() {
    api.loadTasks.mockResolvedValue(page(rows(), null))
    statesFrom(rows())
    api.loadAttempts.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    api.loadLiveSession.mockResolvedValue(session(null))
    await mount()
    return screen.getByRole('group', { name: /browser agents/i })
  }

  it('says Browsers, not Live, and counts the starting browser agent with the running one', async () => {
    const sw = await mountBrowsers()
    expect(within(sw).queryByRole('button', { name: /^Live/ })).toBeNull()
    const browsers = within(sw).getByRole('button', { name: /^Browsers/ })
    expect(browsers.textContent).toMatch(/Browsers\s*2$/)
  })

  it('lists a STARTING browser agent, after the running one, with a starting mark', async () => {
    const sw = await mountBrowsers()
    fireEvent.click(within(sw).getByRole('button', { name: /^Browsers/ }))
    await waitFor(() =>
      expect([...document.querySelectorAll('.rows .row.is-compact')].map((r) => r.getAttribute('data-task-id'))).toEqual([tid(20), tid(22)]),
    )
    expect(rowEl(tid(22))!.querySelector('.cr-starting')?.textContent).toMatch(/starting/)
    expect(rowEl(tid(20))!.querySelector('.cr-starting')).toBeNull()
  })
})

describe('QA12-29: the device outline follows a switch', () => {
  it('is re-read with the list, so a phone switched to a tablet is drawn as a tablet', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
    const advance = async (ms: number) => {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ms)
      })
    }
    const b = row(30, 'RUNNING', { runner_profile: BROWSER })
    api.loadTasks.mockImplementation(async () => page([b], null))
    statesFrom([b])
    api.loadAttempts.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    let device: LiveSession['device'] = { preset: 'iphone-15', orientation: 'portrait', touch: true }
    api.loadLiveSession.mockImplementation(async () => session(device))
    render(<AgentsScreen onOpen={() => {}} />)
    await advance(0)
    await advance(0)
    const outline = () => rowEl(b.id)?.querySelector('.ag-device')
    expect(outline()?.getAttribute('data-device')).toBe('phone')

    device = { preset: 'ipad-7', orientation: 'landscape', touch: true }
    await advance(LIVE_POLL_MS)
    await advance(0)
    expect(outline()?.getAttribute('data-device'), 'the outline was not re-read on the poll').toBe('tablet')
    expect(outline()?.getAttribute('data-orientation')).toBe('landscape')
  })
})
