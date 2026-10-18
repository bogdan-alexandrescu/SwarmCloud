// THE TIMELINE'S READS ON LOAD (live QA CH-6, 2026-10-11): 66 API reads at
// 1440 -- 52 × `GET /v1/workflows/<id>`, one per workflow its lanes name, and
// 25 × `GET /v1/tasks/<id>/events`, one per lane near the screen.
//
// MEASURED THROUGH A STUBBED fetch: the screen and the real api module, every
// request counted by route. Ten workflows of one lane each.
//
//   before: GET /v1/workflows/{id} × 10, and an events read per lane in view
//   after:  GET /v1/workflows?ids= × 1 (50 ids to a read), and an events
//           read only for a lane on screen (LANE_ROOT_MARGIN, 0px)
//
// MUTATIONS: read each workflow with `loadWorkflow` again (the first case
// counts 10 single reads); put the lanes back on `CARD_ROOT_MARGIN` (the
// second case sees a 200px margin).

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, waitFor } from '@testing-library/react'

import { attempt, task as baseTask } from './runfixture'

const NOW = Date.now()
const ago = (min: number) => new Date(NOW - min * 60_000).toISOString()
const WFS = Array.from({ length: 10 }, (_, i) => `wf_${String(i).padStart(4, '0')}`)
const TASKS = WFS.map((wf, i) =>
  baseTask({
    id: `task_${i}`, tenant_id: 'eng', state: 'SUCCEEDED', workflow_id: wf, step_id: 'plan',
    created_at: ago(300), started_at: ago(290), completed_at: ago(100), updated_at: ago(100),
  }),
)

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

function workflowRow(wf: string, taskId: string) {
  return {
    workflow_id: wf, tenant_id: 'eng', state: 'SUCCEEDED', stored_state: 'SUCCEEDED', state_source: 'derived',
    created_at: ago(300), updated_at: ago(100), submitted_by: 'operator@example.com', priority: 0,
    on_step_failure: 'FAIL_WORKFLOW', cancel_requested: false,
    steps: [{ step_id: 'plan', runner_profile: 'mock', resource_class: 'standard', depends_on: [], input_from: {}, task_id: taskId }],
  }
}

/** Every GET by route; the workflow reads by `ids` asked. */
function stub(): { counts: Map<string, number>; idsAsked: string[][] } {
  const counts = new Map<string, number>()
  const idsAsked: string[][] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), 'http://ui.test')
    const ids = url.searchParams.getAll('ids')
    const key = url.pathname.startsWith('/v1/workflows/') ? '/v1/workflows/{id}' : url.pathname.startsWith('/v1/tasks/') && url.pathname.endsWith('/events') ? '/v1/tasks/{id}/events' : url.pathname
    counts.set(key, (counts.get(key) ?? 0) + 1)
    if (url.pathname === '/v1/attempts') {
      return json({
        tenant_id: 'eng', read_at: new Date(NOW).toISOString(), next_page_token: null, coverage: { scope: 'page' },
        attempts: TASKS.map((t, i) => attempt(i + 1, { attempt_id: `att_${i}`, task_id: t.id, tenant_id: 'eng', created_at: ago(200 - i), started_at: ago(199 - i), completed_at: ago(150 - i) })),
      })
    }
    if (url.pathname === '/v1/tasks') return json({ tasks: TASKS, next_page_token: null, tenant_id: 'eng', view: 'summary' })
    if (url.pathname === '/v1/workflows' && ids.length > 0) {
      idsAsked.push(ids)
      const rows = ids.map((wf) => workflowRow(wf, TASKS[WFS.indexOf(wf)]!.id))
      return json({ workflows: rows, tasks: ids.map((wf) => TASKS[WFS.indexOf(wf)]), missing: [], next_page_token: null, tenant_id: 'eng' })
    }
    if (url.pathname.startsWith('/v1/workflows/')) {
      const wf = url.pathname.split('/').pop()!
      return json({ workflow: workflowRow(wf, TASKS[WFS.indexOf(wf)]!.id), tasks: [TASKS[WFS.indexOf(wf)]], dispatch: {} })
    }
    if (url.pathname.endsWith('/events')) return json({ events: [], next_page_token: null })
    return json({ code: 'not_found', message: 'not in this stub' }, 404)
  }) as unknown as typeof fetch
  return { counts, idsAsked }
}

async function mount(): Promise<void> {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { TimelineLanesScreen } = await import('../TimelineLanes')
  render(<TimelineLanesScreen view={null} onView={() => {}} />)
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
})

describe('CH-6: the Timeline reads its workflows in one read, not one each', () => {
  it('asks for the ten workflows its lanes name with one GET /v1/workflows?ids=', async () => {
    const { counts, idsAsked } = stub()
    await mount()
    await waitFor(() => expect(document.querySelectorAll('.tl-row.is-group').length).toBe(WFS.length))
    await waitFor(() => expect(document.querySelector(`.tl-row.is-group[data-group="${WFS[0]}"] small`)?.textContent).toBe('1 step · succeeded'))
    console.info('CH-6 timeline GETs', JSON.stringify([...counts].sort()))
    expect(counts.get('/v1/workflows/{id}') ?? 0, 'one read per workflow').toBe(0)
    expect(counts.get('/v1/workflows')).toBe(1)
    expect([...idsAsked[0]!].sort()).toEqual(WFS)
  })
})

describe('CH-6: a lane reads its events only once it is on screen', () => {
  class Observer {
    static all: Observer[] = []
    constructor(
      readonly callback: IntersectionObserverCallback,
      readonly options: IntersectionObserverInit = {},
    ) {
      Observer.all.push(this)
    }
    readonly targets = new Set<Element>()
    observe(el: Element): void {
      this.targets.add(el)
    }
    unobserve(el: Element): void {
      this.targets.delete(el)
    }
    disconnect(): void {
      this.targets.clear()
    }
    takeRecords(): IntersectionObserverEntry[] {
      return []
    }
  }

  it('watches lanes with no margin, and reads events for the lanes reported in view and no other', async () => {
    Observer.all = []
    vi.stubGlobal('IntersectionObserver', Observer)
    const { counts } = stub()
    await mount()
    await waitFor(() => expect(document.querySelectorAll('.tl-row[data-lane]').length).toBeGreaterThanOrEqual(WFS.length))
    const lanes = [...document.querySelectorAll<HTMLElement>('.tl-row[data-lane]')]
    const watching = (el: Element) => Observer.all.filter((o) => o.targets.has(el))
    await waitFor(() => expect(watching(lanes[0]!).length).toBeGreaterThan(0))
    for (const o of watching(lanes[0]!)) expect(o.options.rootMargin).toBe('0px')
    // Nothing reported in view yet: no lane has read its events.
    expect(counts.get('/v1/tasks/{id}/events') ?? 0).toBe(0)
    // Three lanes scroll on screen.
    act(() => {
      for (const el of lanes.slice(0, 3)) {
        for (const o of watching(el)) o.callback([{ target: el, isIntersecting: true } as unknown as IntersectionObserverEntry], o as unknown as IntersectionObserver)
      }
    })
    await waitFor(() => expect(counts.get('/v1/tasks/{id}/events')).toBe(3))
  })
})
