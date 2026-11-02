// THE SECOND CONSOLE VISUAL-QA RUN, THE AGENT LIST AND THE AGENT PANES (lane
// QAFIX2-AGENTS; part of #1680). Run wf_b6e2441b419f423a84eb, 2026-10-20, the
// fixture build of main at 75148351, 1440 and 390, light and dark. One block
// per finding; each assertion names the mutation that turns it red.
//
//   task-009            `/children` selected no tab: an agent that does not
//                       offer children had no Children tab, so the route's
//                       pane sat under a strip with nothing selected.
//   a11y-keyboard-001   Live / Waiting / Recent and the pane strip carried
//                       role=tab without the tabs pattern: arrows did
//                       nothing and every tab was its own Tab stop.
//   a11y-keyboard-002   opening an agent from its row with Enter beside the
//                       list left focus on the row.
//   QA-111              the sort button read `Started ⇅` over a list in its
//                       own order -- the newest END first on Recent.
//   QA-116              at 390 `More 2 ▾` read as two hidden tabs or two
//                       checkpoints.
//
// user-event is not a dependency of this package; keys go through
// `fireEvent.keyDown`, which is what the handlers read.

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Task, TaskPage, TaskState } from '../types'
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
    started_at: iso(40 - n),
    completed_at: done ? iso(30 - n) : null,
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
const opener = (id: string) => document.querySelector<HTMLElement>(`.row.is-compact[data-task-id="${id}"] > .cr-open`)!
const panes = () => screen.getByRole('tablist', { name: 'Agent panes' })
const paneTab = (label: string) =>
  within(panes())
    .getAllByRole('tab')
    .find((b) => b.querySelector('.c-tab-label')?.textContent === label)

/** The split beside the list, as `.app.has-inspector` draws it at 1440 (`position: sticky`, so not an overlay). */
let beside: HTMLStyleElement | null = null
function sideBySide(): void {
  beside = document.createElement('style')
  beside.textContent = '.ag-split { position: sticky; top: 0; }'
  document.head.appendChild(beside)
}

