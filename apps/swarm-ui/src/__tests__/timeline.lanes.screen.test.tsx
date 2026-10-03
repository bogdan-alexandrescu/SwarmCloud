// THE LANES PAGE (timeline.html pick A), AS BEHAVIOUR.
//
// The api module is replaced with payloads in the routes' exact shapes:
// `/v1/attempts` for the bars, one `/v1/tasks/{id}/events` page per lane for
// the marks, the task list and the workflow read for names and steps, and
// `/v1/outcomes` for the one-line strip. Each case is a rule of the picked
// frame or of redesign-v2's honesty rules.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import { ledgerFixture } from '../outcomes.fixture'
import type { AttemptRow, AttemptsPage, Task, TaskEvent, TaskEventsPage, TaskPage } from '../types'
import type { WorkflowRead } from '../api'

const api = vi.hoisted(() => ({
  loadAttemptsPage: vi.fn(),
  loadTaskEventsPage: vi.fn(),
  loadTasks: vi.fn(),
  loadTask: vi.fn(),
  loadWorkflow: vi.fn(),
  loadOutcomes: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { TimelineLanesScreen, STRIP_DEADLINE_MS } = await import('../TimelineLanes')

// jsdom has no PointerEvent, and without one a pointer event carries no
// clientX: a MouseEvent with a pointerId is what a browser hands the axis.
if (typeof window.PointerEvent === 'undefined') {
  class PointerEventShim extends MouseEvent {
    pointerId: number
    constructor(type: string, init: PointerEventInit = {}) {
      super(type, init)
      this.pointerId = init.pointerId ?? 0
    }
  }
  window.PointerEvent = PointerEventShim as unknown as typeof PointerEvent
}

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })
const failed = <T,>(httpStatus: number, message: string): Result<T> => ({
  status: 'error',
  error: { kind: 'server', httpStatus, code: null, message } as never,
})

const NOW = Date.now()
const ago = (min: number) => new Date(NOW - min * 60_000).toISOString()

function task(id: string, extra: Partial<Task> = {}): Task {
  return {
    id, tenant_id: 'eng', state: 'SUCCEEDED', runner_profile: 'claude-code', resource_class: 'standard', provider: null,
    priority: 0, created_at: ago(300), updated_at: ago(100), started_at: ago(290), completed_at: ago(100),
    submitted_by: 'operator@example.com', attempt_count: 1, max_attempts: 3, park_reason: null, blocked_by: null,
    workflow_id: null, step_id: null, depends_on: null, cancel_requested: false, repository_url: null,
    current_generation: 1, current_lease_id: null, ...extra,
  } as Task
}

function attempt(taskId: string, gen: number, from: number, to: number | null, extra: Partial<AttemptRow> = {}): AttemptRow {
  return {
    attempt_id: `att_${taskId}_${gen}`, task_id: taskId, tenant_id: 'eng', generation: gen, lease_id: `l${gen}`,
    backend: 'cloud_run', execution_name: null, created_at: ago(from), started_at: ago(from - 1),
    completed_at: to === null ? null : ago(to), exit_code: to === null ? null : 0, error: null,
    peak_rss_bytes: null, peak_disk_bytes: null, oom_near_miss: false, checkpoints: [], input_tokens: null,
    output_tokens: null, cache_read_input_tokens: null, cache_creation_input_tokens: null, cost_usd: null, ...extra,
  } as AttemptRow
}

function ev(taskId: string, type: string, min: number, gen: number | null, detail: Record<string, unknown> | null = null): TaskEvent {
  return { event_id: `${taskId}-${type}-${min}`, task_id: taskId, type, at: ago(min), attempt_id: null, lease_id: null, generation: gen, detail }
}

const TASKS: Task[] = [
  task('task_solo', { attempt_count: 2, current_generation: 2 }),
  task('task_plan', { workflow_id: 'wf_7c1e', step_id: 'plan' }),
  task('task_test', { workflow_id: 'wf_7c1e', step_id: 'test', runner_profile: 'codex', state: 'FAILED' }),
]
const NEVER_RAN = task('task_publish', { workflow_id: 'wf_7c1e', step_id: 'publish', state: 'CANCELLED', attempt_count: 0, started_at: null })

const ATTEMPTS: AttemptRow[] = [
  attempt('task_test', 1, 150, 110, { exit_code: 1 }),
  attempt('task_solo', 2, 200, 100),
  attempt('task_plan', 1, 240, 160),
  attempt('task_solo', 1, 280, 210, { exit_code: 70 }),
]

function page(attempts: AttemptRow[], next: string | null = null): AttemptsPage {
  return { tenant_id: 'eng', read_at: new Date(NOW).toISOString(), attempts, next_page_token: next, coverage: { scope: 'page' } } as AttemptsPage
}

const EVENTS: Record<string, TaskEvent[]> = {
  task_solo: [ev('task_solo', 'succeeded', 100, 2), ev('task_solo', 'generation_fenced', 205, 2, { expected_generation: 1, observed_generation: 2 }), ev('task_solo', 'running', 279, 1)],
  task_plan: [ev('task_plan', 'cancel_requested', 170, 1), ev('task_plan', 'succeeded', 160, 1)],
  task_test: [ev('task_test', 'failed', 110, 1)],
}

function eventsPage(events: TaskEvent[], next: string | null = null): TaskEventsPage {
  return { events, next_page_token: next }
}

beforeEach(() => {
  api.loadAttemptsPage.mockResolvedValue(ok(page(ATTEMPTS)))
  api.loadTaskEventsPage.mockImplementation(async (id: string) => ok(eventsPage(EVENTS[id] ?? [])))
  api.loadTasks.mockResolvedValue(ok({ tasks: TASKS } as TaskPage))
  api.loadTask.mockImplementation(async (id: string) => failed(404, `no ${id}`))
  api.loadWorkflow.mockResolvedValue(ok({ workflow: { workflow_id: 'wf_7c1e', state: 'FAILED', steps: [{}, {}, {}] }, tasks: [TASKS[1]!, TASKS[2]!, NEVER_RAN] } as unknown as WorkflowRead))
  api.loadOutcomes.mockResolvedValue(ok(ledgerFixture()))
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const lanes = () => document.querySelectorAll('[data-lane]')
const lane = (id: string) => document.querySelector(`[data-lane="${id}"]`) as HTMLElement

describe('Lanes: the drawing', () => {
  it('reads /v1/attempts for the span and draws one lane per agent, grouped under its workflow, standalone last', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    // The never-ran step comes from the workflow read, which lands after the attempts.
    await waitFor(() => expect(lane('task_publish')).not.toBeNull())
    const args = api.loadAttemptsPage.mock.calls[0]![0] as { since: string; until: string }
    expect(Date.parse(args.until) - Date.parse(args.since)).toBe(24 * 3_600_000)
    const groups = [...document.querySelectorAll('[data-group]')].map((g) => g.getAttribute('data-group'))
    expect(groups).toEqual(['wf_7c1e', 'standalone'])
    expect([...lanes()].map((l) => l.getAttribute('data-lane'))).toEqual(['task_plan', 'task_test', 'task_publish', 'task_solo'])
  })

  it('draws a held attempt solid, a fenced generation cut and labelled, and a cancel request as a mark', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(lane('task_solo')).not.toBeNull())
    const solo = lane('task_solo')
    expect(solo.querySelectorAll('[data-seg="hold"]')).toHaveLength(1)
    const cut = solo.querySelector('[data-seg="cut"]')
    expect(cut).not.toBeNull()
    expect(solo.textContent).toContain('gen 1 fenced')
    await waitFor(() => expect(lane('task_plan').querySelector('[data-mark="cancel_requested"]')).not.toBeNull())
  })

  it('spans a held bar from its attempt start to its end at a visible width (Q6)', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(lane('task_plan').querySelector('.tl-mkr[data-mark="succeeded"]')).not.toBeNull())
    const plan = lane('task_plan')
    const bar = plan.querySelector<HTMLElement>('[data-seg="hold"]')!
    const left = parseFloat(bar.style.left)
    const width = parseFloat(bar.style.width)
    // 24h span ending now: the attempt ran from about 240m to 160m ago, so the
    // bar starts near 83% of the track and is about 80/1440 = 5.6% wide --
    // not a sliver at one column, which is what Q6 measured.
    expect(left).toBeGreaterThan(82)
    expect(left).toBeLessThan(84.5)
    expect(width).toBeGreaterThan(5)
    expect(width).toBeLessThan(6.2)
    // The terminal mark sits after a bar's end; a cancel request stays on its instant.
    expect(plan.querySelector('.tl-mkr[data-mark="succeeded"]')!.classList.contains('is-end')).toBe(true)
    expect(plan.querySelector('[data-mark="cancel_requested"]')!.classList.contains('is-end')).toBe(false)
  })

  it('fills a workflow step that never ran from the workflow read, and draws no bar for it', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(lane('task_publish')).not.toBeNull())
    expect(lane('task_publish').textContent).toContain('never ran')
    expect(lane('task_publish').querySelectorAll('[data-seg="hold"]')).toHaveLength(0)
  })

  it('reads one events page per lane, newest first', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(api.loadTaskEventsPage).toHaveBeenCalledTimes(3))
    const ids = api.loadTaskEventsPage.mock.calls.map((c) => c[0]).sort()
    expect(ids).toEqual(['task_plan', 'task_solo', 'task_test'])
    // newest first is the reader's (api.lanes.test.ts holds the route to order=desc)
  })

  it('says the filters run in this browser over the rows read, and how many pages are left is a dash with its reason', async () => {
    api.loadAttemptsPage.mockResolvedValue(ok(page(ATTEMPTS, 'next')))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(lanes().length).toBeGreaterThan(0))
    const cov = document.querySelector('.tl-cov') as HTMLElement
    expect(cov.textContent).toMatch(/filters run in this browser over the rows read/i)
    const partial = screen.getByRole('status', { name: /more attempts/i })
    expect(partial.textContent).toContain('—')
    expect(partial.textContent).toMatch(/page token, not a total/)
    fireEvent.click(within(partial).getByRole('button', { name: /read the next page/i }))
    await waitFor(() => expect(api.loadAttemptsPage).toHaveBeenCalledTimes(2))
    expect(api.loadAttemptsPage.mock.calls[1]![0].pageToken).toBe('next')
  })
})

