/**
 * VISUAL QA RUN 6, LANE QAFIX6-TIMELINE (part of #1680; run
 * wf_31aca00c977e4d37a935, the fixture build of main at d3cdbd62, 1440 and
 * 390 px, light and dark).
 *
 *   6:work-a-001 (#1680 comment 6283709068, QA-027) The strip's fenced and
 *       parks cells printed 'events not read on 17 lanes' in the image while
 *       the page text, read later, had the figures: the capture shot the
 *       strip while the lanes' event reads were still out, and nothing said
 *       so. The cells now carry `aria-busy="true"` while a lane's events read
 *       is in flight, which the capture's `PENDING_JS` waits on.
 *   6:work-a-003 (#1680 comment 6343760837) The headline '91 % success, 272
 *       of 300 decided' is the ledger's, over Sep 12 – Sep 25, above lanes
 *       drawn over the last 24 hours. When the two windows differ, the
 *       headline names its own, at the figure's weight.
 *   6:work-a-004 (#1680 comment 6314122402, owner pick c00) Attempts that
 *       crowd at 'now' are one counted pill. `lanePills` merged at a lone
 *       pill's floor, but a merged pill paints wider (`PILL_COUNT_MIN_PX`),
 *       so the next attempt still crowded it and the layout piled both up
 *       against 'now'.
 *   6:work-a-011 (#1680, run 6) At 390 the '0 workflows · 2 standalone' lane
 *       count was hidden (`.tl-axis .tl-lab { display: none }`): it is in the
 *       sticky head now, above the axis.
 *
 * Each assertion names the mutation that turns it red.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { WorkflowRead } from '../api'
import { ledgerFixture } from '../outcomes.fixture'
import type { Outcomes } from '../outcomes'
import type { AttemptRow, AttemptsPage, Task, TaskPage } from '../types'
import type { CascadeEnv } from './cssgate'
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

const { TimelineLanesScreen, PILL_COUNT_MIN_PX } = await import('../TimelineLanes')

const PHONE: CascadeEnv = { width: 390 }
const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })
const NOW = Date.now()
const MIN = 60_000
const ago = (min: number) => new Date(NOW - min * MIN).toISOString()
const text = (el: Element | null | undefined): string => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

function task(id: string, extra: Partial<Task> = {}): Task {
  return {
    id, tenant_id: 'eng', state: 'FAILED', runner_profile: 'claude-code', resource_class: 'standard', provider: null,
    priority: 0, created_at: ago(100), updated_at: ago(24), started_at: ago(90), completed_at: ago(24),
    submitted_by: 'operator@example.com', attempt_count: 3, max_attempts: 3, park_reason: null, blocked_by: null,
    workflow_id: null, step_id: null, depends_on: null, cancel_requested: false, repository_url: null,
    current_generation: 3, current_lease_id: null, next_eligible_at: null, ...extra,
  } as Task
}

function attempt(taskId: string, gen: number, from: number, to: number | null, exit: number | null): AttemptRow {
  return {
    attempt_id: `att_${taskId}_${gen}`, task_id: taskId, tenant_id: 'eng', generation: gen, lease_id: `l${gen}`,
    backend: 'cloud_run', execution_name: null, created_at: ago(from), started_at: ago(from),
    completed_at: to === null ? null : ago(to), exit_code: exit, error: null, peak_rss_bytes: null, peak_disk_bytes: null,
    oom_near_miss: false, checkpoints: [], input_tokens: null, output_tokens: null, cache_read_input_tokens: null,
    cache_creation_input_tokens: null, cost_usd: null,
  } as unknown as AttemptRow
}

// Two attempts five minutes apart, which `lanePills` merges, and a third an
// hour later: a lone pill's floor apart at the span's scale, inside the
// merged pill's wider one.
const CROWD = task('task_crowd')
const CROWD_ATTEMPTS = [attempt('task_crowd', 1, 90, 89, 1), attempt('task_crowd', 2, 85, 84, 1), attempt('task_crowd', 3, 25, 24, 1)]
// One attempt still running, started three minutes ago.
const LIVE = task('task_live', { state: 'RUNNING', attempt_count: 1, current_generation: 1, completed_at: null, started_at: ago(3) })
const LIVE_ATTEMPTS = [attempt('task_live', 1, 3, null, null)]

const PAGE = {
  tenant_id: 'eng', read_at: new Date(NOW).toISOString(), next_page_token: null, coverage: { scope: 'page' },
  attempts: [...CROWD_ATTEMPTS, ...LIVE_ATTEMPTS],
} as unknown as AttemptsPage

let trackPx = 1100
const realRect = Element.prototype.getBoundingClientRect
const hadObserver = 'ResizeObserver' in globalThis
const RealObserver = globalThis.ResizeObserver

beforeEach(() => {
  trackPx = 1100
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
    return this.classList.contains('tl-trk') ? ({ ...realRect.call(this), width: trackPx } as DOMRect) : realRect.call(this)
  })
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
  api.loadWorkflowsById.mockImplementation(byIdVia(api.loadWorkflow))
  api.loadAttemptsPage.mockResolvedValue(ok(PAGE))
  api.loadTaskEventsPage.mockResolvedValue(ok({ events: [], next_page_token: null }))
  api.loadTasks.mockResolvedValue(ok({ tasks: [CROWD, LIVE] } as TaskPage))
  api.loadTask.mockResolvedValue({ status: 'error', error: { kind: 'server', httpStatus: 404, code: null, message: 'gone' } })
  api.loadWorkflow.mockResolvedValue(ok({ workflow: { workflow_id: 'wf_none', state: 'RUNNING', steps: [] }, tasks: [] } as unknown as WorkflowRead))
  // A ledger over the 24 h span: since QA2-FIX-05 (runs2-04) one over
  // another window shows no figures.
  api.loadOutcomes.mockResolvedValue(ok({ ...ledgerFixture(), tz: 'UTC', bucket: 'hour', since: new Date(Date.now() - 86_400_000).toISOString(), until: new Date().toISOString() }))
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.clearAllMocks()
  if (hadObserver) globalThis.ResizeObserver = RealObserver
  else delete (globalThis as { ResizeObserver?: unknown }).ResizeObserver
})

const lane = (id: string) => document.querySelector<HTMLElement>(`.tl-row[data-lane="${id}"]`)
const pctOf = (v: string) => Number(/^(-?[\d.e+-]+)%$/.exec(v)![1])
/** The strip's cell whose words match: the fenced cell, the parks cell, the headline. */
const cell = (re: RegExp) => [...document.querySelectorAll('.tl-strip1 > .tl-x')].find((el) => re.test(text(el))) ?? null

