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
import { landed } from './reads'
import { painted } from './marks'
import { byIdVia } from './workflowsById'

const api = vi.hoisted(() => ({
  loadAttemptsPage: vi.fn(),
  loadTaskEventsPage: vi.fn(),
  loadTasks: vi.fn(),
  loadTask: vi.fn(),
  loadWorkflow: vi.fn(),
  loadWorkflowsById: vi.fn(),
  loadOutcomes: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { TimelineLanesScreen, STRIP_DEADLINE_MS, MARK_GAP_PX, MARK_PX, SPAN_PAGES_READ, BAR_MIN_PX, BAR_WORDS_MIN_PX, PILL_COUNT_MIN_PX, laneLayout, markLefts, sliversNote } = await import('../TimelineLanes')

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
  api.loadWorkflowsById.mockImplementation(byIdVia(api.loadWorkflow))
  api.loadAttemptsPage.mockResolvedValue(ok(page(ATTEMPTS)))
  api.loadTaskEventsPage.mockImplementation(async (id: string) => ok(eventsPage(EVENTS[id] ?? [])))
  api.loadTasks.mockResolvedValue(ok({ tasks: TASKS } as TaskPage))
  api.loadTask.mockImplementation(async (id: string) => failed(404, `no ${id}`))
  api.loadWorkflow.mockResolvedValue(ok({ workflow: { workflow_id: 'wf_7c1e', state: 'FAILED', steps: [{}, {}, {}] }, tasks: [TASKS[1]!, TASKS[2]!, NEVER_RAN] } as unknown as WorkflowRead))
  // A ledger over the 24 h span: since QA2-FIX-05 (runs2-04) one over
  // another window shows no figures.
  api.loadOutcomes.mockResolvedValue(ok({ ...ledgerFixture(), tz: 'UTC', bucket: 'hour', since: new Date(Date.now() - 86_400_000).toISOString(), until: new Date().toISOString() }))
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
    // VOCAB-AGENTS (QA-109, part of #1680): the standalone block counts agents.
    // MUTATION: put back `'task' : 'tasks'` in the standalone block's note.
    expect(document.querySelector('[data-group="standalone"] .tl-lab small')?.textContent).toBe('1 agent in this span')
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
    // The bar is the attempts read's and the marks are task_plan's events
    // page, read once per lane after the attempts land: wait on those reads,
    // then look once (#605: this wait went red on a loaded runner).
    await landed(api.loadAttemptsPage)
    await landed(api.loadTaskEventsPage, 3)
    expect(lane('task_plan').querySelector('.tl-mkr[data-mark="succeeded"]')).not.toBeNull()
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
    // The read follows the token on its own up to SPAN_PAGES_READ (live QA
    // RT-1); past that the button goes on by hand.
    await waitFor(() => expect(api.loadAttemptsPage).toHaveBeenCalledTimes(SPAN_PAGES_READ))
    await waitFor(() => expect(within(partial).getByRole('button', { name: /read the next page/i }).hasAttribute('disabled')).toBe(false))
    fireEvent.click(within(partial).getByRole('button', { name: /read the next page/i }))
    await waitFor(() => expect(api.loadAttemptsPage).toHaveBeenCalledTimes(SPAN_PAGES_READ + 1))
    expect(api.loadAttemptsPage.mock.calls[SPAN_PAGES_READ]![0].pageToken).toBe('next')
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
    // In words, as Agents' events say it (QA1-FIX-28, runs-submit-38).
    expect(items[1]).toContain('generation fenced')
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
    fireEvent.click(lane('task_plan').querySelector('.tl-name')!)
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
    // VOCAB-AGENTS (QA-109, part of #1680): the block of unread lanes is headed by the Agents page's word.
    // MUTATION: put back `title = 'Task not read'` in GroupHead.
    expect(document.querySelector('[data-group="unread"] .tl-lab b')?.textContent).toBe('Agent not read')
  })
})

