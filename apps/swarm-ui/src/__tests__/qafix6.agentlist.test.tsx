// CONSOLE VISUAL-QA RUN 6, THE AGENT LIST (lane QAFIX6-AGENTLIST; part of
// #1680). Run wf_31aca00c977e4d37a935, 2026-10-23, the fixture build of main
// at d3cdbd62, 1440 and 390, light and dark. One block per item; each
// assertion names the mutation that turns it red.
//
//   a  6:work-a-007   at 390 the stuck Live / Waiting / Recent strip let the
//                     search field's bottom border show through the gap
//                     under it (its transparent margin);
//   b  6:work-a-008   a STARTING row's meta line left its state out, while
//                     leased and dispatched rows lead with theirs;
//   c  6:flow-find-failure-desktop-03
//                     the sort read `Ended ↓`: the direction is said in words;
//   d  box 6298218679 (list half): the strip's roving tabindex and arrows;
//   e  box 6298219043 an agent opened by a key takes focus, and gives it back.
//
// Layout through `painted` (marks.ts): jsdom applies no `@media` block.
// user-event is not a dependency of this package; keys go through
// `fireEvent.keyDown`, which is what the handlers read.

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Task, TaskPage, TaskState } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTask: vi.fn(),
  loadTasks: vi.fn(),
  loadTasksInStates: vi.fn(),
  loadChildren: vi.fn(),
  loadAttempts: vi.fn(),
  loadLiveSession: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  // Every read this file does not answer never answers, so nothing it does
  // not name can land and change what is asserted.
  const quiet = Object.fromEntries(
    Object.entries(actual)
      .filter(([k, v]) => k.startsWith('load') && typeof v === 'function')
      .map(([k]) => [k, () => new Promise(() => {})]),
  )
  return { ...actual, ...quiet, ...api, USE_FIXTURES: false }
})

const { AgentsScreen } = await import('../Agents')
const { AgentSplit } = await import('../AgentSplit')
const { resetListSnap } = await import('../listSnap')

const PHONE: CascadeEnv = { width: 390, height: 844 }

const NOW = Date.UTC(2026, 9, 23, 12, 0, 0)
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
    started_at: iso(40 - n),
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
  api.loadTask.mockImplementation(async (id: string) => ok(tasks.find((t) => t.id === id)!))
}

const flat = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
const rowOf = (id: string) => document.querySelector<HTMLElement>(`.row.is-compact[data-task-id="${id}"]`)!
const opener = (id: string) => document.querySelector<HTMLElement>(`.row.is-compact[data-task-id="${id}"] > .cr-open`)!

/** The split beside the list, as `.app.has-inspector` draws it at 1440 (`position: sticky`, so not an overlay). */
let beside: HTMLStyleElement | null = null
function sideBySide(): void {
  beside = document.createElement('style')
  beside.textContent = '.ag-split { position: sticky; top: 0; }'
  document.head.appendChild(beside)
}

/** The list and the split as App routes them: a row opens the agent, the split's close goes back to the list. */
function Work() {
  const [open, setOpen] = useState<string | null>(null)
  return (
    <div className="app has-inspector">
      <main className="work">
        <AgentsScreen onOpen={setOpen} taskId={open} />
      </main>
      {open !== null && (
        <AgentSplit
          taskId={open}
          pane="detail"
          artifact={null}
          closeTo="work/running/recent"
          go={(to) => {
            if (to === 'work/running/recent') setOpen(null)
          }}
          base={`work/task/${open}`}
        />
      )}
    </div>
  )
}

async function list(tasks: Task[]): Promise<void> {
  serve(tasks)
  render(<AgentsScreen onOpen={() => {}} taskId={null} />)
  await waitFor(() => expect(document.querySelector('.row.is-compact')).not.toBeNull())
  await act(async () => {})
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
  localStorage.clear()
  resetListSnap()
  api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
  api.loadChildren.mockResolvedValue(ok({ tasks: [], next_page_token: null, tenant_id: 'eng' } satisfies TaskPage))
  api.loadLiveSession.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
})

afterEach(() => {
  beside?.remove()
  beside = null
  vi.useRealTimers()
  window.history.replaceState(null, '', '/')
})

describe('a (6:work-a-007): at 390 the stuck strip hides what scrolls under it', () => {
  it('is sticky on an opaque page background, and paints that background over the gap below it', async () => {
    await list([row(1, 'RUNNING')])
    const strip = screen.getByRole('tablist', { name: 'Agents' })
    expect(strip.classList.contains('ag-list-tabs')).toBe(true)
    expect(painted(strip, 'position', PHONE)).toBe('sticky')
    expect(painted(strip, ['background', 'background-color'], PHONE)).toBe('var(--bg)')
    // The strip's `margin-bottom` is a transparent gap under its rule, and the
    // search field's rounded bottom border scrolled through it. Whatever band
    // the strip leaves under itself is covered in the page's own colour.
    const gap = painted(strip, 'margin-bottom', PHONE) ?? '0'
    if (gap !== '0' && gap !== '0px') {
      // MUTATION: drop the phone rule's `box-shadow` band from `.ag-list-tabs`:
      // the gap is see-through again while the strip is stuck.
      expect(painted(strip, 'box-shadow', PHONE) ?? 'none').toBe(`0 ${gap} 0 var(--bg)`)
    }
  })
})

