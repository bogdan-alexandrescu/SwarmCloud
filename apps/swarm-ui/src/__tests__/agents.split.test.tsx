// THE SPLIT DETAIL (agents.html V1; agent-detail-2.html A; viewers.html A).
//
// #503 measured, at 1440 on /agents/live/<id>: no header row (the title was
// the task id, Stop a small chip mid-pane), a boxed segmented control with no
// counts for the tabs, a transparent 6px grip with no handle, and the
// collapse toggle at the top of the detail rather than in the list header.
// V1 draws a header row -- state pill, the agent's name, Copy link, Stop --
// underline tabs with counts, a divider with a visible handle that snaps to
// 64px / 380px / 50% and remembers per device, and the log docked under the
// detail column across every tab. agent-detail-2.html A adds a Children tab on
// a parent and a "child of …" link on a child, shown only when the API serves
// the fields.
//
// MUTATIONS, one per block: head the split with the task id; draw a tab's
// unknown count as 0; drop `ag-divider` or the drag's stop labels; write the
// snap on every pointer move; offer the Children tab for a task without
// `parent_task_id`; trust a list read whose rows carry no parent field; key
// the log dock on the pane.

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Task, TaskPage } from '../types'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTask: vi.fn(),
  loadChildren: vi.fn(),
  loadAttempts: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadAgentRun: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AgentSplit } = await import('../AgentSplit')
const { resetListSnap } = await import('../listSnap')
const { childOrder, childrenOf, waitingOn } = await import('../AgentChildren')

const ID = 'task_0123456789abcdef0123'
const PARENT = 'task_ffffffffffffffffffff'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function agent(over: Partial<Task> = {}): Task {
  return runTask({
    id: ID,
    state: 'RUNNING',
    runner_profile: 'claude-code',
    resource_class: 'standard',
    workflow_id: 'wf_a',
    step_id: 'fix-heartbeat',
    attempt_count: 2,
    completed_at: null,
    result_summary: null,
    ...over,
  })
}

type Pane = 'detail' | 'logs' | 'children' | 'attempts' | 'artifacts' | 'checkpoints'
const PANES: readonly Pane[] = ['logs', 'children', 'attempts', 'artifacts', 'checkpoints']

/** The split under a router of its own: a tab's address is the pane it opens (U10a D36). */
function Routed({ start, onGo }: { start: Pane; onGo?: (to: string) => void }) {
  const [pane, setPane] = useState<Pane>(start)
  const go = (to: string) => {
    onGo?.(to)
    const last = to.split('/').pop() as Pane
    setPane(PANES.includes(last) ? last : 'detail')
  }
  return (
    <div className="app has-inspector">
      <AgentSplit taskId={ID} pane={pane} artifact={null} closeTo="work/running/live" go={go} base={`work/task/${ID}`} />
    </div>
  )
}

function split(pane: Pane = 'detail', onGo?: (to: string) => void) {
  return <Routed start={pane} onGo={onGo} />
}

function tab(name: string): HTMLElement {
  const list = screen.getByRole('tablist', { name: 'Agent panes' })
  const t = within(list)
    .getAllByRole('tab')
    .find((b) => b.querySelector('.c-tab-label')?.textContent === name)
  expect(t, `no ${name} tab`).toBeTruthy()
  return t!
}

beforeEach(() => {
  localStorage.clear()
  resetListSnap()
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  api.loadChildren.mockResolvedValue(ok({ tasks: [] } satisfies TaskPage))
})

afterEach(() => {
  delete (navigator as unknown as { clipboard?: unknown }).clipboard
  delete document.documentElement.dataset.agentList
})