describe('Lanes: the outcome strip', () => {
  it('says no agent was decided when the span has no rate (VOCAB-AGENTS, QA-109)', async () => {
    const d = { ...ledgerFixture(), tz: 'UTC', bucket: 'hour' as const, since: new Date(Date.now() - 86_400_000).toISOString(), until: new Date().toISOString() }
    d.totals = { ...d.totals, rate: null }
    api.loadOutcomes.mockResolvedValue(ok(d))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    // MUTATION: put back `no task was decided in this span` in OutcomeStrip.
    await waitFor(() => expect(document.querySelector('.tl-strip1')?.textContent).toContain('success: no agent was decided in this span'))
  })

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

  // #123: the strip printed a rate, a failed count and a cancelled count with
  // no total, so the reader had to add them up to know what they were parts of.
  // MUTATION: drop the finished figure, or print FAILED without DEAD_LETTERED.
  it('splits the span’s finished tasks into succeeded, failed and cancelled, which add up to it (#123)', async () => {
    const d = { ...ledgerFixture(), tz: 'UTC', bucket: 'hour' as const, since: new Date(Date.now() - 86_400_000).toISOString(), until: new Date().toISOString() }
    d.totals = { ...d.totals, failed: d.totals.failed - 3, dead_lettered: 3 }
    api.loadOutcomes.mockResolvedValue(ok(d))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(document.querySelector('.tl-strip .tl-ended')).not.toBeNull())
    const ended = document.querySelector('.tl-strip .tl-ended') as HTMLElement
    const fig = (cls: string) => Number(ended.querySelector(`.${cls}`)?.textContent ?? 'NaN')
    expect(fig('tl-n-ended')).toBe(d.totals.ended)
    expect(fig('tl-n-ok')).toBe(d.totals.succeeded)
    expect(fig('tl-n-bad'), 'failed is FAILED + DEAD_LETTERED').toBe(d.totals.failed + d.totals.dead_lettered)
    expect(fig('tl-n-cancelled')).toBe(d.totals.cancelled.total)
    expect(fig('tl-n-ok') + fig('tl-n-bad') + fig('tl-n-cancelled')).toBe(fig('tl-n-ended'))
    expect(ended.textContent).toMatch(/finished: \d+ succeeded · \d+ failed · \d+ cancelled/)
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
    await waitFor(() => expect(document.querySelector('.tl-strip')?.textContent).toMatch(/1\+ generations fenced, counting…/))
    // While the reads are out it says counting, not that they failed (QA1-FIX-28, runs-submit-33).
    expect(document.querySelector('.tl-strip')?.textContent).not.toMatch(/events not read/)
  })

  it('suggests Outcomes past 7 days', async () => {
    render(<TimelineLanesScreen view="span=30d" onView={() => {}} />)
    // QA1 runs-submit-34. MUTATION: restore `Past ${SUGGEST_OUTCOMES_DAYS} days` in TimelineLanes.tsx.
    expect(await screen.findByText(/^Over 30 days the bars are slivers/)).toBeTruthy()
    expect(screen.queryByText(/Past 7 days/)).toBeNull()
  })

  it('floors a zoomed span in the slivers note, so it never claims more days than are drawn', () => {
    // MUTATION: round instead of floor and 7.6 days reads "Over 8 days".
    expect(sliversNote(7.6)).toBe('Over 7 days the bars are slivers: counts over the span read better as a ledger.')
    expect(sliversNote(30)).toMatch(/^Over 30 days /)
  })
})