// ---------------------------------------------------------------------------
// 6:work-a-001: the strip says when it is still reading
// ---------------------------------------------------------------------------

describe('6:work-a-001: the fenced and parks cells are busy exactly while an events read is out', () => {
  it('is busy while pending, not busy with figures once read, not busy with “not read” once failed', async () => {
    const answers: ((r: unknown) => void)[] = []
    api.loadTaskEventsPage.mockImplementation(() => new Promise((res) => answers.push(res)))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    // Pending: the cell says counting and is marked busy.
    await waitFor(() => {
      expect(cell(/fenced/)?.getAttribute('aria-busy')).toBe('true')
    })
    // MUTATION: mark only the fenced cell busy and the capture shoots the parks cell unsettled.
    expect(cell(/park/)?.getAttribute('aria-busy')).toBe('true')
    // Resolved: every lane's read answers, the figures are shown, nothing is busy.
    api.loadTaskEventsPage.mockResolvedValue(ok({ events: [], next_page_token: null }))
    expect(answers.length).toBeGreaterThan(0)
    for (const answer of answers) answer(ok({ events: [], next_page_token: null }))
    await waitFor(() => {
      expect(text(cell(/fenced/))).toMatch(/^0 generations fenced$/)
    })
    // MUTATION: keep aria-busy="true" after the reads answer, and the capture waits out its deadline.
    expect(cell(/fenced/)!.getAttribute('aria-busy')).not.toBe('true')
    expect(text(cell(/park/))).toMatch(/^0 parks, \d+ still parked$/)
    expect(cell(/park/)!.getAttribute('aria-busy')).not.toBe('true')
    cleanup()

    // Failed: the cell says 'not counted' (QA2-FIX-05, runs2-15), and is not busy.
    api.loadTaskEventsPage.mockResolvedValue({ status: 'error', error: { kind: 'server', httpStatus: 500, code: null, message: 'boom' } })
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => {
      // One cell for both, of how many (QA1-FIX-28, chrome-C17 and runs-submit-33).
      expect(text(cell(/fenced/))).toMatch(/not counted on 2 of 2 lanes/)
      // MUTATION: mark the cells busy whenever a lane's events are unread, failed reads included.
      expect(cell(/fenced/)!.getAttribute('aria-busy')).not.toBe('true')
    })
    expect(text(cell(/park/))).toMatch(/not counted on 2 of 2 lanes/)
    expect(cell(/park/)!.getAttribute('aria-busy')).not.toBe('true')
  })
})

