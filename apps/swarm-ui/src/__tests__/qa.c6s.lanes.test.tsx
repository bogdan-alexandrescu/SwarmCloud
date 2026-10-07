/**
 * BROWSER QA G3 (2026-10-07, SwarmCloud at 1440 and 390): Timeline › Lanes,
 * lane C6S.
 *
 *   G3-02  A step its review verdict skipped read `fix ✓ · 1m 15s` in Lanes,
 *          a successful run with a duration, where the graph said `agent not
 *          run`.
 *   G3-12  The Lanes axis was static: at the failed groups it sat 7,090 px
 *          above the viewport, so no lane had a time scale.
 *   G3-15  At 390 the `18:00` tick was overprinted by `now`.
 *   G3-18  Escape left the State checklist open, and it stayed open across a
 *          `+` zoom until its button was clicked again.
 *   G3-23  `1 steps · succeeded`.
 *   G3-30  `+` zoomed to the span's middle (03:07–15:07 at 21:07), dropping
 *          the last 6 h, running work included.
 *   G3-31  Zooming inserted the back chip among the controls and moved every
 *          one after it.
 *
 * MUTATIONS, each turns a case red: print `formatDuration(held)` for a
 * skipped step; put `.tl { overflow: hidden }` back or drop the axis's
 * `position: sticky`; keep a tick on the percentage rule alone; remove the
 * picker's Escape or outside-press handler; print `steps` for one; zoom about
 * the middle; render the back chip inside `.tl-ctl`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import { ledgerFixture } from '../outcomes.fixture'
import type { AttemptRow, AttemptsPage, Task, TaskPage } from '../types'
import type { WorkflowRead } from '../api'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import TIMELINE_CSS from '../styles/timeline.css?raw'

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

const { TimelineLanesScreen, axisLabels, zoomWindow, NOW_LABEL_PX, TICK_CHAR_PX, TICK_PAD_PX } = await import('../TimelineLanes')
const { VALUE_LABEL_GAP_PX } = await import('../charts/parts')

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })

const NOW = Date.now()
const ago = (min: number) => new Date(NOW - min * 60_000).toISOString()
const HOUR = 3_600_000

function task(id: string, wf: string, step: string, extra: Partial<Task> = {}): Task {
  return {
    id, tenant_id: 'eng', state: 'SUCCEEDED', runner_profile: 'mock', resource_class: 'standard', provider: null,
    priority: 0, created_at: ago(300), updated_at: ago(100), started_at: ago(290), completed_at: ago(100),
    submitted_by: 'operator@example.com', attempt_count: 1, max_attempts: 3, park_reason: null, blocked_by: null,
    workflow_id: wf, step_id: step, depends_on: null, cancel_requested: false, repository_url: null,
    current_generation: 1, current_lease_id: null, ...extra,
  } as Task
}

function attempt(taskId: string, startMin: number, endMin: number): AttemptRow {
  return {
    attempt_id: `att_${taskId}`, task_id: taskId, tenant_id: 'eng', generation: 1, lease_id: `l_${taskId}`,
    backend: 'cloud_run', execution_name: null, created_at: ago(startMin + 1), started_at: ago(startMin),
    completed_at: ago(endMin), exit_code: 0, error: null, peak_rss_bytes: null, peak_disk_bytes: null,
    oom_near_miss: false, checkpoints: [], input_tokens: null, output_tokens: null, cache_read_input_tokens: null,
    cache_creation_input_tokens: null, cost_usd: null,
  } as AttemptRow
}

const REVIEW = task('task_review', 'wf_three', 'review')
const FIX = task('task_fix', 'wf_three', 'fix', {
  result_summary: { verdict_gate: { agent_ran: false, verdict: 'MERGE' } },
} as Partial<Task>)
const SOLO = task('task_solo', 'wf_one', 'implement')
const TASKS = [REVIEW, FIX, SOLO]

function workflowRead(wf: string): WorkflowRead {
  const steps = wf === 'wf_one' ? [{}] : [{}, {}, {}]
  return { workflow: { workflow_id: wf, state: 'SUCCEEDED', steps }, tasks: [] } as unknown as WorkflowRead
}

beforeEach(() => {
  api.loadAttemptsPage.mockResolvedValue(
    ok({
      tenant_id: 'eng',
      read_at: new Date(NOW).toISOString(),
      attempts: [attempt('task_review', 200, 150), attempt('task_fix', 140, 139), attempt('task_solo', 120, 60)],
      next_page_token: null,
      coverage: { scope: 'page' },
    } as AttemptsPage),
  )
  api.loadTaskEventsPage.mockResolvedValue(ok({ events: [], next_page_token: null }))
  api.loadTasks.mockResolvedValue(ok({ tasks: TASKS } as TaskPage))
  api.loadTask.mockResolvedValue({ status: 'error', error: { kind: 'server', httpStatus: 404, code: null, message: 'gone' } })
  api.loadOutcomes.mockResolvedValue(ok(ledgerFixture()))
  api.loadWorkflow.mockImplementation(async (wf: string) => ok(workflowRead(wf)))
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

async function drawn(props: { onView?: (q: string) => void } = {}): Promise<void> {
  render(<TimelineLanesScreen view={null} {...props} />)
  await waitFor(() => expect(document.querySelector('[data-lane="task_fix"]')).not.toBeNull())
  await waitFor(() => expect(document.querySelectorAll('.tl-row.is-group a').length).toBe(2))
}

describe('G3-02: a verdict-skipped step reads `not run (verdict MERGE)` in Lanes', () => {
  it('prints the verdict where the duration was, and the ran step keeps its duration', async () => {
    await drawn()
    const fix = document.querySelector('[data-lane="task_fix"] .tl-lab b')!
    expect(fix.textContent).toContain('not run (verdict MERGE)')
    expect(fix.textContent, 'a skipped step is not a run with a duration').not.toMatch(/·\s*1m\b/)
    expect(fix.querySelector('.tl-dur')!.getAttribute('title')).toContain('skipped by verdict')
    const review = document.querySelector('[data-lane="task_review"] .tl-dur')!
    expect(review.textContent).toMatch(/^ · 5\dm/)
  })

  it('says the verdict is unknown with a dash rather than inventing one', async () => {
    const bare = { ...FIX, result_summary: { verdict_gate: { agent_ran: false } } } as unknown as Task
    api.loadTasks.mockResolvedValue(ok({ tasks: [REVIEW, bare, SOLO] } as TaskPage))
    await drawn()
    expect(document.querySelector('[data-lane="task_fix"] .tl-dur')!.textContent).toBe(' · not run (verdict —)')
  })
})

describe('G3-23: one step is a step', () => {
  it('reads `1 step · succeeded` and `3 steps · succeeded`', async () => {
    await drawn()
    expect(document.querySelector('.tl-row.is-group[data-group="wf_one"] small')!.textContent).toBe('1 step · succeeded')
    expect(document.querySelector('.tl-row.is-group[data-group="wf_three"] small')!.textContent).toBe('3 steps · succeeded')
  })
})

describe('G3-18: the filter popover closes on Escape and on a press outside it', () => {
  function state(): HTMLDetailsElement {
    return [...document.querySelectorAll<HTMLDetailsElement>('details.tl-pick')].find((d) => d.querySelector('summary')!.textContent!.startsWith('State'))!
  }
  async function open(d: HTMLDetailsElement): Promise<void> {
    fireEvent.click(d.querySelector('summary')!)
    await waitFor(() => expect(d.open).toBe(true))
  }

  it('Escape closes it and returns focus to its button', async () => {
    await drawn()
    const d = state()
    await open(d)
    fireEvent.keyDown(d.querySelector('input')!, { key: 'Escape' })
    await waitFor(() => expect(d.open).toBe(false))
    expect(document.activeElement).toBe(d.querySelector('summary'))
  })

  it('a press outside it -- the `+` zoom included -- closes it; a press inside does not', async () => {
    await drawn()
    const d = state()
    await open(d)
    fireEvent.pointerDown(d.querySelector('input')!)
    expect(d.open).toBe(true)
    fireEvent.pointerDown(document.querySelector('button[aria-label="Zoom in"]')!)
    await waitFor(() => expect(d.open).toBe(false))
  })
})

describe('G3-30: `+` keeps the present in view', () => {
  it('a window ending now zooms about its end; one in the past about its middle', () => {
    const now = Date.UTC(2026, 9, 7, 21, 7)
    expect(zoomWindow({ since: now - 24 * HOUR, until: now }, 0.5, now)).toEqual({ since: now - 12 * HOUR, until: now })
    expect(zoomWindow({ since: now - 12 * HOUR, until: now }, 2, now)).toEqual({ since: now - 24 * HOUR, until: now })
    const past = { since: now - 48 * HOUR, until: now - 24 * HOUR }
    expect(zoomWindow(past, 0.5, now)).toEqual({ since: now - 42 * HOUR, until: now - 30 * HOUR })
    // Zooming a past window out never reaches past the present.
    expect(zoomWindow({ since: now - 10 * HOUR, until: now - 2 * HOUR }, 2, now).until).toBe(now)
  })

  it('the screen’s `+` writes a window that still ends now', async () => {
    const onView = vi.fn()
    await drawn({ onView })
    fireEvent.click(document.querySelector('button[aria-label="Zoom in"]')!)
    const q = new URLSearchParams(onView.mock.calls.at(-1)![0] as string)
    const since = Date.parse(q.get('since')!)
    const until = Date.parse(q.get('until')!)
    expect(Math.abs(until - Date.now())).toBeLessThan(120_000)
    expect(until - since).toBe(12 * HOUR)
  })
})

describe('G3-31: zooming does not move the controls', () => {
  it('the back chip has its own line below the toolbar, which keeps the same controls', async () => {
    await drawn()
    const ctl = document.querySelector('.tl-ctl')!
    const before = [...ctl.children].map((c) => c.className)
    fireEvent.click(document.querySelector('button[aria-label="Zoom in"]')!)
    const back = await waitFor(() => {
      const el = document.querySelector('.tl-back')
      expect(el).not.toBeNull()
      return el!
    })
    expect(ctl.contains(back)).toBe(false)
    expect([...document.querySelector('.tl-ctl')!.children].map((c) => c.className)).toEqual(before)
  })

  it('a picker’s summary and the workflow select are bounded, so a selection cannot widen the row', () => {
    expect(TIMELINE_CSS).toMatch(/\.tl-flt i\s*\{[^}]*max-width:[^}]*text-overflow:\s*ellipsis/)
    expect(TIMELINE_CSS).toMatch(/\.tl-flt select\s*\{[^}]*max-width:/)
  })
})

describe('G3-15: no tick label within 14 px of `now`', () => {
  // 21:07 local, the QA's clock.
  const at = new Date(2026, 9, 7, 21, 7).getTime()
  const since = at - 24 * HOUR

  it('drops `18:00` at a phone’s track width and keeps it on a wide one', () => {
    expect(axisLabels(since, at, at, 358).map((t) => t.label)).not.toContain('18:00')
    expect(axisLabels(since, at, at, 1200).map((t) => t.label)).toContain('18:00')
  })

  it('every kept label ends 14 px before `now` and 14 px before the next label', () => {
    for (const w of [300, 358, 600, 1200]) {
      const ticks = axisLabels(since, at, at, w)
      expect(ticks.length, `no ticks at ${w}`).toBeGreaterThan(0)
      const ends = ticks.map((t) => ({ from: (t.pct / 100) * w, to: (t.pct / 100) * w + TICK_PAD_PX + t.label.length * TICK_CHAR_PX }))
      for (const e of ends) expect(e.to, `a tick runs into now at ${w}`).toBeLessThanOrEqual(w - NOW_LABEL_PX - VALUE_LABEL_GAP_PX)
      for (let i = 1; i < ends.length; i += 1) expect(ends[i]!.from - ends[i - 1]!.to).toBeGreaterThanOrEqual(VALUE_LABEL_GAP_PX)
    }
  })
})

describe('G3-12: the Lanes time axis stays in view while the lanes scroll', () => {
  const ENVS: CascadeEnv[] = [{ width: 1440 }, { width: 390 }]

  it('is sticky at the top of the page’s scroller, painted, over the lanes', async () => {
    await drawn()
    const axis = document.querySelector('.tl-row.tl-axis')!
    for (const env of ENVS) {
      expect(painted(axis, 'position', env), `at ${env.width}`).toBe('sticky')
      expect(painted(axis, 'top', env)).toBe('0')
      expect(Number(painted(axis, 'z-index', env))).toBeGreaterThanOrEqual(2)
      expect(painted(axis, ['background', 'background-color'], env)).toBe('var(--surface-2)')
    }
  })

  it('no box between the axis and the page scroller is a scroll container, which would hold the axis in place', async () => {
    await drawn()
    const axis = document.querySelector('.tl-row.tl-axis')!
    let checked = 0
    for (let n = axis.parentElement; n !== null && n !== document.body; n = n.parentElement) {
      checked += 1
      for (const env of ENVS) {
        const o = painted(n, ['overflow', 'overflow-y'], env) ?? 'visible'
        expect(['visible', 'clip'], `${n.className} at ${env.width}`).toContain(o)
      }
    }
    expect(checked).toBeGreaterThanOrEqual(2)
  })
})