describe('Lanes: refresh', () => {
  it('a refresh reads every unfinished lane in view again, keeps an ended lane\'s events, and draws its marks again', async () => {
    // task_solo is still running; task_plan and task_test have ended, and an
    // ended task writes no more events (live QA RT-7b).
    api.loadTasks.mockResolvedValue(ok({ tasks: [task('task_solo', { attempt_count: 2, current_generation: 2, state: 'RUNNING', completed_at: null }), TASKS[1]!, TASKS[2]!] } as TaskPage))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(lane('task_plan')?.querySelector('[data-mark="cancel_requested"]')).not.toBeNull())
    const asked = (id: string) => api.loadTaskEventsPage.mock.calls.filter((c) => c[0] === id && (c[1] as { order?: string }).order === 'desc' && !(c[1] as { pageToken?: string }).pageToken).length
    await waitFor(() => expect(asked('task_solo')).toBe(1))
    expect(asked('task_plan')).toBe(1)
    fireEvent.click(screen.getByRole('button', { name: /(^|· )refresh\b/i }))
    await waitFor(() => expect(asked('task_solo')).toBe(2))
    await waitFor(() => expect(api.loadAttemptsPage).toHaveBeenCalledTimes(2))
    expect(asked('task_plan'), 'an ended lane is not asked again').toBe(1)
    expect(asked('task_test'), 'an ended lane is not asked again').toBe(1)
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
    fireEvent.click(await screen.findByRole('button', { name: /(^|· )refresh\b/i }))
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
    // Its profile in full, not 'claude-code agent' (QA2-FIX-05, runs2-27).
    expect(solo.textContent).toBe('claude-code · solo')
    // A lane that never ran has no duration: there is nothing it held.
    const never = document.querySelector<HTMLElement>('.tl-row[data-lane="task_publish"]')
    if (never !== null) expect(never.querySelector('.tl-dur')).toBeNull()
  })
})


// VQA-L13 (#1038), the Lanes half.
describe('VQA-L13: the marks at now, the filter row and the counts strip', () => {
  // V039: an end mark starts 2px after its instant and the track clips, so a
  // task that ended at `now` drew its mark past the edge, and two ending a
  // minute apart drew one disc over the other.
  // MUTATION: return the instants unchanged from `markLefts`.
  it('V039: keeps every mark inside the track at now, and two marks at now apart', () => {
    const px = 600
    const disc = (m: { pct: number; end: boolean }, left: number) => {
      const start = (left / 100) * px + (m.end ? 2 : -MARK_PX / 2)
      return [start, start + MARK_PX] as const
    }
    const marks = [
      { pct: 99.9, end: true },
      { pct: 100, end: true },
      { pct: 40, end: true },
      { pct: 99.95, end: false },
    ]
    const lefts = markLefts(marks, px)
    const boxes = marks.map((m, i) => disc(m, lefts[i]!)).sort((a, b) => a[0] - b[0])
    for (const [from, to] of boxes) {
      expect(from).toBeGreaterThanOrEqual(0)
      expect(to, 'a mark is clipped by the track’s right edge').toBeLessThanOrEqual(px)
    }
    for (let i = 1; i < boxes.length; i++) {
      expect(boxes[i]![0] - boxes[i - 1]![1], 'two marks at now are drawn as one').toBeGreaterThanOrEqual(MARK_GAP_PX - 1e-9)
    }
    // A mark with room keeps its instant, and an unmeasured track moves nothing.
    expect(lefts[2]).toBe(40)
    expect(markLefts(marks, null)).toEqual(marks.map((m) => m.pct))
  })

  // V104: the workflow select was as wide as its longest option, so it grew
  // when the rows landed and pushed Group by onto a second line.
  // MUTATION: drop `.tl-wf-sel`'s width, or the class from the select.
  it('V104: gives the workflow select a width of its own, so the rows landing cannot widen it', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await landed(api.loadAttemptsPage)
    const sel = document.querySelector('.tl-ctl select.tl-wf-sel')
    expect(sel, 'the workflow select carries no fixed width').not.toBeNull()
    for (const width of [400, 1440]) {
      expect(painted(sel!, 'width', { width }) ?? '', `${width}px: the select sizes to its options`).toMatch(/^\d+(\.\d+)?(em|px)$/)
    }
    // A picker's count ("all 0" -> "all 12") keeps a floor.
    const count = document.querySelector('.tl-ctl .tl-pick i')!
    expect(painted(count, 'min-width', { width: 400 }) ?? '').toMatch(/^\d+(\.\d+)?em$/)
  })

  // V105: the words between the figures were bare flex items, so at 400px
  // "succeeded ·" wrapped inside itself and left each "·" alone on a line.
  // MUTATION: put the parts back as bare text, or drop `.tl-xp`'s nowrap.
  it('V105: keeps each separator on the line of the figure before it, and wraps only between parts', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(document.querySelector('.tl-strip .tl-ended')).not.toBeNull())
    const ended = document.querySelector('.tl-strip .tl-ended') as HTMLElement
    expect(painted(ended, 'flex-wrap', { width: 400 })).toBe('wrap')
    // No bare text beside the parts: everything the strip prints is in one.
    for (const n of ended.childNodes) {
      if (n.nodeType === Node.TEXT_NODE) expect(n.textContent?.trim(), 'a bare word can wrap inside itself').toBe('')
      else expect((n as Element).classList.contains('tl-xp')).toBe(true)
    }
    const parts = [...ended.querySelectorAll<HTMLElement>(':scope > .tl-xp')]
    expect(parts).toHaveLength(4)
    for (const p of parts) {
      expect(painted(p, 'white-space', { width: 400 })).toBe('nowrap')
      expect(p.querySelector('b'), `"${p.textContent}" has no figure`).not.toBeNull()
    }
    // Every separator ends a part: none can start a line or stand on its own.
    expect(parts.filter((p) => p.textContent!.includes('·')).map((p) => /^\S.*\S ·$/.test(p.textContent!))).toEqual([true, true])
  })
})