describe('Lanes: a selected lane', () => {
  it('shows its summary and newest events below the chart, with Open in Agents to the split view', async () => {
    const onView = vi.fn()
    render(<TimelineLanesScreen view="lane=task_solo" onView={onView} />)
    const detail = await screen.findByRole('region', { name: /task_solo/ })
    const open = within(detail).getByRole('link', { name: /open in agents/i })
    expect(open.getAttribute('href')).toBe('/agents/recent/task_solo/attempts')
    await waitFor(() => expect(within(detail).getAllByRole('listitem').length).toBe(3))
    const items = within(detail).getAllByRole('listitem').map((li) => li.textContent ?? '')
    expect(items[0]).toContain('succeeded')
    expect(items[1]).toContain('generation_fenced')
  })

  it('follows the events page token for older events', async () => {
    api.loadTaskEventsPage.mockImplementation(async (id: string, o: { pageToken?: string }) =>
      o.pageToken === 'older'
        ? ok(eventsPage([ev(id, 'lease_acquired', 285, 1)]))
        : ok(eventsPage(EVENTS[id] ?? [], id === 'task_solo' ? 'older' : null)),
    )
    render(<TimelineLanesScreen view="lane=task_solo" onView={() => {}} />)
    const detail = await screen.findByRole('region', { name: /task_solo/ })
    fireEvent.click(await within(detail).findByRole('button', { name: /older events/i }))
    await waitFor(() => expect(within(detail).getAllByRole('listitem').length).toBe(4))
  })

  it('selects a lane on click, through the address', async () => {
    const onView = vi.fn()
    render(<TimelineLanesScreen view={null} onView={onView} />)
    await waitFor(() => expect(lane('task_plan')).not.toBeNull())
    fireEvent.click(within(lane('task_plan')).getByRole('button'))
    expect(onView).toHaveBeenCalledWith('lane=task_plan')
  })
})

