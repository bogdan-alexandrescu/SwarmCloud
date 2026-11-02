// THE FIRST CONSOLE VISUAL-QA RUN, THE AGENTS LIST (lane QAFIX-AGENTS; part of
// #1680). Run wf_9e9bf74834634db7bcc3, 2026-10-20, the offline preview build
// at 1440 and 390, light and dark. One block per finding a unit test can
// hold; each assertion names the mutation that turns it red.
//
//   QA-026  the open task's row said `attempt 1 of 3` while its header said
//           `attempt 3 of 3`: until the row's attempt read landed it printed
//           the charged counter, a figure the header never shows.
//   QA-029  the Live / Waiting / Recent tabs were named by a sentence that
//           left out the visible count (axe label-content-name-mismatch).
//   QA-030  the sort button showed `Started` and was named `Sort by start
//           time` (the same axe rule).
//   QA-032  each row was a role=button div holding the copy-id button (axe
//           nested-interactive).
//   QA-079  Live's right-hand column mixed durations with the words `leased`
//           and `dispatched`.
//   QA-080  at 390 the profile select was 44px tall, the search field and the
//           Started button about 31px.
//   QA-110  Recent's right-hand column was the run's length, so which failure
//           was the newest could not be read off the list.
//   QA-111  `Started` showed no direction; `Failed first` was offered on a
//           list already filtered to `failed`.

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { AttemptRow, Task, TaskPage, TaskState } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
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

import { AgentsScreen } from '../Agents'

const PHONE: CascadeEnv = { width: 390 }
const NOW = Date.UTC(2026, 9, 20, 12, 0, 0)
const iso = (minutesAgo: number) => new Date(NOW - minutesAgo * 60_000).toISOString()

/** A task id of the real shape, built at runtime. */
function tid(n: number): string {
  return `task_${n.toString(16).padStart(20, '0')}`
}

function row(n: number, state: TaskState, over: Partial<Task> = {}): Task {
  const done = ['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(state)
  return runTask({
    id: tid(n),
    state,
    runner_profile: 'claude-code',
    created_at: iso(60 - n),
    updated_at: iso(30 - n),
    started_at: ['LEASED', 'DISPATCHED', 'QUEUED', 'READY'].includes(state) ? null : iso(40 - n),
    completed_at: done ? iso(30 - n) : null,
    attempt_count: 1,
    max_attempts: 3,
    ...over,
  })
}

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function serve(tasks: Task[]): void {
  api.loadTasks.mockResolvedValue(ok({ tasks, next_page_token: null, tenant_id: 'eng' } satisfies TaskPage))
  api.loadTasksInStates.mockImplementation(async (states: readonly TaskState[]) => {
    const of = tasks.filter((t) => states.includes(t.state))
    return of.length === 0
      ? ({ status: 'empty', fetchedAt: Date.now() } satisfies Result<TaskPage>)
      : ok({ tasks: of, next_page_token: null, tenant_id: 'eng' } satisfies TaskPage)
  })
}

const rowEl = (id: string) => document.querySelector<HTMLElement>(`.row.is-compact[data-task-id="${id}"]`)!
/** The row's opener by its name: it starts with the agent and its id; the copy's starts with `Copy`. */
const opener = (id: string) => (name: string) => !name.startsWith('Copy') && name.includes(` · ${id}`)
const flat = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

async function mount(taskId: string | null = null): Promise<void> {
  render(<AgentsScreen onOpen={() => {}} taskId={taskId} />)
  await waitFor(() => expect(document.querySelector('.row.is-compact')).not.toBeNull())
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
  api.loadAttempts.mockReset()
  api.loadLiveSession.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
})

afterEach(() => {
  vi.useRealTimers()
  window.history.replaceState(null, '', '/')
})