// VQA2-R9 (#1427): the Timeline lanes at 1440.
describe('VQA2-R9: the bars at now and the standalone lane names', () => {
  // V039: the marks were kept inside the track and apart, but the bars were
  // not: three attempts in the last half hour of a 24h span painted three
  // floor-width bars over each other, past the track's right edge and under
  // the end mark.
  // MUTATION: draw the bars at their instants (drop `box` from `Seg`), put
  // `.tl-seg.is-hold`'s min-width back to 8px, or print words at any width.
  it('V039: draws a lane’s last attempts inside the track at now, apart from each other and from the end mark', async () => {
    const px = 600
    const realRect = Element.prototype.getBoundingClientRect
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
      return this.classList.contains('tl-trk') ? ({ ...realRect.call(this), width: px } as DOMRect) : realRect.call(this)
    })
    const hadObserver = 'ResizeObserver' in globalThis
    const RealObserver = globalThis.ResizeObserver
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver
    try {
      api.loadTasks.mockResolvedValue(ok({ tasks: [task('task_solo', { attempt_count: 3, current_generation: 3, completed_at: ago(2) })] } as TaskPage))
      api.loadAttemptsPage.mockResolvedValue(
        ok(page([attempt('task_solo', 3, 9, 2), attempt('task_solo', 2, 20, 14, { exit_code: 1 }), attempt('task_solo', 1, 30, 25, { exit_code: 1 })])),
      )
      api.loadTaskEventsPage.mockImplementation(async () => ok(eventsPage([ev('task_solo', 'succeeded', 2, 3)])))
      render(<TimelineLanesScreen view={null} onView={() => {}} />)
      const row = await waitFor(() => {
        const r = lane('task_solo')
        // QAFIX4-TIMELINE-2 (c00): three attempts this close at 24 h are ONE
        // counted pill now, not three floor-width pills packed at 'now'.
        // MUTATION: draw one pill per attempt again and this is 3.
        expect(r?.querySelectorAll('.tl-seg.is-hold')).toHaveLength(1)
        expect(r?.querySelector('.tl-mkr.is-end')).not.toBeNull()
        return r!
      })
      // The floor the layout takes is the floor that is painted: a merged
      // pill's, which is wider than a lone pill's to hold its count.
      // MUTATION: lay a merged pill out at `BAR_MIN_PX` while it paints wider.
      const bar = row.querySelector('.tl-seg.is-hold')!
      expect(painted(bar, 'min-width', { width: 1440 }), 'the CSS floor is not the layout’s').toBe(`${PILL_COUNT_MIN_PX}px`)
      const pctOf = (v: string) => Number(/^(-?[\d.e+-]+)%$/.exec(v)![1])
      const boxes: { what: string; from: number; to: number }[] = []
      for (const seg of row.querySelectorAll<HTMLElement>('.tl-seg.is-hold')) {
        const from = (pctOf(seg.style.left) / 100) * px
        const width = Math.max((pctOf(seg.style.width) / 100) * px, PILL_COUNT_MIN_PX)
        boxes.push({ what: seg.title, from, to: from + width })
        // A floor-width bar has no room for `gen 2`: its clipped first letter
        // read as a glyph. Its title says it.
        if (width < BAR_WORDS_MIN_PX) expect(seg.querySelector('.tl-seg-t'), `${seg.title} prints a clipped word`).toBeNull()
        // MUTATION: title the merged pill by its newest attempt alone.
        expect(seg.title).toMatch(/^3 attempts: gen 1 /)
      }
      const mark = row.querySelector<HTMLElement>('.tl-mkr.is-end')!
      const markFrom = (pctOf(mark.style.left) / 100) * px + 2
      boxes.push({ what: mark.title, from: markFrom, to: markFrom + MARK_PX })
      boxes.sort((a, b) => a.from - b.from)
      for (const b of boxes) {
        expect(b.from, `${b.what} starts before the track`).toBeGreaterThanOrEqual(0)
        expect(b.to, `${b.what} is cut by the track’s right edge`).toBeLessThanOrEqual(px + 1e-9)
      }
      for (let i = 1; i < boxes.length; i++) {
        expect(boxes[i]!.from - boxes[i - 1]!.to, `${boxes[i - 1]!.what} and ${boxes[i]!.what} are drawn as one`).toBeGreaterThanOrEqual(MARK_GAP_PX - 1e-9)
      }
    } finally {
      if (hadObserver) globalThis.ResizeObserver = RealObserver
      else delete (globalThis as { ResizeObserver?: unknown }).ResizeObserver
    }
  })

  // MUTATION: move a long bar rather than trim it (`left = right - width`).
  it('V039: trims a long bar at its end and keeps its start; moves only what has to move; unmeasured, the instants', () => {
    const px = 1000
    const out = laneLayout(
      [
        { from: 60, to: 99.9 },
        { from: 30, to: 30.1 },
      ],
      [{ pct: 99.9, end: true }],
      px,
    )
    expect(out.bars[0]!.left, 'a long bar’s start is its instant').toBeCloseTo(60, 9)
    expect((out.marks[0]! / 100) * px + 2 + MARK_PX).toBeLessThanOrEqual(px + 1e-9)
    expect(((out.bars[0]!.left + out.bars[0]!.width) / 100) * px).toBeLessThanOrEqual((out.marks[0]! / 100) * px + 2 - MARK_GAP_PX + 1e-9)
    // A short bar with room keeps its instant and takes the painted floor.
    expect(out.bars[1]!.left).toBeCloseTo(30, 9)
    expect((out.bars[1]!.width / 100) * px).toBeCloseTo(BAR_MIN_PX, 9)
    // A mid-lane mark (a cancel request) is drawn over its bar and moves no bar.
    const mid = laneLayout([{ from: 40, to: 60 }], [{ pct: 50, end: false }], px)
    expect(mid.bars[0]).toEqual({ left: 40, width: 20 })
    expect(mid.marks[0]).toBeCloseTo(50, 9)
    expect(laneLayout([{ from: 99, to: 99.5 }], [{ pct: 99.5, end: true }], null)).toEqual({ bars: [{ left: 99, width: 0.5 }], marks: [99.5] })
  })

  // V212: `claude-code agent · a073aff5` was ellipsized from its end, so the
  // short id -- the only part that tells two standalone lanes apart -- was
  // the part cut: "claude-code agent · a0…".
  // MUTATION: render the name as one text node, or drop `.tl-name-id`'s flex: none.
  it('V212: keeps a standalone lane’s short id whole and lets the kind give way', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(lane('task_solo')?.querySelector('.tl-name')).toBeTruthy())
    const name = lane('task_solo').querySelector<HTMLElement>('.tl-name')!
    // Its profile in full, not 'claude-code agent' (QA2-FIX-05, runs2-27).
    expect(name.textContent).toBe('claude-code · solo')
    const kind = name.querySelector('.tl-name-k')
    const id = name.querySelector('.tl-name-id')
    expect(kind?.textContent).toBe('claude-code')
    expect(id?.textContent, 'the id is not a part of its own').toBe(' · solo')
    expect(painted(name, 'display', { width: 1440 })).toBe('inline-flex')
    expect(painted(id!, 'flex', { width: 1440 }), 'the id can shrink').toBe('none')
    expect(painted(id!, 'white-space', { width: 1440 })).toBe('pre')
    expect(painted(kind!, 'text-overflow', { width: 1440 })).toBe('ellipsis')
    expect(painted(kind!, 'overflow', { width: 1440 })).toBe('hidden')
    expect(painted(kind!, 'min-width', { width: 1440 })).toBe('0')
    // A workflow step is named by its step alone: nothing to split.
    const step = lane('task_plan').querySelector<HTMLElement>('.tl-name')!
    expect(step.querySelector('.tl-name-id')).toBeNull()
    expect(step.textContent).toBe('plan')
  })
})

