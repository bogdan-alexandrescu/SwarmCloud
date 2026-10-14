// LIVE-BROWSER QA PASS P12 (2026-10-13, task_4defa61c5eea4a5c87f5), lane
// ROUTES-TABS: three defects of the agent's address, tab bar and back link.
//
// QA12-17: `/agents/live/<id>/details` was read as a task called
// `<id>/details` and drew a 404. Every tab's own name is an address segment
// now, `details` included, and the bar is written back without it.
// QA12-18: at 1440 with the list open the tab bar was cut at `C ›`. The tabs
// that do not fit go into a More menu, so the bar always fits; the open tab
// is never one of them.
// QA12-26: a task that parked while open kept `‹ Live` and the list stayed on
// Live. The back link, the list and the address follow the task's group.
//
// MUTATIONS: drop the `details` alias from `fromAddress` (the 404 case and the
// every-tab-name case go red); return every tab from `fitTabs` (the More case
// goes red); drop the split's `onGroup` call (the parked cases go red).

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import type { Task, TaskPage, TaskState } from '../types'
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

const { AgentSplit, PANE_LABELS, agentTabOf, fitTabs } = await import('../AgentSplit')
const { canonical, fromAddress } = await import('../App')
const { addressToPath, pathToAddress } = await import('../paths')
const { resetListSnap } = await import('../listSnap')