describe('Lanes: every honest state', () => {
  it('loading: says what it is reading, and draws nothing', () => {
    api.loadAttemptsPage.mockReturnValue(new Promise(() => {}))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    // No route in the copy (walkthrough E): it says what it reads, in words.
    expect(screen.getByText(/Reading attempts for/)).toBeTruthy()
    expect(lanes()).toHaveLength(0)
  })

  it('error: names its status, and draws no bar under a failure', async () => {
    api.loadAttemptsPage.mockResolvedValue(failed(503, 'unavailable'))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('the read answered 503')
    expect(alert.textContent).toMatch(/Nothing is drawn, because nothing was read/)
    expect(lanes()).toHaveLength(0)
    fireEvent.click(within(alert).getByRole('button', { name: /try again/i }))
    await waitFor(() => expect(api.loadAttemptsPage).toHaveBeenCalledTimes(2))
  })

  it('empty: no attempt held capacity is not "nothing exists"', async () => {
    api.loadAttemptsPage.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    const empty = await screen.findByText(/No attempt held capacity between/)
    expect(empty.closest('.tl-state')?.textContent).toMatch(/Waiting work costs nothing and draws no bar/)
    expect(empty.closest('.tl-state')?.querySelector('a[href="/agents/waiting"]')).not.toBeNull()
  })

  it('filtered to nothing: says which filters, over how many attempts read, and clears them', async () => {
    const onView = vi.fn()
    render(<TimelineLanesScreen view="state=DEAD_LETTERED" onView={onView} />)
    const none = await screen.findByText(/No lane matches/)
    expect(none.textContent).toContain('4 attempts read')
    fireEvent.click(within(none.closest('.tl-state') as HTMLElement).getByRole('button', { name: /clear filters/i }))
    expect(onView).toHaveBeenCalledWith('')
  })

  it("one lane's events read failed: its marks are a dash with the reason, the bars stay", async () => {
    api.loadTaskEventsPage.mockImplementation(async (id: string) => (id === 'task_solo' ? failed(403, 'forbidden') : ok(eventsPage(EVENTS[id] ?? []))))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(lane('task_solo')?.textContent).toMatch(/marks: — this lane's events read failed \(403\)/i))
    expect(lane('task_solo').querySelectorAll('[data-seg="hold"]').length).toBeGreaterThan(0)
  })

  it('unknown task: a lane whose task could not be read is labelled by its id, with a dash for its profile', async () => {
    api.loadTasks.mockResolvedValue(ok({ tasks: [TASKS[1]!, TASKS[2]!] } as TaskPage))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(lane('task_solo')?.textContent).toMatch(/task_solo/))
    expect(lane('task_solo').textContent).toMatch(/profile —/)
  })
})

