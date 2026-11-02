// LANE VQ-AGENTS (part of #2391): BGD's visual QA of Work › Agents and one
// agent's detail, 2026-10-26, against agents.html V1 and agent-details-v3.html A.
//
//   VQ-008  At 390 the agent's tab bar drew the open tab 0px wide: More was
//           fitted at its whole label (`6 more: Logs, Changes, …`, 405px in a
//           358px column), so no tab fitted beside it and the strip shrank.
//           (More draws `More <n>` since BROWSER-TAB-TABS, #2423; what this
//           lane keeps is the fitted strip and More fitted from what it draws.)
//   VQ-009  List rows were named by the step alone (`build`, `implement`)
//           while Overview named them `<workflow title> · implement`.
//   VQ-010  The header was three lines (title, id, meta), and the workflow
//           link read its raw id.
//   VQ-011  The Attempts head said `1 attempt · 28 events` over one card of
//           27, and nothing named the 28th.
//   VQ-012  Over a capped page the list tab read `Waiting 38+` beside the
//           panel's `Waiting 38`.
//
// MUTATIONS, one per block: fit More at its offsetWidth again; draw
// `agentName(task)` in the row; put `TaskIdLine` back under the title row, or
// label the link `task.workflow_id`; drop `beforeSay`; read only the live
// states in `loadAgentsPage`.

import AGENTS_CSS from '../styles/agents.css?raw'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AgentDetail } from '../api'
import type { Result } from '../fetch'
import type { Task, TaskPage, TaskState } from '../types'
import { at, attempt, ev, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTasks: vi.fn(),
  loadTasksInState: vi.fn(),
  loadTask: vi.fn(),
  loadAttempts: vi.fn(),
  loadAgentDetail: vi.fn(),
  loadAgentRun: vi.fn(),
  loadChildren: vi.fn(),
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadTranscript: vi.fn(),
  loadResourceClasses: vi.fn(),
  loadLiveSession: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

const { AgentsScreen } = await import('../Agents')
const { AgentSplit } = await import('../AgentSplit')
const { AgTabsEdge, paneTabs } = await import('../agent-split/tabs')
const { AttemptTimelineScreen } = await import('../AttemptTimeline')

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })
const flat = (el: Element | null | undefined) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

/** A task id of the real shape, built at runtime. */
function tid(n: number): string {
  return `task_${n.toString(16).padStart(20, '0')}`
}

function row(n: number, state: TaskState, over: Partial<Task> = {}): Task {
  return runTask({
    id: tid(n),
    state,
    created_at: new Date(Date.UTC(2026, 9, 26, 10, 0, n)).toISOString(),
    updated_at: new Date(Date.UTC(2026, 9, 26, 11, 0, n)).toISOString(),
    started_at: new Date(Date.UTC(2026, 9, 26, 10, 0, n)).toISOString(),
    completed_at: ['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(state) ? new Date(Date.UTC(2026, 9, 26, 11, 0, n)).toISOString() : null,
    attempt_count: 1,
    max_attempts: 3,
    ...over,
  })
}

function page(tasks: Task[], token: string | null): Result<TaskPage> {
  return ok({ tasks, next_page_token: token, tenant_id: 'eng' })
}

afterEach(() => {
  vi.clearAllMocks()
  unsize()
})

// ------------------------------------------------------------------ VQ-008

const proto = HTMLElement.prototype
const had = {
  client: Object.getOwnPropertyDescriptor(proto, 'clientWidth'),
  offset: Object.getOwnPropertyDescriptor(proto, 'offsetWidth'),
  scroll: Object.getOwnPropertyDescriptor(proto, 'scrollWidth'),
}
function unsize() {
  for (const [k, d] of [['clientWidth', had.client], ['offsetWidth', had.offset], ['scrollWidth', had.scroll]] as const) {
    if (d) Object.defineProperty(proto, k, d)
    else delete (proto as unknown as Record<string, unknown>)[k]
  }
}

// THE BAR AS THE RENDER LAID IT OUT: a 358px column at 390. More's label
// (`More 3`) wants 7px a letter, 42px, and its chevron is 10px; its names are
// read, not drawn. The host is measured at the width CSS already squeezed it
// to (32px, the review's `2 m`), so a fit that reads the host's box reads a
// More that is not there.
const WIDTH: Readonly<Record<string, number>> = { Details: 62, Logs: 46, Changes: 130, Attempts: 84, Artifacts: 130, Checkpoints: 118 }
function sizeBar(column = 358) {
  Object.defineProperty(proto, 'clientWidth', {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList.contains('ag-tabs-edge') ? column : 0
    },
  })
  Object.defineProperty(proto, 'offsetWidth', {
    configurable: true,
    get(this: HTMLElement) {
      if (this.classList.contains('ag-tabs-more-host')) return 32
      if (this.getAttribute('aria-hidden') === 'true' && this.parentElement?.classList.contains('ag-tabs-more')) return 10
      if (this.getAttribute('role') !== 'tab' || this.closest('.ag-tabs-edge') === null) return 0
      return WIDTH[this.querySelector('.c-tab-label')?.textContent ?? ''] ?? 0
    },
  })
  Object.defineProperty(proto, 'scrollWidth', {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList.contains('ag-tabs-more-label') ? 7 * (this.textContent ?? '').length : 0
    },
  })
}