describe('the detail has a header row: state pill, name, Copy link, Stop', () => {
  it('heads the split with the agent’s name, not its id, and puts the id, profile, class and units under it', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    render(split())
    const head = await waitFor(() => {
      const h = document.querySelector<HTMLElement>('.ag-head')
      expect(h?.querySelector('.ag-head-title')?.textContent).toBe('fix-heartbeat')
      return h!
    })
    expect(head.querySelector('.sk-st')?.textContent).toMatch(/running/i)
    // ONCE, IN THE HEADER BLOCK (walkthrough B): the whole id with its copy,
    // then profile · class · units · gen as the first of the facts.
    expect(head.querySelector('.ag-head-id .ad-id-text')?.textContent).toBe(ID)
    expect(head.querySelector('.ag-head-facts .ag-head-shape')?.textContent).toBe('claude-code · standard · 1u · gen 1')
    // Stop is in the header, once.
    expect(within(head).getByRole('button', { name: 'stop' })).toBeTruthy()
  })

  it('copies the address of this agent', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    render(split())
    const copy = await screen.findByRole('button', { name: 'Copy link' })
    fireEvent.click(copy)
    await waitFor(() => expect(document.querySelector('.ag-head-copied')?.textContent).toBe('copied'))
    expect(writeText).toHaveBeenCalledWith(window.location.href)
  })

  it('says the read is in flight, and heads with the id until the task is read', () => {
    api.loadTask.mockReturnValue(new Promise(() => {}))
    render(split())
    expect(document.querySelector('.ag-head-title')?.textContent).toBe(ID)
    expect(document.querySelector('.ag-head-state')?.textContent).toBe('reading')
    expect(document.querySelector('.ag-head .run-stop-btn')).toBeNull()
  })
})

describe('underline tabs with counts, and an unknown count is a dash with its reason', () => {
  it('draws the four panes as the canonical `Tabs` (tablist form), Attempts counted from the task, the rest a dash until known', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    render(split('attempts'))
    const list = screen.getByRole('tablist', { name: 'Agent panes' })
    expect(list.classList.contains('c-tabs'), 'the strip is not the canonical Tabs').toBe(true)
    expect(list.classList.contains('c-seg'), 'the boxed segmented control is back').toBe(false)
    await waitFor(() => expect(tab('Attempts').querySelector('.c-tabs em')?.textContent).toBe('2'))
    expect(tab('Attempts').getAttribute('aria-selected')).toBe('true')
    const artifacts = tab('Artifacts').querySelector('.c-tabs em')!
    expect(artifacts.textContent).toBe('—')
    expect(artifacts.getAttribute('title')).toMatch(/not known/)
    expect(tab('Checkpoints').querySelector('.c-tabs em')?.textContent).toBe('—')
    expect(tab('Details').querySelector('.c-tabs em')).toBeNull()
  })

  it('counts the files once the manifest is written', async () => {
    api.loadTask.mockResolvedValue(
      ok(agent({ state: 'SUCCEEDED', result_summary: { artifacts: [{ name: 'a.md', bytes: 1, uri: 'gs://x/a.md' }, { name: 'b.md', bytes: 1, uri: 'gs://x/b.md' }] } })),
    )
    render(split())
    await waitFor(() => expect(tab('Artifacts').querySelector('.c-tabs em')?.textContent).toBe('2'))
  })
})

describe('the divider has a handle, snaps to three stops, and remembers per device', () => {
  it('is a visible handle that steps between the stops on the arrow keys and remembers the stop', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    render(split())
    const divider = screen.getByRole('separator', { name: 'Resize the agent list' })
    expect(divider.classList.contains('ag-divider')).toBe(true)
    expect(divider.getAttribute('aria-valuetext')).toBe('compact list, 380px')
    expect(document.documentElement.dataset.agentList).toBe('list')
    fireEvent.keyDown(divider, { key: 'ArrowRight' })
    expect(divider.getAttribute('aria-valuetext')).toBe('list at half the width')
    expect(localStorage.getItem('swarm.agents.list')).toBe('half')
    expect(document.documentElement.style.getPropertyValue('--list-w')).toBe('50%')
  })

  it('draws the three stops while dragged, lights the nearest, and keeps only the one it is released on', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    render(split())
    const divider = screen.getByRole('separator', { name: 'Resize the agent list' })
    fireEvent.pointerDown(divider, { pointerId: 1, clientX: 380 })
    const snaps = document.querySelector<HTMLElement>('.ag-snaps')!
    expect(snaps.getAttribute('role')).toBe('status')
    expect([...snaps.querySelectorAll('.ag-snap')].map((s) => s.textContent)).toEqual([
      'strip · 64px',
      'compact · 380px',
      'wide · 50%',
    ])
    // jsdom lays nothing out, so the work area is 0px wide and its half is 0:
    // a pointer at 70px is nearest the strip.
    fireEvent.pointerMove(divider, { pointerId: 1, clientX: 70 })
    expect(snaps.querySelector('.ag-snap.is-on')?.textContent).toBe('strip · 64px')
    expect(snaps.textContent).toContain('release to snap to strip')
    expect(localStorage.getItem('swarm.agents.list'), 'a stop passed over mid-drag was kept').toBeNull()
    fireEvent.pointerUp(divider, { pointerId: 1 })
    expect(localStorage.getItem('swarm.agents.list')).toBe('strip')
    expect(document.querySelector('.ag-snaps')).toBeNull()
  })

  it('folds the list with [ as the list header’s « does', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    render(split())
    fireEvent.keyDown(window, { key: '[' })
    expect(document.documentElement.dataset.agentList).toBe('strip')
    fireEvent.keyDown(window, { key: '[' })
    expect(document.documentElement.dataset.agentList).toBe('list')
  })
})