describe('QA-026: the row never prints an attempt figure the header does not', () => {
  it('says nothing countable until the attempt documents are read, then their count', async () => {
    const t = row(1, 'RUNNING')
    serve([t])
    let land: (r: Result<{ attempts: AttemptRow[] }>) => void = () => {}
    api.loadAttempts.mockImplementation(() => new Promise((r) => (land = r)))
    await mount(t.id)
    await waitFor(() => expect(api.loadAttempts).toHaveBeenCalledWith(t.id))
    // MUTATION: let `useRuns` fall back to `task.attempt_count` while the read
    // is in flight -- the row says `attempt 1 of 3` beside a header that,
    // over the same three documents, says `attempt 3 of 3`.
    expect(rowEl(t.id).querySelector('.cr-try')!.textContent).toBe('attempt … of 3')
    await act(async () => land(ok({ attempts: [attempt(1), attempt(2), attempt(3)] })))
    // MUTATION: print `task.attempt_count` in the row (QA12-12's defect).
    await waitFor(() => expect(rowEl(t.id).querySelector('.cr-try')!.textContent).toBe('attempt 3 of 3'))
  })
})

describe('QA-029: each list tab is named by the words it shows', () => {
  it('starts every tab name with its visible label and count', async () => {
    serve([row(1, 'RUNNING'), row(2, 'LEASED'), row(3, 'READY'), row(4, 'FAILED')])
    await mount()
    const tabs = within(screen.getByRole('tablist', { name: 'Agents' })).getAllByRole('tab')
    expect(tabs.map(flat)).toEqual(['Live 2', 'Waiting 1', 'Recent 1'])
    // MUTATION: name a tab by its sentence alone (`Live: holding a pool
    // slot, and costing money`), which drops the visible `2`.
    for (const tab of tabs) expect(tab.getAttribute('aria-label')!.startsWith(flat(tab))).toBe(true)
    // MUTATION: drop the sentence; the tab still says what Live costs.
    expect(screen.getByRole('tab', { name: 'Live 2: holding a pool slot, and costing money' })).toBe(tabs[0])
  })
})

describe('QA-030 and QA-111: the sort is named by its words and shows its direction', () => {
  it('names the button from the word it shows and draws a direction in every state', async () => {
    serve([row(1, 'FAILED'), row(2, 'SUCCEEDED')])
    await mount()
    const sort = document.querySelector<HTMLElement>('.ag-sort')!
    const seen: string[] = []
    for (let i = 0; i < 3; i++) {
      // MUTATION: name it `Sort by start time` again -- the visible word is
      // no longer the start of its name.
      expect(sort.getAttribute('aria-label')!.startsWith(flat(sort))).toBe(true)
      // MUTATION: draw a bare word -- the control shows no direction at all.
      // The direction is words since run 6 (6:flow-find-failure-desktop-03).
      expect(flat(sort)).toMatch(/^(Ended|Started), (newest|oldest) first$/)
      seen.push(flat(sort))
      fireEvent.click(sort)
    }
    // MUTATION: one label for two orders.
    expect(new Set(seen).size).toBe(3)
    // The list's own order on Recent is said, as the newest end first (QA-111, run 2: it read `Started`).
    expect(screen.getByRole('button', { name: /^Ended, newest first/ })).toBe(sort)
  })

  it('does not offer `Failed first` on a list filtered to one state', async () => {
    serve([row(1, 'FAILED'), row(2, 'SUCCEEDED')])
    await mount()
    fireEvent.click(screen.getByRole('tab', { name: /^Recent/ }))
    expect(screen.getByRole('checkbox', { name: 'Failed first' })).toBeTruthy()
    fireEvent.click(within(screen.getByRole('group', { name: 'Recent, by state' })).getByRole('button', { name: /^failed/ }))
    // MUTATION: offer the checkbox whatever the state filter.
    expect(screen.queryByRole('checkbox', { name: 'Failed first' })).toBeNull()
  })
})