describe('VQ-008: at 390 the open tab is drawn, and More fits beside it', () => {
  function bar(column = 358) {
    sizeBar(column)
    const t = row(1, 'FAILED')
    const tabs = paneTabs({ task: t, base: '/agents/live/x', hasBrowser: false, sessionOpen: false, childCount: null, runs: 1, ckpt: null, ckptCount: 2 })
    render(
      <div className="drawer ctl-drawer ag-split">
        <AgTabsEdge
          current="detail"
          onSelect={() => {}}
          tabs={tabs.map((x) => ({
            key: x.id,
            label: x.label,
            ...((x.id === 'detail' && x.count === null) || x.id === 'logs' ? {} : { count: x.count, why: x.say ?? undefined }),
          }))}
        />
      </div>,
    )
    act(() => {
      fireEvent(window, new Event('resize'))
    })
    const strip = screen.getByRole('tablist', { name: 'Agent panes' })
    return { strip, onBar: () => within(strip).getAllByRole('tab').map((b) => b.querySelector('.c-tab-label')?.textContent ?? '') }
  }

  it('reserves More at what it draws, so Details, Logs and Attempts stay on the bar', () => {
    const { strip, onBar } = bar()
    // MUTATION: fit More at its whole label again and only Details is left, in a strip CSS shrinks to 0;
    // fit it at its squeezed host (32px) and Changes joins the bar, leaving More `2 m`.
    expect(onBar()).toEqual(['Details', 'Logs', 'Attempts'])
    expect(within(strip).getByRole('tab', { name: 'Details' }).getAttribute('aria-selected')).toBe('true')
    // The strip that holds only tabs that fit is marked, so the sheet keeps its width.
    expect(strip.classList.contains('is-fitted')).toBe(true)
    const more = document.querySelector<HTMLElement>('.ag-tabs-more')!
    // The button draws `More 3`, and is still named by every tab it holds (QA-116, run 6; BT-005).
    expect(flat(more.querySelector('.ag-tabs-more-label'))).toBe('More 3')
    expect(screen.getByRole('button', { name: 'More 3 tabs: Changes, Artifacts, Checkpoints' })).toBe(more)
  })

  it('holds the open tab at its width and leaves More’s host to the sheet', () => {
    const { strip } = bar()
    // MUTATION: drop the strip's floor and the open tab can be squeezed to 0px again.
    expect(strip.style.minWidth).toBe('62px')
    // The host is not pinned at a width read from its own squeezed box.
    expect(document.querySelector<HTMLElement>('.ag-tabs-more-host')!.style.minWidth).toBe('')
  })

  it('keeps room at desktop for the widest tab in More, beside `More 2`', () => {
    // 550px: the six tabs are 570px, so More is drawn; every tab but
    // Checkpoints is 452px, beside More's 82px (`More 2` 42, chevron 10, the
    // button's padding and gap 30); with room kept for Checkpoints to be drawn
    // when it is opened, Artifacts joins it in More.
    // MUTATION: drop the room kept for the widest tab in More and Artifacts stays on the bar.
    const { onBar } = bar(550)
    expect(onBar()).toEqual(['Details', 'Logs', 'Changes', 'Attempts'])
    expect(flat(document.querySelector('.ag-tabs-more-label'))).toBe('More 2')
  })

  it('does not mark a strip where every tab fits (control)', () => {
    const { strip } = bar(0)
    expect(document.querySelector('.ag-tabs-more')).toBeNull()
    expect(strip.classList.contains('is-fitted')).toBe(false)
  })

  it('lets More give way and never the fitted strip, in the sheet', () => {
    expect(AGENTS_CSS).toMatch(/\.ag-tabs-edge > \.ag-split-tabs\.is-fitted \{ flex: none; \}/)
    expect(AGENTS_CSS).toMatch(/\.ag-tabs-more-host \{ flex: 0 1 auto; min-width: 0;/)
    expect(AGENTS_CSS).toMatch(/\.ag-tabs-more-label \{[^}]*text-overflow: ellipsis;/)
    expect(AGENTS_CSS).toMatch(/\.ag-tabs-more > \[aria-hidden\] \{ flex: none; \}/)
  })
})

// ------------------------------------------------------------------ VQ-009

describe('VQ-009: a workflow step’s row is named by its workflow, as Overview names it', () => {
  it('reads `<title> · <step>` once the workflow is read, and keeps a lone agent’s name', async () => {
    const title = 'Fix flaky lease heartbeat test (#248)'
    const steps = [
      row(1, 'RUNNING', { workflow_id: 'wf_vq009', step_id: 'implement' }),
      row(2, 'RUNNING', { workflow_id: 'wf_vq009', step_id: 're-review' }),
    ]
    const lone = row(3, 'RUNNING', { runner_profile: 'claude-code' })
    api.loadTasks.mockResolvedValue(page([...steps, lone], null))
    api.loadAttempts.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    api.loadTask.mockImplementation(async (id: string) => {
      const t = [...steps, lone].find((x) => x.id === id)!
      return ok({ ...t, metadata: { title } })
    })
    render(<AgentsScreen onOpen={() => {}} />)
    const name = (id: string) => document.querySelector(`.row.is-compact[data-task-id="${id}"] .cr-name b`)
    // MUTATION: draw `agentName(task)` and the row says `implement` alone.
    await waitFor(() => expect(flat(name(steps[0]!.id))).toBe(`${title} · implement`))
    expect(flat(name(steps[1]!.id))).toBe(`${title} · re-review`)
    // The step is its own element, so the title is the part an ellipsis cuts.
    expect(name(steps[0]!.id)!.querySelector('.cr-step')?.textContent).toMatch(/implement$/)
    // The opener is named as the row reads.
    const opener = document.querySelector(`.row.is-compact[data-task-id="${steps[0]!.id}"] .cr-open`)!
    expect(opener.getAttribute('aria-label')).toMatch(new RegExp(`^${title.replace(/[()#]/g, '\\$&')} · implement · ${steps[0]!.id}`))
    // One read names the workflow's steps; the lone agent is never read for a title.
    expect(api.loadTask).toHaveBeenCalledTimes(1)
    expect(steps.map((t) => t.id)).toContain(api.loadTask.mock.calls[0]![0])
    expect(flat(name(lone.id))).toBe('claude-code agent') // the id is the line under it (QA1-FIX-07, overview-agents-14)
  })
})

// ------------------------------------------------------------------ VQ-010

async function header(t: Task): Promise<HTMLElement> {
  api.loadTask.mockResolvedValue(ok(t))
  api.loadChildren.mockResolvedValue(ok({ tasks: [] }))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadAgentDetail.mockReturnValue(new Promise(() => {}))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.loadResourceClasses.mockReturnValue(new Promise(() => {}))
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  render(<AgentSplit taskId={t.id} pane="attempts" artifact={null} closeTo="work/running/live" go={() => {}} base={`work/task/${t.id}`} />)
  return waitFor(() => {
    const h = document.querySelector<HTMLElement>('.ag-head')
    expect(h?.querySelector('.ag-head-facts a')).toBeTruthy()
    return h!
  })
}

describe('VQ-010: the header is two lines, the workflow by its name and the id on the meta line', () => {
  const STEP = row(10, 'SUCCEEDED', { workflow_id: 'wf_65e76c6a28424bd4a448', step_id: 'implement', metadata: { title: 'quota-broker-aimd' } })

  it('draws the title row and one meta line, with the id and its copy in it', async () => {
    const head = await header(STEP)
    const lines = [...head.children].filter((c) => !c.classList.contains('ag-parent'))
    // MUTATION: put `TaskIdLine` back between the title row and the facts.
    expect(lines.map((l) => l.className.split(' ')[0])).toEqual(['ag-head-row', 'ag-head-facts'])
    const facts = head.querySelector('.ag-head-facts')!
    expect(facts.querySelector('.ag-head-id .tid-text')?.textContent).toBe(STEP.id)
    expect(facts.querySelector<HTMLButtonElement>('.ag-head-id .tid-copy')?.title).toContain(STEP.id)
  })

  it('labels the workflow link with the workflow’s title, the id its title', async () => {
    const head = await header(STEP)
    const link = head.querySelector<HTMLAnchorElement>('.ag-head-facts a')!
    // MUTATION: label it `task.workflow_id` and it reads `wf_65e76c6a… ›`.
    expect(flat(link)).toBe('quota-broker-aimd ›')
    expect(link.title).toBe(STEP.workflow_id)
    expect(link.getAttribute('href')).toBe(`/workflows/${STEP.workflow_id}`)
    expect(link.classList.contains('mono'), 'a name is not drawn in the id face').toBe(false)
  })

  it('keeps the id, in mono, for a workflow submitted without a title (control)', async () => {
    const head = await header({ ...STEP, metadata: null })
    const link = head.querySelector<HTMLAnchorElement>('.ag-head-facts a')!
    expect(flat(link)).toBe(`${STEP.workflow_id} ›`)
    expect(link.classList.contains('mono')).toBe(true)
  })
})

// ------------------------------------------------------------------ VQ-011

describe('VQ-011: the Attempts totals say which events no attempt holds', () => {
  function serve(events: ReturnType<typeof ev>[]) {
    const t = runTask({ state: 'SUCCEEDED', started_at: at(1), completed_at: at(10) })
    api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1)] }))
    api.loadAgentDetail.mockResolvedValue(ok({ task: t, events } as unknown as AgentDetail))
  }

  it('reads `3 events, 1 before any attempt` on the toolbar over a card of 2', async () => {
    serve([ev('submitted', at(-1), null), ev('running', at(1), 'att_1'), ev('succeeded', at(10), 'att_1')])
    render(<AttemptTimelineScreen taskId="tsk_charts" />)
    // MUTATION: drop `beforeSay` and the toolbar reads `3 events` over a card of 2.
    // The head counts attempts only since QA2-FIX-13 (overview-agents-41): the toolbar says the events, once.
    await waitFor(() => expect(screen.getByText('1 attempt')).toBeTruthy())
    expect(flat(document.querySelector('.att-ev-cap'))).toBe('3 events, 1 before any attempt · first page')
    const cards = [...document.querySelectorAll('.att-card')].map((c) => flat(c.querySelector('.ctl-card-head')))
    expect(cards.some((c) => c.startsWith('Before any attempt') && c.includes('1 event'))).toBe(true)
  })

  it('says nothing more when every event is an attempt’s (control)', async () => {
    serve([ev('running', at(1), 'att_1'), ev('succeeded', at(10), 'att_1')])
    render(<AttemptTimelineScreen taskId="tsk_charts" />)
    await waitFor(() => expect(screen.getByText('1 attempt')).toBeTruthy())
    expect(flat(document.querySelector('.att-ev-cap'))).toBe('2 events · first page')
  })
})