/** The list and the split as App routes them: a row opens the agent, the split's close goes back to the list. */
function Work({ start = null }: { start?: string | null }) {
  const [open, setOpen] = useState<string | null>(start)
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

describe('task-009: the /children route has a Children tab, and it is the selected one', () => {
  it('draws Children on the strip and selects it for an agent that does not otherwise offer children', async () => {
    // The runfixture task carries no `parent_task_id` key: a deployment that
    // does not serve the child fields, so `offersChildren` is false.
    const t = row(1, 'FAILED')
    serve([t])
    render(<AgentSplit taskId={t.id} pane="children" artifact={null} closeTo="work/running/recent" go={() => {}} base={`work/task/${t.id}`} />)
    await waitFor(() => expect(api.loadTask).toHaveBeenCalled())
    await waitFor(() => expect(document.querySelector('.ag-head-title')).not.toBeNull())
    // MUTATION: offer Children only when `offersChildren(task)` -- the route
    // draws its pane under a strip with no Children tab at all.
    const children = paneTab('Children')
    expect(children).toBeDefined()
    // MUTATION: select nothing on `/children` -- the strip has no
    // aria-selected tab, which is what all four run-2 captures showed.
    expect(children!.getAttribute('aria-selected')).toBe('true')
    const selected = within(panes())
      .getAllByRole('tab')
      .filter((b) => b.getAttribute('aria-selected') === 'true')
    expect(selected).toEqual([children])
  })

  it('draws no Children tab on another pane of the same agent', async () => {
    const t = row(1, 'FAILED')
    serve([t])
    render(<AgentSplit taskId={t.id} pane="detail" artifact={null} closeTo="work/running/recent" go={() => {}} base={`work/task/${t.id}`} />)
    await waitFor(() => expect(document.querySelector('.ag-head-title')).not.toBeNull())
    // MUTATION: offer Children on every agent -- a child or a deployment
    // that does not serve the field would show a tab that can only read 0.
    expect(paneTab('Children')).toBeUndefined()
  })
})

describe('a11y-keyboard-001: the tab strips follow the WAI-ARIA tabs pattern', () => {
  it('Live / Waiting / Recent: one Tab stop, arrows move and select, Home and End go to the ends', async () => {
    serve([row(1, 'RUNNING'), row(2, 'READY'), row(3, 'FAILED')])
    render(<AgentsScreen onOpen={() => {}} taskId={null} />)
    await waitFor(() => expect(document.querySelector('.row.is-compact')).not.toBeNull())
    const list = screen.getByRole('tablist', { name: 'Agents' })
    const tabs = within(list).getAllByRole('tab')
    const selected = () => tabs.findIndex((t) => t.getAttribute('aria-selected') === 'true')
    const start = selected()
    // MUTATION: drop the roving tabIndex -- every tab is a Tab stop again.
    expect(tabs.map((t) => t.tabIndex)).toEqual(tabs.map((_, i) => (i === start ? 0 : -1)))
    tabs[start]!.focus()
    fireEvent.keyDown(tabs[start]!, { key: 'ArrowRight' })
    const next = (start + 1) % tabs.length
    // MUTATION: drop the ArrowRight branch -- focus and selection stay put.
    expect(document.activeElement).toBe(tabs[next])
    expect(selected()).toBe(next)
    expect(tabs.map((t) => t.tabIndex)).toEqual(tabs.map((_, i) => (i === next ? 0 : -1)))
    fireEvent.keyDown(tabs[next]!, { key: 'End' })
    // MUTATION: drop End.
    expect(document.activeElement).toBe(tabs[tabs.length - 1])
    fireEvent.keyDown(tabs[tabs.length - 1]!, { key: 'ArrowRight' })
    // MUTATION: stop at the last tab instead of wrapping to the first.
    expect(document.activeElement).toBe(tabs[0])
    fireEvent.keyDown(tabs[0]!, { key: 'ArrowLeft' })
    // MUTATION: drop ArrowLeft, or let it stop at the first tab.
    expect(document.activeElement).toBe(tabs[tabs.length - 1])
    fireEvent.keyDown(tabs[tabs.length - 1]!, { key: 'Home' })
    // MUTATION: drop Home.
    expect(document.activeElement).toBe(tabs[0])
    expect(selected()).toBe(0)
  })

  it('the agent pane strip: one Tab stop, arrows move and open the next pane', async () => {
    const t = row(1, 'FAILED')
    serve([t])
    const go = vi.fn()
    const { rerender } = render(
      <AgentSplit taskId={t.id} pane="detail" artifact={null} closeTo="work/running/recent" go={go} base={`work/task/${t.id}`} />,
    )
    await waitFor(() => expect(document.querySelector('.ag-head-title')).not.toBeNull())
    const tabs = within(panes()).getAllByRole('tab')
    // MUTATION: drop the strip's roving tabIndex -- six Tab stops.
    expect(tabs.map((b) => b.tabIndex)).toEqual(tabs.map((_, i) => (i === 0 ? 0 : -1)))
    tabs[0]!.focus()
    fireEvent.keyDown(tabs[0]!, { key: 'ArrowRight' })
    // MUTATION: drop the strip's ArrowRight -- focus stays on Details and nothing opens.
    expect(document.activeElement).toBe(tabs[1])
    expect(go).toHaveBeenLastCalledWith(`work/task/${t.id}/logs`)
    rerender(<AgentSplit taskId={t.id} pane="logs" artifact={null} closeTo="work/running/recent" go={go} base={`work/task/${t.id}`} />)
    const again = within(panes()).getAllByRole('tab')
    // MUTATION: leave the Tab stop on the first tab after the selection moved.
    expect(again.map((b) => b.tabIndex)).toEqual(again.map((_, i) => (i === 1 ? 0 : -1)))
    fireEvent.keyDown(again[1]!, { key: 'End' })
    // MUTATION: drop End on the pane strip.
    expect(document.activeElement).toBe(again[again.length - 1])
    expect(go).toHaveBeenLastCalledWith(`work/task/${t.id}/checkpoints`)
    fireEvent.keyDown(again[again.length - 1]!, { key: 'Home' })
    // MUTATION: drop Home on the pane strip.
    expect(document.activeElement).toBe(again[0])
    expect(go).toHaveBeenLastCalledWith(`work/task/${t.id}`)
  })
})

describe('a11y-keyboard-002: opening an agent by keyboard moves focus into it, and closing it gives focus back', () => {
  it('beside the list: Enter on a row focuses the agent panel; Escape returns to the row', async () => {
    sideBySide()
    const a = row(1, 'FAILED')
    const b = row(2, 'SUCCEEDED')
    serve([a, b])
    render(<Work />)
    await waitFor(() => expect(document.querySelector('.row.is-compact')).not.toBeNull())
    const rowA = opener(a.id)
    rowA.focus()
    // A key's click: `detail` 0, as a browser dispatches for Enter on a button.
    fireEvent.click(rowA, { detail: 0 })
    const panel = await waitFor(() => screen.getByRole('dialog', { name: `Agent ${a.id}` }))
    // MUTATION: move focus in only over an overlay (`isOverlay`) -- beside
    // the list focus stays on the row, and the panel's first control is the
    // 9th Tab stop.
    await waitFor(() => expect(document.activeElement).toBe(panel))
    fireEvent.keyDown(panel, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    // MUTATION: drop the return of focus on close.
    expect(document.activeElement).toBe(opener(a.id))
  })

  it('beside an open agent: Enter on another row moves focus to the new agent, and Escape back to that row', async () => {
    sideBySide()
    const a = row(1, 'FAILED')
    const b = row(2, 'SUCCEEDED')
    serve([a, b])
    render(<Work start={a.id} />)
    await waitFor(() => expect(screen.getByRole('dialog', { name: `Agent ${a.id}` })).toBeTruthy())
    await waitFor(() => expect(opener(b.id)).not.toBeNull())
    opener(b.id).focus()
    fireEvent.click(opener(b.id), { detail: 0 })
    const panel = await waitFor(() => screen.getByRole('dialog', { name: `Agent ${b.id}` }))
    // MUTATION: take focus in only on the split's first open -- switching
    // agents from the list leaves focus on the row.
    await waitFor(() => expect(document.activeElement).toBe(panel))
    fireEvent.keyDown(panel, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    // MUTATION: return focus to whatever held it at the split's first open.
    expect(document.activeElement).toBe(opener(b.id))
  })

  it('a mouse click beside the list still leaves focus where it was', async () => {
    sideBySide()
    const a = row(1, 'FAILED')
    serve([a])
    render(<Work />)
    await waitFor(() => expect(document.querySelector('.row.is-compact')).not.toBeNull())
    const search = screen.getByRole('searchbox', { name: 'Find by name or task id' })
    search.focus()
    await act(async () => {
      fireEvent.click(opener(a.id), { detail: 1 })
    })
    await waitFor(() => expect(screen.getByRole('dialog', { name: `Agent ${a.id}` })).toBeTruthy())
    // MUTATION: move focus in on every open -- a pointer reader beside the
    // list loses their place in the search box (V152).
    expect(document.activeElement).toBe(search)
  })
})

describe('QA-111: the sort button says the order the list is in', () => {
  it('reads the list’s own order on Recent as the end, and each sorted order as the start', async () => {
    serve([row(1, 'FAILED'), row(2, 'SUCCEEDED')])
    render(<AgentsScreen onOpen={() => {}} taskId={null} />)
    await waitFor(() => expect(document.querySelector('.row.is-compact')).not.toBeNull())
    fireEvent.click(screen.getByRole('tab', { name: /^Recent/ }))
    const sort = document.querySelector<HTMLElement>('.ag-sort')!
    // MUTATION: draw `Started ⇅` while the list is newest END first -- the
    // label names the opposite of the order on screen.
    // The direction is words since run 6 (6:flow-find-failure-desktop-03).
    expect(flat(sort)).toBe('Ended, newest first')
    fireEvent.click(sort)
    // MUTATION: keep one label for every state.
    expect(flat(sort)).toBe('Started, newest first')
    fireEvent.click(sort)
    expect(flat(sort)).toBe('Started, oldest first')
    fireEvent.click(sort)
    expect(flat(sort)).toBe('Ended, newest first')
    // MUTATION: name it apart from its words (axe label-content-name-mismatch).
    expect(sort.getAttribute('aria-label')!.startsWith('Ended')).toBe(true)
    // MUTATION: drop what the press will do from the name and the tooltip.
    expect(sort.getAttribute('aria-label')).toBe('Ended, newest first; press to sort by start, newest first')
    expect(sort.getAttribute('title')).toBe(sort.getAttribute('aria-label'))
  })

  it('reads the list’s own order on Live as the submission', async () => {
    serve([row(1, 'RUNNING'), row(2, 'RUNNING')])
    render(<AgentsScreen onOpen={() => {}} taskId={null} />)
    await waitFor(() => expect(document.querySelector('.row.is-compact')).not.toBeNull())
    fireEvent.click(screen.getByRole('tab', { name: /^Live/ }))
    // MUTATION: say `Ended` on a list of agents that have not ended.
    expect(flat(document.querySelector('.ag-sort'))).toBe('Submitted, newest first')
  })
})

describe('QA-116: More says what it holds', () => {
  it('reads `More 3` and names the tabs behind it', async () => {
    const t = row(1, 'FAILED')
    serve([t])
    const proto = HTMLElement.prototype
    const had = {
      client: Object.getOwnPropertyDescriptor(proto, 'clientWidth'),
      offset: Object.getOwnPropertyDescriptor(proto, 'offsetWidth'),
    }
    // A 390 phone's strip: room for four 80px tabs beside More, not six; three
    // stay, with room kept for the tab More holds (BROWSER-TAB-TABS, BT-013).
    Object.defineProperty(proto, 'clientWidth', {
      configurable: true,
      get(this: HTMLElement) {
        return this.classList.contains('ag-tabs-edge') ? 400 : 0
      },
    })
    Object.defineProperty(proto, 'offsetWidth', {
      configurable: true,
      get(this: HTMLElement) {
        if (this.classList.contains('ag-tabs-more-host')) return 70
        return this.getAttribute('role') === 'tab' && this.closest('.ag-tabs-edge') !== null ? 80 : 0
      },
    })
    try {
      render(<AgentSplit taskId={t.id} pane="detail" artifact={null} closeTo="work/running/recent" go={() => {}} base={`work/task/${t.id}`} />)
      await waitFor(() => expect(document.querySelector('.ag-head-title')).not.toBeNull())
      fireEvent(window, new Event('resize'))
      await waitFor(() => expect(within(panes()).getAllByRole('tab')).toHaveLength(3))
      const more = document.querySelector<HTMLElement>('.ag-tabs-more')!
      // MUTATION: draw `More 2` again -- a bare figure that reads as two
      // checkpoints as easily as two hidden tabs.
      // (BROWSER-TAB-TABS, #2423: the names are its name and title, not its glass.)
      expect(flat(more.querySelector('.ag-tabs-more-label'))).toBe('More 3')
      // MUTATION: drop the names from the button's name and tooltip -- a
      // reader cannot tell Checkpoints is behind it without opening it.
      expect(screen.getByRole('button', { name: 'More 3 tabs: Changes, Artifacts, Checkpoints' })).toBe(more)
      expect(more.getAttribute('title')).toBe('Changes, Artifacts, Checkpoints')
    } finally {
      for (const [k, d] of [['clientWidth', had.client], ['offsetWidth', had.offset]] as const) {
        if (d) Object.defineProperty(proto, k, d)
        else delete (proto as unknown as Record<string, unknown>)[k]
      }
    }
  })
})