// VISUAL QA, WORK › TIMELINE › LANES (VQ-TIMELINE; part of #2391).
describe('VQ-025: a workflow group row names the workflow, its id and steps, and brackets its span', () => {
  const named = () =>
    TASKS.map((t) => (t.id === 'task_plan' ? { ...t, metadata: { title: 'refactor-broker' } } : t)) as Task[]

  it('names the workflow from its own lanes before the workflow read lands, with the id after the steps note', async () => {
    api.loadTasks.mockResolvedValue(ok({ tasks: named() } as TaskPage))
    api.loadWorkflow.mockReturnValue(new Promise(() => {}))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(lane('task_plan')).not.toBeNull())
    const head = document.querySelector<HTMLElement>('.tl-row.is-group[data-group="wf_7c1e"]')!
    // MUTATION: name the group from `wfNames` (the workflow read) alone again.
    expect(head.querySelector('.tl-name-k')!.textContent).toBe('refactor-broker')
    // MUTATION: lead the note with the id again (`${block.key} · `): a
    // real-length id filled the note cell and cut the steps off (review of VQ-025).
    expect(head.querySelector('.tl-lab small')!.textContent).toBe('steps: reading… · wf_7c1e')
    expect(head.querySelector('.tl-lab small')!.getAttribute('title'), 'what the width cuts is read on hover').toBe('steps: reading… · wf_7c1e')
    expect(head.textContent, 'the old wording').not.toContain('workflow: reading')
  })

  it('keeps the id after the steps once the read lands', async () => {
    api.loadTasks.mockResolvedValue(ok({ tasks: named() } as TaskPage))
    api.loadWorkflow.mockResolvedValue(
      ok({ workflow: { workflow_id: 'wf_7c1e', state: 'FAILED', steps: [{}, {}, {}] }, tasks: [named()[1]!, named()[2]!, NEVER_RAN] } as unknown as WorkflowRead),
    )
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(lane('task_publish')).not.toBeNull())
    const head = document.querySelector<HTMLElement>('.tl-row.is-group[data-group="wf_7c1e"]')!
    expect(head.querySelector('.tl-name-k')!.textContent).toBe('refactor-broker')
    expect(head.querySelector('.tl-lab small')!.textContent).toBe('2 of 3 steps have attempts in this span · failed · wf_7c1e')
    expect(head.querySelector('.tl-lab small')!.getAttribute('title')).toBe('2 of 3 steps have attempts in this span · failed · wf_7c1e')
  })

  it('draws a bracket over the workflow’s span: its lanes’ first start to their last end', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(lane('task_test')).not.toBeNull())
    const head = document.querySelector<HTMLElement>('.tl-row.is-group[data-group="wf_7c1e"]')!
    // MUTATION: draw the group's track empty again (`<div className="tl-trk" />`).
    const br = head.querySelector<HTMLElement>('.tl-trk .tl-wf-span')
    expect(br, 'no span bracket on the group row').not.toBeNull()
    // The workflow's lanes draw from their tasks' wait, created 300 minutes
    // ago, to the end marks at the tasks' completed_at, 100 minutes ago, in a
    // 24-hour window ending now.
    const left = Number.parseFloat(br!.style.left)
    // `min(<width>%, calc(<to the edge>% - 4px))`: it stops short of the
    // track's edge at now (QA2-FIX-05, runs2-16).
    // (jsdom's style parser pads the comma, which a browser does not.)
    expect(br!.style.width).toMatch(/^min\([\d.]+%.*calc\([\d.]+% - 4px\)\)$/)
    const width = Number.parseFloat(br!.style.width.slice('min('.length))
    const drawn = [...document.querySelectorAll<HTMLElement>('[data-lane="task_plan"] .tl-seg, [data-lane="task_test"] .tl-seg')]
    expect(Math.min(...drawn.map((e) => Number.parseFloat(e.style.left))), 'the bracket starts where the first bar does').toBeCloseTo(left, 0)
    expect(left).toBeCloseTo(((1440 - 300) / 1440) * 100, 0)
    expect(left + width).toBeCloseTo(((1440 - 100) / 1440) * 100, 0)
    expect(br!.getAttribute('aria-hidden')).toBe('true')
    // The standalone block is not one thing with a span: no bracket.
    expect(document.querySelector('.tl-row.is-group[data-group="standalone"] .tl-wf-span')).toBeNull()
  })

  it('lets the lane count wrap in the top-left cell instead of cutting it', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(lane('task_plan')).not.toBeNull())
    const summary = document.querySelector('.tl-axis .tl-lab small')!
    // MUTATION: drop the `.tl-axis .tl-lab small` white-space rule from timeline.css.
    expect(painted(summary, 'white-space', { width: 1440 })).toBe('normal')
    expect(painted(summary, 'text-overflow', { width: 1440 })).toBe('clip')
  })
})