// ------------------------------------------------------------------ VQ-012

describe('VQ-012: over a capped page Waiting is counted whole, as the panel counts it', () => {
  // A capped first page of finished rows, two waiting agents on it and one
  // waiting agent older than the page.
  const finished = Array.from({ length: 4 }, (_, i) => row(100 + i, 'SUCCEEDED'))
  const waiting = [row(20, 'READY'), row(21, 'PARKED', { park_reason: 'quota' })]
  const old = row(5, 'READY', { created_at: new Date(Date.UTC(2026, 9, 1)).toISOString() })
  const badge = (name: RegExp) => screen.getByRole('tab', { name }).querySelector('.badge')?.textContent

  it('reads `Waiting 3`, with no `+`, when the waiting states answered whole', async () => {
    api.loadTasks.mockResolvedValue(page([...waiting, ...finished], 'next-1'))
    const all = [...waiting, old, ...finished]
    // One state or a comma list of them, as one `in` query (#2998).
    api.loadTasksInState.mockImplementation(async (state: TaskState) => {
      const asked = new Set(state.split(','))
      const tasks = all.filter((t) => asked.has(t.state))
      return tasks.length === 0 ? { status: 'empty', fetchedAt: Date.now() } : page(tasks, null)
    })
    api.loadAttempts.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    render(<AgentsScreen onOpen={() => {}} />)
    // MUTATION: read only the live states and the tab reads `2+`.
    await waitFor(() => expect(badge(/^Waiting/)).toBe('3'))
    expect(api.loadTasksInState).toHaveBeenCalledTimes(1)
    expect(String(api.loadTasksInState.mock.calls[0]![0]).split(',').sort()).toEqual(['DISPATCHED', 'LEASED', 'PARKED', 'READY', 'RUNNING', 'STARTING'])
    // CONTROL: Recent is still the capped page's, and says so.
    expect(badge(/^Recent/)).toBe('4+')
  })

  it('keeps the lower bound when a waiting-state read fails', async () => {
    api.loadTasks.mockResolvedValue(page([...waiting, ...finished], 'next-1'))
    api.loadTasksInState.mockResolvedValue({ status: 'error', error: { kind: 'unreachable', httpStatus: null, code: null, message: 'no' } })
    api.loadAttempts.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    render(<AgentsScreen onOpen={() => {}} />)
    await waitFor(() => expect(api.loadTasksInState).toHaveBeenCalled())
    await waitFor(() => expect(badge(/^Waiting/)).toBe('2+'))
  })

  it('puts the profile select and the sort on one row at phone width, in the sheet', () => {
    const phone = AGENTS_CSS.slice(AGENTS_CSS.indexOf('THE PROFILE AND THE SORT SHARE A ROW'))
    expect(phone).toMatch(/\.ag-list-head \.ag-tools-tail \{ display: contents; \}/)
    expect(phone).toMatch(/\.ag-tools > \.ag-filter-profile \{ order: -2; flex: 1 1 7em; min-width: 7em; \}/)
    expect(phone).toMatch(/\.ag-tools-tail > \.ag-sort \{ order: -1; \}/)
  })
})