describe('QA-032: a row is not a button around another button', () => {
  it('opens from a button that holds no focusable child, beside the copy', async () => {
    const a = row(1, 'RUNNING')
    const b = row(2, 'RUNNING')
    serve([a, b])
    api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1)] }))
    const opened: string[] = []
    render(<AgentsScreen onOpen={(id) => opened.push(id)} taskId={null} />)
    await waitFor(() => expect(document.querySelectorAll('.row.is-compact').length).toBe(2))
    const first = rowEl(a.id)
    // MUTATION: `role="button"` back on the row div.
    expect(first.getAttribute('role')).toBeNull()
    for (const el of document.querySelectorAll('.rows [role="button"], .rows button')) {
      // MUTATION: put the copy button back inside the element that opens the row.
      expect(el.querySelector('button, [tabindex], a[href], input, select')).toBeNull()
    }
    const open = within(first).getByRole('button', { name: opener(a.id) })
    const copy = within(first).getByRole('button', { name: `Copy task id ${a.id}` })
    expect(open.contains(copy)).toBe(false)
    // MUTATION: drop the keyboard from the open button -- a click (which is
    // what Enter and Space give a button) must open, ArrowDown must reach the
    // next row's.
    fireEvent.click(open)
    expect(opened).toEqual([a.id])
    // Live lists the newer submission first, so `b` is the top row.
    const top = within(rowEl(b.id)).getByRole('button', { name: opener(b.id) })
    top.focus()
    fireEvent.keyDown(top, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(open)
  })
})

describe('QA-079: Live’s right-hand column holds times only', () => {
  it('moves `leased` and `dispatched` to line two and keeps the column for figures', async () => {
    const leased = row(1, 'LEASED')
    const dispatched = row(2, 'DISPATCHED')
    const running = row(3, 'RUNNING')
    serve([leased, dispatched, running])
    api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1)] }))
    await mount()
    for (const t of [leased, dispatched]) {
      // MUTATION: print `elapsed().text` (the state word) in `.when` again.
      // The column holds the age since submission now (QA1-FIX-07,
      // overview-agents-19): a figure, never the word.
      expect(flat(rowEl(t.id).querySelector('.when'))).toMatch(/^\d+[dhms]/)
      // MUTATION: drop the word from line two -- the state is then said nowhere visible.
      expect(flat(rowEl(t.id).querySelector('.cr-sub'))).toContain(t.state.toLowerCase())
    }
    expect(flat(rowEl(running.id).querySelector('.when'))).toMatch(/^\d+m \d+s$/)
  })
})

describe('QA-110: a finished row says when it ended', () => {
  it('puts the end in the right-hand column and the run length on line two', async () => {
    const older = row(1, 'FAILED', { completed_at: iso(50), started_at: iso(55), updated_at: iso(50) })
    const newer = row(2, 'FAILED', { completed_at: iso(5), started_at: iso(10), updated_at: iso(5) })
    serve([older, newer])
    await mount()
    fireEvent.click(screen.getByRole('tab', { name: /^Recent/ }))
    // MUTATION: the run length back in `.when` -- both rows read `5m 0s`.
    expect(flat(rowEl(newer.id).querySelector('.when'))).toBe('5m ago')
    expect(flat(rowEl(older.id).querySelector('.when'))).toBe('50m ago')
    // MUTATION: drop the run length from line two.
    expect(flat(rowEl(newer.id).querySelector('.cr-sub'))).toContain('ran 5m 0s')
    // MUTATION: drop the end's title -- the instant is not readable anywhere.
    expect(rowEl(newer.id).querySelector('.when')!.getAttribute('title')).toMatch(/^ended /)
  })
})

describe('QA-080: at 390 the filter row’s controls are one height', () => {
  it('gives the search, the profile select and the Started button one minimum height', async () => {
    serve([row(1, 'RUNNING')])
    api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1)] }))
    await mount()
    const select = document.querySelector('.ag-list-head .ag-filter select')!
    const search = document.querySelector('.ag-find > input')!
    const sort = document.querySelector('.ag-sort')!
    expect(painted(select, 'min-height', PHONE)).toBe('44px')
    // MUTATION: drop the phone rule for `.ag-find > input` / `.ag-sort` --
    // they fall back to the 32px base while the select stays 44px.
    expect(painted(search, 'min-height', PHONE)).toBe('44px')
    expect(painted(sort, 'min-height', PHONE)).toBe('44px')
  })
})