describe('VQ-024: on a phone the Lanes filters fold behind one Filters button', () => {
  it('puts State, Profile, Workflow, Kind and Group by in a sheet behind `Filters · N`', async () => {
    render(<TimelineLanesScreen view="state=FAILED&kind=steps" onView={() => {}} />)
    await waitFor(() => expect(lane('task_test')).not.toBeNull())
    // MUTATION: drop the `.tl-sheet-toggle` button from TimelineLanes.tsx.
    const toggle = document.querySelector<HTMLButtonElement>('.tl-ctl > .tl-sheet-toggle')
    expect(toggle, 'no Filters button').not.toBeNull()
    expect(toggle!.textContent).toBe('Filters · 2')
    expect(toggle!.getAttribute('aria-expanded')).toBe('false')
    const sheet = document.getElementById(toggle!.getAttribute('aria-controls')!)!
    expect(sheet.classList.contains('tl-sheet')).toBe(true)
    expect(sheet.querySelector('.tl-kind'), 'Kind is not in the sheet').not.toBeNull()
    expect(sheet.querySelector('.tl-wf-sel'), 'Workflow is not in the sheet').not.toBeNull()
    expect(sheet.textContent).toContain('State')
    expect(sheet.textContent).toContain('Group by')
    // The span and the zoom stay out of it.
    expect(sheet.querySelector('.tl-span, .tl-zoom')).toBeNull()
    // Wide: the sheet's controls sit in the row, and there is no button.
    expect(painted(toggle!, 'display', { width: 1440 })).toBe('none')
    expect(painted(sheet, 'display', { width: 1440 })).toBe('contents')
    // Phone: the button, and the sheet only once it is opened.
    expect(painted(toggle!, 'display', { width: 390 })).toBe('inline-flex')
    expect(painted(toggle!, 'min-height', { width: 390 })).toBe('44px')
    expect(painted(sheet, 'display', { width: 390 })).toBe('none')
    fireEvent.click(toggle!)
    expect(toggle!.getAttribute('aria-expanded')).toBe('true')
    expect(sheet.classList.contains('is-open')).toBe(true)
    expect(painted(sheet, 'display', { width: 390 })).toBe('flex')
  })
})

