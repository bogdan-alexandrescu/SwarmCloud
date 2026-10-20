// LIVE QA, WORK › AGENTS, THE LIST (2026-10-11, swarm.example.com).
//
// WA-1 (S1): a whole task id pasted into "Find by name or task id" was searched
// for over the loaded page only, and a task past the newest 200 read `real
// zero · nothing in recent matching "task_…"` -- a measured absence of a task
// that existed. A whole id is now looked up (`GET /v1/tasks/{id}`), and a
// search over a capped page that finds nothing says `partial`, not `real zero`.
//
// WA-2 (S2): `200 loaded · more beyond`, and no way to read the more. The list
// now follows `next_page_token` on "Load more", and the counts go with it.
//
// WA-4 (S2): Live rows reordered on every 5s re-read, because the list's own
// order was `updated_at`, which a running task moves with each write.
//
// WA-9 (S3): the Recent state chips counted every profile's rows with a
// profile chosen.
//
// WA-15 (S3): a deep-linked agent's row was marked but left below the fold.
//
// Each test asserts the property (what is found, what is counted, what order
// holds), not the markup. MUTATIONS: drop the `IdLookup` branch (WA-1 red);
// put the `zero` mark back on a capped search (WA-1 red); drop `onMore` or
// `withMorePages` (WA-2 red); put `updated_at` back as Live's order (WA-4
// red); drop the profile from `stateCounts` (WA-9 red); drop the scroll
// effect (WA-15 red).

import { afterEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Task, TaskPage, TaskState } from '../types'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({ loadTasks: vi.fn(), loadTask: vi.fn() }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

import { AgentsScreen, WHOLE_TASK_ID, listOrder } from '../Agents'

/** A task id of the real shape, built at runtime: `task_` and twenty hex digits. */
function tid(n: number): string {
  return `task_${n.toString(16).padStart(20, '0')}`
}

function row(n: number, state: TaskState, over: Partial<Task> = {}): Task {
  return runTask({
    id: tid(n),
    state,
    runner_profile: 'claude-code',
    created_at: new Date(Date.UTC(2026, 9, 11, 10, 0, n)).toISOString(),
    updated_at: new Date(Date.UTC(2026, 9, 11, 11, 0, n)).toISOString(),
    started_at: new Date(Date.UTC(2026, 9, 11, 10, 0, n)).toISOString(),
    completed_at: ['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(state) ? new Date(Date.UTC(2026, 9, 11, 11, 0, n)).toISOString() : null,
    ...over,
  })
}

function page(tasks: Task[], token: string | null): Result<TaskPage> {
  return { status: 'ok', data: { tasks, next_page_token: token, tenant_id: 'eng' }, fetchedAt: Date.now() }
}

/** The ids of the list's rows, in the order drawn. */
function drawn(): string[] {
  return [...document.querySelectorAll<HTMLElement>('.rows .row.is-compact[data-task-id]')].map((r) => r.getAttribute('data-task-id')!)
}

async function list(first: Result<TaskPage>, taskId: string | null = null): Promise<void> {
  api.loadTasks.mockResolvedValueOnce(first)
  render(<AgentsScreen onOpen={() => {}} taskId={taskId} />)
  await waitFor(() => expect(document.querySelector('.ag-list-tabs [role="tab"]')).not.toBeNull())
}

function search(text: string) {
  fireEvent.change(screen.getByRole('searchbox', { name: 'Find by name or task id' }), { target: { value: text } })
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('WA-1: a whole task id is found beyond the loaded page', () => {
  const loaded = [row(1, 'SUCCEEDED'), row(2, 'SUCCEEDED'), row(3, 'FAILED')]
  const beyond = row(999, 'SUCCEEDED', { step_id: 'older-step' })

  it('recognises a whole id, and only a whole id', () => {
    expect(WHOLE_TASK_ID.test(tid(7))).toBe(true)
    expect(WHOLE_TASK_ID.test('task_7898edecb15543d98553')).toBe(true)
    expect(WHOLE_TASK_ID.test('task_7898edec')).toBe(false)
    expect(WHOLE_TASK_ID.test('older-step')).toBe(false)
  })

  it('looks a whole id up on the server when the page does not hold it, and draws the task', async () => {
    api.loadTask.mockResolvedValue({ status: 'ok', data: beyond, fetchedAt: Date.now() } satisfies Result<Task>)
    await list(page(loaded, 'next-1'))
    search(beyond.id)
    await waitFor(() => expect(drawn()).toEqual([beyond.id]))
    expect(api.loadTask).toHaveBeenCalledWith(beyond.id)
    // The one claim the defect made, gone: no measured absence of a task that exists.
    expect(document.querySelector('.ctl-mark.is-zero')).toBeNull()
  })

  it('opens a found task under the tab its state puts it in', async () => {
    const running = row(998, 'RUNNING')
    api.loadTask.mockResolvedValue({ status: 'ok', data: running, fetchedAt: Date.now() } satisfies Result<Task>)
    const onOpen = vi.fn()
    const onList = vi.fn()
    api.loadTasks.mockResolvedValueOnce(page(loaded, 'next-1'))
    render(<AgentsScreen onOpen={onOpen} onList={onList} />)
    await waitFor(() => expect(drawn().length).toBeGreaterThan(0))
    search(running.id)
    await waitFor(() => expect(drawn()).toEqual([running.id]))
    fireEvent.click(document.querySelector<HTMLElement>(`.row[data-task-id="${running.id}"]`)!)
    expect(onOpen).toHaveBeenCalledWith(running.id)
    expect(onList.mock.calls.at(-1)?.[0]).toMatchObject({ tab: 'live' })
  })

  it('finds a whole id the page holds under another tab without a read', async () => {
    const live = row(4, 'RUNNING')
    await list(page([...loaded, live], null))
    // The list lands on Live (something runs); go to Recent and paste the live id.
    fireEvent.click(screen.getByRole('tab', { name: /^Recent/ }))
    search(live.id)
    await waitFor(() => expect(drawn()).toEqual([live.id]))
    expect(api.loadTask).not.toHaveBeenCalled()
  })

  it('draws a 404 as an answer for the whole tenant', async () => {
    api.loadTask.mockResolvedValue({
      status: 'error',
      error: { kind: 'not_found', httpStatus: 404, code: 'not_found', message: 'no such task' },
    } satisfies Result<Task>)
    await list(page(loaded, 'next-1'))
    search(tid(4242))
    const mark = await waitFor(() => {
      const m = document.querySelector<HTMLElement>('.ctl-empty .ctl-mark')
      expect(m?.getAttribute('aria-label')).toMatch(/HTTP 404/)
      return m!
    })
    expect(mark.className).toContain('is-zero')
    expect(mark.getAttribute('aria-label')).toMatch(/whole tenant/)
  })

  it('draws a failed lookup as a failed read, never as zero', async () => {
    api.loadTask.mockResolvedValue({
      status: 'error',
      error: { kind: 'server_error', httpStatus: 500, code: null, message: 'boom' },
    } satisfies Result<Task>)
    await list(page(loaded, 'next-1'))
    search(tid(4243))
    await waitFor(() => expect(document.querySelector('.ctl-empty .ctl-mark.is-unread')).not.toBeNull())
    expect(document.querySelector('.ctl-mark.is-zero')).toBeNull()
  })

  it('does not call a search over a capped page that finds nothing a real zero', async () => {
    await list(page(loaded, 'next-1'))
    search('no-such-step')
    await waitFor(() => expect(document.querySelector('.ctl-empty .ctl-mark')).not.toBeNull())
    expect(document.querySelector('.ctl-mark.is-zero'), 'a capped page measured an absence').toBeNull()
    expect(document.querySelector('.ctl-empty .ctl-mark.is-partial')).not.toBeNull()
    expect(api.loadTask).not.toHaveBeenCalled()
  })

  it('still calls a search over the whole list that finds nothing a real zero (control)', async () => {
    await list(page(loaded, null))
    search('no-such-step')
    await waitFor(() => expect(document.querySelector('.ctl-empty .ctl-mark.is-zero')).not.toBeNull())
  })
})

describe('WA-2: "load more" follows the next page', () => {
  it('reads the page after the token into the list, counts it, and stops at the end', async () => {
    const first = [row(10, 'SUCCEEDED'), row(11, 'SUCCEEDED')]
    const second = [row(5, 'FAILED'), row(6, 'SUCCEEDED')]
    await list(page(first, 'tok-2'))
    const recent = () => screen.getByRole('tab', { name: /^Recent/ })
    expect(recent().textContent).toContain('2+')

    api.loadTasks.mockResolvedValue(page(second, null))
    fireEvent.click(screen.getByRole('button', { name: /^Load \d+ more$/ }))
    await waitFor(() => expect(drawn()).toHaveLength(4))

    const [, opts] = api.loadTasks.mock.calls.find(([, o]) => (o as { pageToken?: string } | undefined)?.pageToken !== undefined)!
    expect(opts).toMatchObject({ pageToken: 'tok-2', view: 'summary' })
    // The whole list is read: the count is exact, and there is nothing more to load.
    expect(recent().textContent).toMatch(/Recent\s*4$/)
    expect(screen.queryByRole('button', { name: /^Load \d+ more$/ })).toBeNull()
    expect(drawn()).toContain(tid(5))
  })

  it('says a failed page read failed and keeps what it has', async () => {
    const first = [row(10, 'SUCCEEDED')]
    await list(page(first, 'tok-2'))
    api.loadTasks.mockResolvedValue({ status: 'error', error: { kind: 'server_error', httpStatus: 503, code: null, message: 'unavailable' } })
    fireEvent.click(screen.getByRole('button', { name: /^Load \d+ more$/ }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('unavailable'))
    expect(drawn()).toEqual([tid(10)])
    expect(screen.getByRole('button', { name: /^Load \d+ more$/ })).toBeTruthy()
  })

  it('offers no "load more" on a page that is the whole list', async () => {
    await list(page([row(1, 'SUCCEEDED')], null))
    expect(screen.queryByRole('button', { name: /^Load \d+ more$/ })).toBeNull()
  })
})

describe('WA-4: the list keeps its order across re-reads', () => {
  it('orders Live by when a task was submitted, which a write does not move', () => {
    const older = row(1, 'RUNNING')
    const newer = row(2, 'RUNNING')
    // The older task has just checkpointed: its updated_at is now the newest.
    const touched = { ...older, updated_at: '2026-10-11T12:59:59.000Z' }
    expect([older, newer].sort((a, b) => listOrder(a, b, 'live')).map((t) => t.id)).toEqual([newer.id, older.id])
    expect([touched, newer].sort((a, b) => listOrder(a, b, 'live')).map((t) => t.id)).toEqual([newer.id, older.id])
  })

  it('breaks a tie by the id, so equal times cannot swap between reads', () => {
    const a = row(1, 'RUNNING', { created_at: '2026-10-11T10:00:00.000Z' })
    const b = row(2, 'RUNNING', { created_at: '2026-10-11T10:00:00.000Z' })
    expect([a, b].sort((x, y) => listOrder(x, y, 'live'))).toEqual([b, a].sort((x, y) => listOrder(x, y, 'live')))
  })

  it('draws the Live rows in the same order before and after a task writes', async () => {
    const rows = [row(1, 'RUNNING'), row(2, 'RUNNING'), row(3, 'RUNNING')]
    await list(page(rows, null))
    const before = drawn()
    expect(before).toEqual([tid(3), tid(2), tid(1)])
    // The same three, re-sorted with the oldest one freshly written.
    const touched = rows.map((t) => (t.id === tid(1) ? { ...t, updated_at: '2026-10-11T12:59:59.000Z' } : t))
    expect([...touched].sort((a, b) => listOrder(a, b, 'live')).map((t) => t.id)).toEqual(before)
  })
})

describe('WA-9: the Recent state counts follow the profile filter', () => {
  it('counts only the chosen profile’s rows', async () => {
    const rows = [
      row(1, 'SUCCEEDED', { runner_profile: 'indexer' }),
      row(2, 'FAILED', { runner_profile: 'indexer' }),
      row(3, 'SUCCEEDED', { runner_profile: 'claude-code' }),
      row(4, 'SUCCEEDED', { runner_profile: 'claude-code' }),
      row(5, 'FAILED', { runner_profile: 'claude-code' }),
    ]
    await list(page(rows, null))
    const seg = () => screen.getByRole('group', { name: 'Recent, by state' })
    const count = (word: string) => {
      const b = within(seg()).getAllByRole('button').find((x) => (x.textContent ?? '').startsWith(word))!
      return Number((b.textContent ?? '').replace(/\D+/g, ''))
    }
    expect(count('all')).toBe(5)
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'indexer' } })
    await waitFor(() => expect(drawn()).toHaveLength(2))
    expect(count('all')).toBe(2)
    expect(count('failed')).toBe(1)
    expect(count('succeeded')).toBe(1)
  })
})

describe('WA-15: a deep-linked agent’s row is scrolled into view', () => {
  it('scrolls the open agent’s row into view once it is drawn', async () => {
    // jsdom lays nothing out and has no `scrollIntoView`; the test supplies
    // one that records which row asked to be shown.
    const scrolled: string[] = []
    const proto = HTMLElement.prototype as unknown as { scrollIntoView?: unknown }
    const had = Object.prototype.hasOwnProperty.call(proto, 'scrollIntoView')
    proto.scrollIntoView = function (this: HTMLElement) {
      scrolled.push(this.getAttribute('data-task-id') ?? '')
    }
    onTestFinished(() => {
      if (!had) delete proto.scrollIntoView
    })
    const rows = Array.from({ length: 30 }, (_, i) => row(i + 1, 'SUCCEEDED'))
    await list(page(rows, null), tid(2))
    await waitFor(() => expect(scrolled).toContain(tid(2)))
    // Once: a later render does not pull the reader back.
    await act(async () => {
      search('')
    })
    expect(scrolled.filter((id) => id === tid(2))).toHaveLength(1)
  })
})