const ID = 'task_4defa61c5eea4a5c87f5'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function agent(state: TaskState = 'RUNNING', over: Partial<Task> = {}): Task {
  return runTask({
    id: ID,
    state,
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

beforeEach(() => {
  localStorage.clear()
  resetListSnap()
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  api.loadChildren.mockResolvedValue(ok({ tasks: [] } satisfies TaskPage))
})

describe('QA12-17: every tab name is an address segment', () => {
  it('opens Details at /agents/live/<id>/details, and writes the bar back without it', () => {
    const p = pathToAddress(`/agents/live/${ID}/details`)
    expect(p).not.toBeNull()
    const r = fromAddress(p!.address)
    expect([r.sectionId, r.tab, r.taskId, r.taskPane]).toEqual(['work', 'running', ID, 'detail'])
    expect(addressToPath(canonical(r), 'live')).toBe(`/agents/live/${ID}`)
    // The singular reads the same way.
    expect(fromAddress(`work/task/${ID}/detail`).taskId).toBe(ID)
  })

  it('opens each tab at its own lower-cased name', () => {
    const named = Object.entries(PANE_LABELS)
    expect(named.length, 'the split names no tabs').toBeGreaterThanOrEqual(8)
    for (const [pane, label] of named) {
      const r = fromAddress(pathToAddress(`/agents/waiting/${ID}/${label.toLowerCase()}`)!.address)
      expect([r.taskId, r.taskPane], `/${label.toLowerCase()}`).toEqual([ID, pane])
    }
  })

  it('still reads a lone `details` under work/task as nothing but an id', () => {
    expect(fromAddress('work/task/details').taskPane).toBe('detail')
    expect(fromAddress('work/task/details').taskId).not.toBe('')
  })
})

describe('QA12-18: the tab bar fits', () => {
  const W = [90, 100, 60, 100, 100, 100, 100, 120]
  const keys = ['detail', 'browser', 'logs', 'changes', 'children', 'attempts', 'artifacts', 'checkpoints']

  it('keeps every tab on the bar when they fit, and moves the rest to More when they do not', () => {
    expect(fitTabs(keys, W, 2000, 70, 'detail')).toEqual(keys)
    const shown = fitTabs(keys, W, 500, 70, 'detail')
    expect(shown).toEqual(['detail', 'browser', 'logs', 'changes'])
  })

  it('never puts the open tab in More', () => {
    const shown = fitTabs(keys, W, 500, 70, 'checkpoints')
    expect(shown).toContain('checkpoints')
    expect(shown.reduce((n, k) => n + W[keys.indexOf(k)]!, 0)).toBeLessThanOrEqual(500 - 70)
    expect(shown[0]).toBe('detail')
  })

  it('draws More with the tabs that did not fit, and choosing one opens it', async () => {
    api.loadTask.mockResolvedValue(ok(agent('RUNNING')))
    const proto = HTMLElement.prototype
    const had = {
      client: Object.getOwnPropertyDescriptor(proto, 'clientWidth'),
      offset: Object.getOwnPropertyDescriptor(proto, 'offsetWidth'),
    }
    Object.defineProperty(proto, 'clientWidth', {
      configurable: true,
      get(this: HTMLElement) {
        return this.classList.contains('ag-tabs-edge') ? 420 : 0
      },
    })
    Object.defineProperty(proto, 'offsetWidth', {
      configurable: true,
      get(this: HTMLElement) {
        if (this.classList.contains('ag-tabs-more-host')) return 70
        return this.getAttribute('role') === 'tab' && this.closest('.ag-tabs-edge') !== null ? 100 : 0
      },
    })
    const go = vi.fn()
    try {
      render(
        <div className="app has-inspector">
          <AgentSplit taskId={ID} pane="detail" artifact={null} closeTo="work/running/live" go={go} base={`work/task/${ID}`} />
        </div>,
      )
      await waitFor(() => expect(document.querySelector('.ag-head-title')?.textContent).toBe('fix-heartbeat'))
      fireEvent(window, new Event('resize'))
      const strip = screen.getByRole('tablist', { name: 'Agent panes' })
      await waitFor(() => expect(within(strip).getAllByRole('tab')).toHaveLength(3))
      const more = screen.getByRole('button', { name: /^More/ })
      expect(more.getAttribute('aria-expanded')).toBe('false')
      fireEvent.click(more)
      expect(more.getAttribute('aria-expanded')).toBe('true')
      const menu = screen.getByRole('group', { name: 'More agent panes' })
      const items = within(menu).getAllByRole('button').map((b) => b.querySelector('.c-tab-label')?.textContent)
      expect(items).toContain('Checkpoints')
      expect(items).not.toContain('Details')
      fireEvent.click(within(menu).getAllByRole('button').find((b) => b.textContent?.startsWith('Checkpoints'))!)
      expect(go).toHaveBeenLastCalledWith(`work/task/${ID}/checkpoints`)
      expect(screen.queryByRole('group', { name: 'More agent panes' })).toBeNull()
    } finally {
      for (const [k, d] of [['clientWidth', had.client], ['offsetWidth', had.offset]] as const) {
        if (d) Object.defineProperty(proto, k, d)
        else delete (proto as unknown as Record<string, unknown>)[k]
      }
    }
  })

  it('draws no More when nothing is measured (every tab fits)', async () => {
    api.loadTask.mockResolvedValue(ok(agent('RUNNING')))
    render(<AgentSplit taskId={ID} pane="detail" artifact={null} closeTo="work/running/live" go={() => {}} base={`work/task/${ID}`} />)
    await waitFor(() => expect(document.querySelector('.ag-head-title')?.textContent).toBe('fix-heartbeat'))
    expect(screen.queryByRole('button', { name: /^More/ })).toBeNull()
  })
})

describe('QA12-18: the sheet draws More at the end of the bar', () => {
  const ENVS: CascadeEnv[] = [{ width: 1440 }, { width: 390, height: 844 }]
  it('lets the strip shrink and keeps More beside it at full width, at 1440 and 390', () => {
    const host = document.createElement('div')
    host.innerHTML =
      '<div class="ag-split"><div class="ag-tabs-edge"><div class="c-tabs ag-split-tabs" role="tablist"></div>' +
      '<span class="c-menu-host ag-tabs-more-host"><button class="ag-tabs-more">More</button></span></div></div>'
    document.body.appendChild(host)
    try {
      for (const env of ENVS) {
        expect(painted(host.querySelector('.ag-tabs-edge')!, 'display', env)).toBe('flex')
        expect(painted(host.querySelector('.ag-split-tabs')!, 'min-width', env)).toBe('0')
        expect(painted(host.querySelector('.ag-tabs-more-host')!, ['flex', 'flex-shrink'], env)).toMatch(/none|^0$/)
        expect(painted(host.querySelector('.ag-tabs-more')!, 'white-space', env)).toBe('nowrap')
      }
    } finally {
      host.remove()
    }
  })
})

describe('QA12-26: the back link and the list follow the task’s group', () => {
  it('maps a state to the list tab that holds it', () => {
    expect(agentTabOf('RUNNING')).toBe('live')
    expect(agentTabOf('LEASED')).toBe('live')
    expect(agentTabOf('PARKED')).toBe('waiting')
    expect(agentTabOf('READY')).toBe('waiting')
    expect(agentTabOf('QUEUED')).toBe('waiting')
    expect(agentTabOf('FAILED')).toBe('recent')
    expect(agentTabOf('SUCCEEDED')).toBe('recent')
  })

  it('tells the router when a task opened under Live is parked', async () => {
    api.loadTask.mockResolvedValue(ok(agent('PARKED')))
    const onGroup = vi.fn()
    render(
      <AgentSplit taskId={ID} pane="detail" artifact={null} closeTo="work/running/live" go={() => {}} onGroup={onGroup} base={`work/task/${ID}`} />,
    )
    await waitFor(() => expect(onGroup).toHaveBeenCalledWith('waiting'))
  })

  it('says nothing when the task is already in the list it was opened from', async () => {
    api.loadTask.mockResolvedValue(ok(agent('RUNNING')))
    const onGroup = vi.fn()
    render(
      <AgentSplit taskId={ID} pane="detail" artifact={null} closeTo="work/running/live" go={() => {}} onGroup={onGroup} base={`work/task/${ID}`} />,
    )
    await waitFor(() => expect(document.querySelector('.ag-head-title')?.textContent).toBe('fix-heartbeat'))
    expect(onGroup).not.toHaveBeenCalled()
  })
})

describe('QA12-26 through the router: /agents/live/<parked id>', () => {
  afterEach(() => {
    vi.unstubAllEnvs()
    vi.doUnmock('../api')
    window.history.replaceState(null, '', '/')
  })

  it('rewrites the address to Waiting and the back link says Waiting', async () => {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    vi.doUnmock('../api')
    const parked = agent('PARKED', { tenant_id: 'eng' })
    const routes: Record<string, unknown> = {
      '/v1/tasks': { tasks: [parked], next_page_token: null },
      [`/v1/tasks/${ID}`]: { task: parked },
      [`/v1/tasks/${ID}/events`]: { events: [] },
      [`/v1/tasks/${ID}/attempts`]: { attempts: [] },
      '/v1/resource-classes': { resource_classes: {} },
    }
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const answer = routes[new URL(String(input), 'http://ui.test').pathname]
      if (answer === undefined) return new Promise<Response>(() => {})
      return new Response(JSON.stringify(answer), { status: 200, headers: { 'content-type': 'application/json' } })
    }) as unknown as typeof fetch
    window.history.replaceState(null, '', `/agents/live/${ID}/details`)
    const { App } = await import('../App')
    render(<App />)
    await waitFor(() => expect(window.location.pathname).toBe(`/agents/waiting/${ID}`))
    const back = document.querySelector<HTMLElement>('.ctl-agent-back')
    expect(back?.textContent).toMatch(/Waiting/)
    expect(document.querySelector('.ctl-drawer')?.getAttribute('aria-label')).toBe(`Agent ${ID}`)
  })
})