describe('VQ-024: on a phone the Lanes legend folds behind a Key toggle', () => {
  it('folds the legend closed by default on a phone, beside `Filters · N`', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(lane('task_plan')).not.toBeNull())
    // MUTATION: drop the `.tl-key-toggle` button from TimelineLanes.tsx.
    const key = document.querySelector<HTMLButtonElement>('.tl-ctl > .tl-sheet-toggle + .tl-key-toggle')
    expect(key, 'no Key button beside Filters').not.toBeNull()
    expect(key!.tagName).toBe('BUTTON')
    expect(key!.textContent).toBe('Key')
    expect(key!.getAttribute('aria-expanded')).toBe('false')
    const legend = document.getElementById(key!.getAttribute('aria-controls')!)!
    expect(legend.classList.contains('tl-legend')).toBe(true)
    // MUTATION: drop the `.tl-legend.tl-key` phone rule from timeline.css.
    expect(painted(legend, 'display', { width: 390 })).toBe('none')
    expect(painted(key!, 'display', { width: 390 })).toBe('inline-flex')
    expect(painted(key!, 'min-height', { width: 390 })).toBe('44px')
  })

  it('opens the legend on the toggle, its content unchanged, and closes it again', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(lane('task_plan')).not.toBeNull())
    const key = document.querySelector<HTMLButtonElement>('.tl-key-toggle')!
    const legend = document.getElementById(key.getAttribute('aria-controls')!)!
    const words = legend.textContent
    expect(words).toContain('held capacity (leased → running)')
    expect(words).toContain('failed · dead-lettered')
    // MUTATION: make the button's onClick a no-op.
    fireEvent.click(key)
    expect(key.getAttribute('aria-expanded')).toBe('true')
    expect(legend.classList.contains('is-open')).toBe(true)
    expect(painted(legend, 'display', { width: 390 })).toBe('flex')
    expect(legend.textContent).toBe(words)
    expect(legend.querySelectorAll('.tl-lg')).toHaveLength(4)
    // The outcome strip's counts are not folded with it.
    expect(document.querySelector('.tl-strip')).not.toBeNull()
    // Keyboard: a native button, so Enter and Space reach it from Tab.
    key.focus()
    expect(document.activeElement).toBe(key)
    fireEvent.click(key)
    expect(key.getAttribute('aria-expanded')).toBe('false')
    expect(painted(legend, 'display', { width: 390 })).toBe('none')
  })

  it('always shows the legend on a desktop, with no Key button', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(lane('task_plan')).not.toBeNull())
    const key = document.querySelector<HTMLButtonElement>('.tl-key-toggle')!
    const legend = document.getElementById(key.getAttribute('aria-controls')!)!
    expect(painted(key, 'display', { width: 1440 })).toBe('none')
    expect(painted(legend, 'display', { width: 1440 })).toBe('flex')
    fireEvent.click(key)
    expect(painted(legend, 'display', { width: 1440 })).toBe('flex')
  })
})