describe('Lanes: the outcome strip', () => {
  it('reads /v1/outcomes on its own, and a failure there leaves the lanes drawn', async () => {
    api.loadOutcomes.mockResolvedValue(failed(422, 'since is in the future'))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    const strip = await screen.findByText(/Outcomes not read/)
    expect(strip.closest('.tl-strip')?.textContent).toContain('422')
    expect(strip.closest('.tl-strip')?.textContent).toMatch(/lanes below are unaffected/)
    await waitFor(() => expect(lanes().length).toBeGreaterThan(0))
  })

  // VISUAL QA Q6 (2026-10-02): the strip said "Reading /v1/outcomes…" while
  // the lanes were drawn, and never moved. Every way the read can end -- no
  // answer, an empty answer, a thrown read -- resolves to words.
  // MUTATION: drop the deadline, or map `empty` back to loading.
  it('says the outcomes read had no answer after its deadline, instead of reading forever (Q6)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      api.loadOutcomes.mockReturnValue(new Promise(() => {}))
      render(<TimelineLanesScreen view={null} onView={() => {}} />)
      await waitFor(() => expect(lanes().length).toBeGreaterThan(0))
      expect(document.querySelector('.tl-strip')?.textContent).toMatch(/Reading outcomes…/)
      await act(async () => {
        vi.advanceTimersByTime(STRIP_DEADLINE_MS + 1)
      })
      const text = document.querySelector('.tl-strip')?.textContent ?? ''
      expect(text).not.toMatch(/Reading outcomes…/)
      expect(text).toMatch(/Outcomes not read/)
      expect(text).toMatch(/no answer after \d+ s/)
      expect(text).toMatch(/lanes below are unaffected/)
    } finally {
      vi.useRealTimers()
    }
  })

  it('says an empty or thrown outcomes read in words, never "Reading…" (Q6)', async () => {
    api.loadOutcomes.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    const a = render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(document.querySelector('.tl-strip')?.textContent).toMatch(/Outcomes not read/))
    expect(document.querySelector('.tl-strip')?.textContent).toMatch(/answered with nothing/)
    a.unmount()
    api.loadOutcomes.mockRejectedValue(new Error('boom'))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(document.querySelector('.tl-strip')?.textContent).toMatch(/Outcomes not read/))
    expect(document.querySelector('.tl-strip')?.textContent).toMatch(/boom/)
  })

  it('prints the success rate from the route, and fences from the lanes drawn', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(document.querySelector('.tl-strip')?.textContent).toMatch(/% success/))
    await waitFor(() => expect(document.querySelector('.tl-strip')?.textContent).toMatch(/1 generation fenced/))
    const link = within(document.querySelector('.tl-strip') as HTMLElement).getByRole('link', { name: /outcomes for this span/i })
    expect(link.getAttribute('href')).toBe('/timeline/outcomes?span=24h')
  })

  it('prints no settled fence count while lanes are unread: a floor, and the lanes it could not read', async () => {
    api.loadTaskEventsPage.mockImplementation(() => new Promise(() => {}))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(lanes().length).toBeGreaterThan(0))
    await waitFor(() => expect(document.querySelector('.tl-strip')?.textContent).toMatch(/1\+ generations fenced, events not read on 3 lanes/))
  })

  it('suggests Outcomes past 7 days', async () => {
    render(<TimelineLanesScreen view="span=30d" onView={() => {}} />)
    expect(await screen.findByText(/bars are slivers/i)).toBeTruthy()
  })
})