describe('the log is a tab, not a dock across the tabs (U11a, owner decision 2026-10-04)', () => {
  it('draws the log on Logs only, and keeps what the reader typed while the agent stays open there', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    render(split('logs'))
    const logs = await screen.findByRole('region', { name: 'Logs' })
    expect(logs.closest('.ag-split-pane'), 'the log is not in the pane').not.toBeNull()
    fireEvent.change(within(logs).getByRole('searchbox', { name: 'Search this window' }), { target: { value: 'lease' } })
    fireEvent.click(tab('Checkpoints'))
    expect(screen.queryByRole('region', { name: 'Logs' }), 'the log is drawn on another tab').toBeNull()
    fireEvent.click(tab('Logs'))
    expect(screen.getByRole('region', { name: 'Logs' })).toBeTruthy()
  })
})

describe('children (D15): a tab on a parent, a link on a child, only when the API serves them', () => {
  it('offers no Children tab for an API that sends no parent field', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    render(split())
    await waitFor(() => expect(tab('Attempts').querySelector('.c-tabs em')?.textContent).toBe('2'))
    const names = [...document.querySelectorAll('.c-tabs[role="tablist"] .c-tab-label')].map((t) => t.textContent)
    expect(names).toEqual(['Details', 'Logs', 'Attempts', 'Artifacts', 'Checkpoints'])
    expect(api.loadChildren).not.toHaveBeenCalled()
  })

  it('counts a served parent’s children, says the cap is not served, and a parked parent holds no capacity', async () => {
    api.loadTask.mockResolvedValue(
      ok(agent({ state: 'PARKED', park_reason: 'CHILDREN_INCOMPLETE', parent_task_id: null, metadata: { child_await_resumes: 1 } })),
    )
    const kid = (id: string, state: Task['state'], created: string) =>
      runTask({ id, state, parent_task_id: ID, parent_attempt_id: 'att_2', created_at: created, step_id: null, workflow_id: null })
    api.loadChildren.mockResolvedValue(
      ok({
        tasks: [
          kid('task_c1', 'SUCCEEDED', '2026-10-02T14:00:00Z'),
          kid('task_c2', 'RUNNING', '2026-10-02T14:01:00Z'),
          kid('task_c3', 'FAILED', '2026-10-02T14:02:00Z'),
        ],
      } satisfies TaskPage),
    )
    render(split())
    await waitFor(() => expect(tab('Children').querySelector('.c-tabs em')?.textContent).toBe('3'))
    fireEvent.click(tab('Children'))
    const pane = await screen.findByRole('region', { name: 'Children' })
    expect(tab('Children').getAttribute('aria-selected')).toBe('true')
    await waitFor(() => expect(pane.querySelectorAll('tbody tr')).toHaveLength(3))
    // Unfinished first, then failed, then succeeded.
    expect([...pane.querySelectorAll('tbody tr')].map((r) => r.getAttribute('data-task-id'))).toEqual(['task_c2', 'task_c3', 'task_c1'])
    // "Waiting on" counts what has not ended: a failed child has ended.
    expect(pane.querySelector('.ag-children-facts')?.textContent).toMatch(/waiting on 1 · 3 of —/)
    expect(pane.textContent).toContain('depth 1: a child cannot have children')
    // The parked parent: no lease, no capacity, nobody needs to act; the
    // deadline is a setting the API does not serve.
    const callout = pane.querySelector('.ag-children-await')!
    expect(callout.textContent).toMatch(/no lease and no capacity/)
    expect(callout.textContent).toMatch(/Nobody needs to act/)
    expect(callout.querySelector('.ag-children-deadline')?.textContent).toMatch(/not served/)
    expect(pane.textContent).toMatch(/await refunds 1 used/)
  })

  it('calls a list read that ignored the filter "not served", and never draws its rows as children', async () => {
    api.loadTask.mockResolvedValue(ok(agent({ parent_task_id: null })))
    // An API without the filter answers the tenant's newest tasks, with no parent field.
    api.loadChildren.mockResolvedValue(ok({ tasks: [runTask({ id: 'task_unrelated' })] } satisfies TaskPage))
    render(split())
    await waitFor(() => expect(tab('Children').querySelector('.c-tabs em')?.textContent).toBe('—'))
    fireEvent.click(tab('Children'))
    const pane = await screen.findByRole('region', { name: 'Children' })
    // In the reader's words (walkthrough E); the route is the mark's sentence.
    await waitFor(() => expect(pane.textContent).toMatch(/not available on this deployment yet/))
    expect(pane.textContent).not.toContain('task_unrelated')
  })

  it('links a child to its parent above its title', async () => {
    api.loadTask.mockResolvedValue(ok(agent({ parent_task_id: PARENT, parent_attempt_id: 'att_9' })))
    render(split())
    const link = await waitFor(() => {
      const a = document.querySelector<HTMLAnchorElement>('.ag-head .ag-parent a')
      expect(a).not.toBeNull()
      return a!
    })
    expect(link.getAttribute('href')).toBe(`#work/task/${PARENT}`)
    expect(link.closest('.ag-parent')?.textContent).toContain('child of')
    expect(link.closest('.ag-parent')?.textContent).toContain('submitted by attempt att_9')
  })

  it('orders, filters and counts by the rules the design states', () => {
    const k = (id: string, state: Task['state'], parent: string | null | undefined, created = '2026-10-02T14:00:00Z') => {
      const t = runTask({ id, state, created_at: created })
      if (parent !== undefined) t.parent_task_id = parent
      return t
    }
    const page = { tasks: [k('a', 'QUEUED', ID), k('b', 'SUCCEEDED', ID), k('c', 'RUNNING', 'task_other')] }
    const list = childrenOf(page, ID)
    expect(list.served).toBe(true)
    expect(list.served && list.children.map((t) => t.id)).toEqual(['a', 'b'])
    expect(childrenOf({ tasks: [k('d', 'RUNNING', undefined)] }, ID)).toEqual({ served: false })
    expect(childOrder([k('s', 'SUCCEEDED', ID), k('f', 'FAILED', ID), k('q', 'QUEUED', ID), k('r', 'RUNNING', ID)]).map((t) => t.id)).toEqual([
      'r',
      'q',
      'f',
      's',
    ])
    expect(waitingOn([k('f', 'FAILED', ID), k('c', 'CANCELLED', ID), k('r', 'RUNNING', ID)])).toBe(1)
  })
})


describe('the split is ONE drawer: AgentDetail does not draw a second one inside it', () => {
  // Measured live at 1440x900 on /agents/live/<task>: the split (`.ctl-drawer`)
  // held `.ag-split-pane` which held AgentDetail's own `.drawer`, rendered
  // `position: fixed` over it, because the stylesheet flattens only a DIRECT
  // child `.ctl-drawer > .drawer` and U1 (#517) put the pane between them.
  it('has exactly one dialog, no nested .drawer and one close control', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    render(split())
    await screen.findByText('fix-heartbeat')
    const root = document.querySelector<HTMLElement>('.ag-split')!
    expect(root.querySelectorAll('[role="dialog"]').length + (root.getAttribute('role') === 'dialog' ? 1 : 0)).toBe(1)
    expect(root.querySelectorAll('.drawer'), 'a second .drawer inside the split').toHaveLength(0)
    // AgentSplit's own close is the one; AgentDetail's must not add another.
    expect(root.querySelectorAll('.drawer-close'), 'not exactly one close button').toHaveLength(1)
  })
})