describe('b (6:work-a-008): a STARTING row says so first, as leased and dispatched rows do', () => {
  it('leads the meta line with `starting ·`', async () => {
    await list([row(1, 'STARTING'), row(2, 'DISPATCHED'), row(3, 'LEASED')])
    // MUTATION: lead only the word-only live rows (leased, dispatched): the
    // starting row's line opens on `attempt 1 of 3`.
    await waitFor(() => expect(flat(rowOf(tid(1)).querySelector('.cr-sub'))).toMatch(/^starting · attempt 1 of 3 · /))
    // Its siblings keep theirs.
    expect(flat(rowOf(tid(2)).querySelector('.cr-sub'))).toMatch(/^dispatched · /)
    expect(flat(rowOf(tid(3)).querySelector('.cr-sub'))).toMatch(/^leased · /)
    // MUTATION: draw the word twice on a row that is not a browser agent.
    expect(flat(rowOf(tid(1)).querySelector('.cr-sub')).match(/starting/g)).toHaveLength(1)
  })
})

describe('c (6:flow-find-failure-desktop-03): the sort says its direction in words', () => {
  it('reads `Ended, newest first` on Recent, and its name starts with what it reads', async () => {
    await list([row(1, 'FAILED'), row(2, 'SUCCEEDED')])
    fireEvent.click(screen.getByRole('tab', { name: /^Recent/ }))
    const sort = document.querySelector<HTMLElement>('.ag-sort')!
    // MUTATION: draw `Ended ↓` again: the direction is an arrow, not words.
    expect(flat(sort)).toContain('newest first')
    expect(flat(sort)).toBe('Ended, newest first')
    // MUTATION: name it apart from its words (QA-030, axe label-content-name-mismatch).
    expect(screen.getByRole('button', { name: /^Ended, newest first/ })).toBe(sort)
    fireEvent.click(sort)
    expect(flat(sort)).toBe('Started, newest first')
    expect(sort.getAttribute('aria-label')!.startsWith(flat(sort))).toBe(true)
    fireEvent.click(sort)
    // MUTATION: keep one direction word for both sorted states.
    expect(flat(sort)).toBe('Started, oldest first')
    expect(sort.getAttribute('aria-label')!.startsWith(flat(sort))).toBe(true)
    fireEvent.click(sort)
    expect(flat(sort)).toBe('Ended, newest first')
  })

  it('reads `Submitted, newest first` on Live', async () => {
    await list([row(1, 'RUNNING'), row(2, 'RUNNING')])
    fireEvent.click(screen.getByRole('tab', { name: /^Live/ }))
    const sort = document.querySelector<HTMLElement>('.ag-sort')!
    // MUTATION: say `Ended` on a list of agents that have not ended.
    expect(flat(sort)).toBe('Submitted, newest first')
    expect(sort.getAttribute('aria-label')!.startsWith('Submitted, newest first')).toBe(true)
  })
})

describe('d (box 6298218679, the list half): Live / Waiting / Recent follow the tabs pattern', () => {
  it('has one Tab stop, and an arrow moves focus and selection together', async () => {
    await list([row(1, 'RUNNING'), row(2, 'READY'), row(3, 'FAILED')])
    const tabs = within(screen.getByRole('tablist', { name: 'Agents' })).getAllByRole('tab')
    const selected = () => tabs.findIndex((t) => t.getAttribute('aria-selected') === 'true')
    const start = selected()
    // MUTATION: drop the roving tabIndex in AgentsBody's strip.
    expect(tabs.filter((t) => t.tabIndex === 0)).toEqual([tabs[start]])
    tabs[start]!.focus()
    fireEvent.keyDown(tabs[start]!, { key: 'ArrowRight' })
    const next = (start + 1) % tabs.length
    // MUTATION: drop the strip's `onKeyDown`: the arrow does nothing.
    expect(document.activeElement).toBe(tabs[next])
    expect(selected()).toBe(next)
    expect(tabs.filter((t) => t.tabIndex === 0)).toEqual([tabs[next]])
  })
})

describe('e (box 6298219043): an agent opened by a key takes focus, and closing gives it back', () => {
  it('Enter on a row puts focus inside the agent; Escape puts it back on the row', async () => {
    sideBySide()
    const a = row(1, 'FAILED')
    serve([a, row(2, 'SUCCEEDED')])
    render(<Work />)
    await waitFor(() => expect(document.querySelector('.row.is-compact')).not.toBeNull())
    opener(a.id).focus()
    // A key's click: `detail` 0, as a browser dispatches for Enter on a button.
    fireEvent.click(opener(a.id), { detail: 0 })
    const panel = await waitFor(() => screen.getByRole('dialog', { name: `Agent ${a.id}` }))
    // MUTATION: drop `focusSplitOnOpen` from CompactRow's click: focus stays on the row.
    await waitFor(() => expect(panel.contains(document.activeElement)).toBe(true))
    fireEvent.keyDown(panel, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(document.activeElement).toBe(opener(a.id))
  })
})