describe('Lanes: refresh', () => {
  it('a refresh reads every lane in view again, and draws its marks again', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(lane('task_plan')?.querySelector('[data-mark="cancel_requested"]')).not.toBeNull())
    const asked = (id: string) => api.loadTaskEventsPage.mock.calls.filter((c) => c[0] === id && (c[1] as { order?: string }).order === 'desc' && !(c[1] as { pageToken?: string }).pageToken).length
    expect(asked('task_plan')).toBe(1)
    expect(asked('task_solo')).toBe(1)
    fireEvent.click(screen.getByRole('button', { name: 'refresh' }))
    await waitFor(() => expect(asked('task_plan')).toBe(2))
    await waitFor(() => expect(asked('task_solo')).toBe(2))
    expect(asked('task_test')).toBe(2)
    await waitFor(() => expect(lane('task_plan').querySelector('[data-mark="cancel_requested"]')).not.toBeNull())
    await waitFor(() => expect(document.querySelector('.tl-strip')?.textContent).toMatch(/1 generation fenced/))
    expect(document.querySelector('.tl-strip')?.textContent).not.toMatch(/events not read/)
  })

  it('drops an events page that lands after a newer read began', async () => {
    let release: (() => void) | null = null
    api.loadTaskEventsPage.mockImplementation((id: string) =>
      id === 'task_plan' && release === null
        ? new Promise((res) => {
            release = () => res(ok(eventsPage([ev('task_plan', 'cancel_requested', 170, 1)])))
          })
        : Promise.resolve(ok(eventsPage(id === 'task_plan' ? [] : (EVENTS[id] ?? [])))),
    )
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(release).not.toBeNull())
    fireEvent.click(await screen.findByRole('button', { name: 'refresh' }))
    await waitFor(() => expect(api.loadTaskEventsPage.mock.calls.filter((c) => c[0] === 'task_plan').length).toBe(2))
    release!()
    await new Promise((r) => setTimeout(r, 20))
    expect(lane('task_plan').querySelector('[data-mark="cancel_requested"]')).toBeNull()
  })
})