// ---------------------------------------------------------------------------
// 6:work-a-003: the headline names its own window
// ---------------------------------------------------------------------------

describe('6:work-a-003: the success headline names the ledger’s window when it is not the span’s', () => {
  it('carries “Sep 12 – Sep 25” in place of the figures over a ledger outside the 24 h span, and nothing over one that matches', async () => {
    api.loadOutcomes.mockResolvedValue(ok(ledgerFixture()))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    const head = await waitFor(() => {
      const el = cell(/not this span, read for/)
      expect(el).not.toBeNull()
      return el!
    })
    // MUTATION: leave the window to the small line under the strip, and the headline reads as the span's.
    expect(text(head)).toMatch(/Sep 12 – Sep 25/)
    // QA2-FIX-05 (runs2-04): the ledger's figures are not printed over another window.
    // MUTATION: print '91 % success' beside the window and it reads as the span's.
    expect(text(head)).not.toMatch(/% success/)
    cleanup()

    // A ledger over the span itself: the headline names no window.
    const d = ledgerFixture()
    const same: Outcomes = { ...d, tz: 'UTC', bucket: 'hour', since: ago(24 * 60), until: ago(0), generated_at: ago(0) }
    api.loadOutcomes.mockResolvedValue(ok(same))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    const plain = await waitFor(() => {
      const el = cell(/% success/)
      expect(el).not.toBeNull()
      return el!
    })
    // MUTATION: name the window whatever it is, and every live page repeats its own span.
    expect(plain.querySelectorAll('b')).toHaveLength(1)
  })
})

// ---------------------------------------------------------------------------
// 6:work-a-004: crowded attempts at 'now' are one counted pill (c00)
// ---------------------------------------------------------------------------

describe('6:work-a-004: attempts crowding at now are one counted pill, ending at or before the now line', () => {
  it('draws one pill of 3 and nothing past now, at 1440 and 390', async () => {
    for (const px of [1100, 358]) {
      trackPx = px
      render(<TimelineLanesScreen view={null} onView={() => {}} />)
      const row = await waitFor(() => {
        const r = lane('task_crowd')
        const pill = r?.querySelector<HTMLElement>('.tl-seg.is-merged') ?? null
        expect(pill).not.toBeNull()
        // Laid out on the measured track: the axis measures it after the first paint.
        expect((pctOf(pill!.style.width) / 100) * px).toBeGreaterThanOrEqual(PILL_COUNT_MIN_PX - 1e-9)
        return r!
      })
      const pills = [...row.querySelectorAll<HTMLElement>('.tl-seg.is-hold, .tl-seg.is-cut')]
      // MUTATION: merge at a lone pill's floor only, and the third attempt is a second pill piled against the first.
      expect(pills, `${px}`).toHaveLength(1)
      expect(pills[0]!.querySelector('.tl-seg-n')?.textContent, `${px}`).toBe('3')
      // The now line is the track's right edge (`.tl-tk.is-now { right: 0 }`): the span ends now.
      for (const id of ['task_crowd', 'task_live']) {
        const bars = [...lane(id)!.querySelectorAll<HTMLElement>('.tl-seg.is-hold, .tl-seg.is-cut')].filter((b) => b.style.left !== '')
        const last = bars[bars.length - 1]!
        const right = ((pctOf(last.style.left) + pctOf(last.style.width)) / 100) * px
        // MUTATION: lay the last pill out at its instants with its floor, and it runs past the now line.
        expect(right, `${px} ${id}`).toBeLessThanOrEqual(px + 1e-9)
      }
      cleanup()
    }
  })
})

// ---------------------------------------------------------------------------
// 6:work-a-011: the lane count at 390
// ---------------------------------------------------------------------------

describe('6:work-a-011: the lane count is shown at 390, in the sticky head above the axis', () => {
  it('keeps the axis row’s count drawn under the phone query', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    const lab = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('.tl-axis .tl-lab')
      expect(el).not.toBeNull()
      return el!
    })
    // The count is the cell's first line; the zone the axis is drawn in is its second (VQ-023).
    expect(text(lab.querySelector('small')!)).toBe('0 workflows · 2 standalone')
    expect(text(lab.querySelector('.tl-zone')!)).toBe('times UTC')
    // MUTATION: put back `.tl-axis .tl-lab { display: none }` in the phone block.
    expect(painted(lab, 'display', PHONE)).not.toBe('none')
    // The axis row is the sticky head, so the count stays above the lanes.
    expect(painted(lab.parentElement!, 'position', PHONE)).toBe('sticky')
  })
})