describe('Lanes: span and zoom', () => {
  it('a drag on the axis zooms to since/until with a way back', async () => {
    const onView = vi.fn()
    render(<TimelineLanesScreen view={null} onView={onView} />)
    await waitFor(() => expect(lanes().length).toBeGreaterThan(0))
    const axis = document.querySelector('.tl-axis .tl-trk') as HTMLElement
    axis.getBoundingClientRect = () => ({ left: 0, width: 1000, top: 0, height: 30, right: 1000, bottom: 30, x: 0, y: 0, toJSON: () => ({}) })
    fireEvent.pointerDown(axis, { clientX: 250, button: 0, pointerId: 1 })
    fireEvent.pointerMove(axis, { clientX: 500, pointerId: 1 })
    fireEvent.pointerUp(axis, { clientX: 500, pointerId: 1 })
    expect(onView).toHaveBeenCalled()
    const q = new URLSearchParams(onView.mock.calls.at(-1)![0] as string)
    expect(q.get('since')).not.toBeNull()
    expect(q.get('until')).not.toBeNull()
    expect(Date.parse(q.get('until')!) - Date.parse(q.get('since')!)).toBeCloseTo(6 * 3_600_000, -3)
    expect(q.has('back')).toBe(true)
  })

  it('a zoomed view shows the back chip to the span it came from', async () => {
    const onView = vi.fn()
    const since = new Date(NOW - 6 * 3_600_000).toISOString()
    const until = new Date(NOW - 3_600_000).toISOString()
    render(<TimelineLanesScreen view={`since=${encodeURIComponent(since)}&until=${encodeURIComponent(until)}&back=span%3D7d`} onView={onView} />)
    fireEvent.click(await screen.findByRole('button', { name: /← 7d/ }))
    expect(onView).toHaveBeenCalledWith('span=7d')
  })

  it('offers the two pages, Lanes current', () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    const nav = screen.getByRole('navigation', { name: /timeline pages/i })
    expect(within(nav).getByRole('link', { name: 'Lanes' }).getAttribute('aria-current')).toBe('page')
    expect(within(nav).getByRole('link', { name: 'Outcomes' }).getAttribute('href')).toBe('/timeline/outcomes')
  })
})

describe('walkthrough G: a lane label leads with its name, state and duration', () => {
  it('draws `<step> <mark> · <duration>`, with the task id in the tooltip and nowhere in the label or its note', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    const row = await waitFor(() => {
      const r = document.querySelector<HTMLElement>('.tl-row[data-lane="task_plan"]')
      expect(r?.querySelector('.tl-dur')).toBeTruthy()
      return r!
    })
    const lab = row.querySelector('.tl-lab b')!
    const name = lab.querySelector<HTMLElement>('.tl-name')!
    expect(name.textContent).toBe('plan')
    expect(name.getAttribute('title')).toBe('plan · task_plan')
    // The name, then the state mark, then the time its attempt held capacity
    // (240 → 160 minutes ago: 1h 20m).
    const order = [...lab.children].map((c) => (c.classList.contains('tl-name') ? 'name' : c.classList.contains('tl-dur') ? 'dur' : c.hasAttribute('data-mark') || c.querySelector('[data-mark]') ? 'mark' : c.className))
    expect(order).toEqual(['name', 'mark', 'dur'])
    expect(lab.querySelector('.tl-dur')!.textContent).toBe(' · 1h 20m')
    expect(row.querySelector('.tl-lab small')!.textContent).not.toContain('task_plan')
    // A lone task is named from what it is, not by its id.
    const solo = document.querySelector<HTMLElement>('.tl-row[data-lane="task_solo"] .tl-name')!
    expect(solo.textContent).toBe('claude-code task · solo')
    // A lane that never ran has no duration: there is nothing it held.
    const never = document.querySelector<HTMLElement>('.tl-row[data-lane="task_publish"]')
    if (never !== null) expect(never.querySelector('.tl-dur')).toBeNull()
  })
})

