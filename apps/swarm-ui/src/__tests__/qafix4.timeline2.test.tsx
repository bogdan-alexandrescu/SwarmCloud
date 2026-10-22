// QAFIX4-TIMELINE-2, CROWDED ATTEMPTS DRAW AS ONE COUNTED PILL (part of #1680;
// owner decision 2026-10-22 01:23Z, Console Design Calls 2, card c00 "Merge
// crowded attempts into one counted pill"; box comment 6314122402):
//
//   run 3 work-a-005  each attempt was its own floor-width pill, packed right
//          to left from 'now': on a 24 h span three 5-minute attempts were
//          three pills and a mark in the last ~60 px.
//   run 4 work-a-004  the right-most pill was cut in half at 'now'.
//   run 4 work-a-006  the end mark sat between pills, in a different place at
//          1440 and at 390.
//
// Decided: attempts closer together than one pill's width at the span's scale
// draw as ONE pill with the count inside, in the newest attempt's style, and
// the end mark right after it. Hovering, focusing or tapping it lists each
// attempt. Zooming in splits it again. The same at every width; nothing past
// 'now'.
//
// Every assertion names the mutation that turns it red.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { WorkflowRead } from '../api'
import { attemptWord, buildLane, lanePills, pillName, serializeLanesView, zoomedTo, DEFAULT_LANES_VIEW } from '../lanes'
import { ledgerFixture } from '../outcomes.fixture'
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

const { TimelineLanesScreen, MARK_PX, PILL_COUNT_MIN_PX, laneLayout } = await import('../TimelineLanes')

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })
const NOW = Date.now()
const MIN = 60_000
const ago = (min: number) => new Date(NOW - min * MIN).toISOString()
const DAY = 24 * 60 * MIN

function task(id: string, extra: Partial<Task> = {}): Task {
  return {
    id, tenant_id: 'eng', state: 'FAILED', runner_profile: 'claude-code', resource_class: 'standard', provider: null,
    priority: 0, created_at: ago(40), updated_at: ago(4), started_at: ago(30), completed_at: ago(4),
    submitted_by: 'operator@example.com', attempt_count: 3, max_attempts: 3, park_reason: null, blocked_by: null,
    workflow_id: null, step_id: null, depends_on: null, cancel_requested: false, repository_url: null,
    current_generation: 3, current_lease_id: null, next_eligible_at: null, ...extra,
  } as Task
}

function attempt(taskId: string, gen: number, from: number, to: number, exit: number): AttemptRow {
  return {
    attempt_id: `att_${taskId}_${gen}`, task_id: taskId, tenant_id: 'eng', generation: gen, lease_id: `l${gen}`,
    backend: 'cloud_run', execution_name: null, created_at: ago(from), started_at: ago(from), completed_at: ago(to),
    exit_code: exit, error: null, peak_rss_bytes: null, peak_disk_bytes: null, oom_near_miss: false, checkpoints: [],
    input_tokens: null, output_tokens: null, cache_read_input_tokens: null, cache_creation_input_tokens: null, cost_usd: null,
  } as unknown as AttemptRow
}

// Three 5-minute attempts, the newest ending four minutes ago, the task failed.
const CROWD = task('task_crowd')
const CROWD_ATTEMPTS = [attempt('task_crowd', 1, 30, 25, 1), attempt('task_crowd', 2, 19, 14, 1), attempt('task_crowd', 3, 9, 4, 1)]
// Two attempts hours apart.
const APART = task('task_apart', { state: 'SUCCEEDED', attempt_count: 2, current_generation: 2, completed_at: ago(115), created_at: ago(620) })
const APART_ATTEMPTS = [attempt('task_apart', 1, 600, 595, 1), attempt('task_apart', 2, 120, 115, 0)]

describe('c00: which attempts merge, as a pure layout over the span', () => {
  it('lays three 5-minute attempts near now out as ONE pill of 3 on a 24 h span, in the newest attempt\'s state', () => {
    const lane = buildLane('task_crowd', CROWD, CROWD_ATTEMPTS, [], NOW)
    const out = lanePills(lane.segs, lane.marks, NOW - DAY, NOW)
    // MUTATION: never merge (one pill per attempt, as before) and this is 3.
    expect(out.pills).toHaveLength(1)
    // MUTATION: merge only the first two (stop the chain at one join) and the count is 2.
    expect(out.pills[0]!.segs).toHaveLength(3)
    const newest = out.pills[0]!.segs[out.pills[0]!.segs.length - 1]!
    // MUTATION: order a pill's attempts newest first and its style is the oldest's.
    expect(newest.gen).toBe(3)
    // MUTATION: word the newest attempt by its exit code alone as 'exit 1'.
    expect(attemptWord(newest, CROWD_ATTEMPTS[2]!)).toBe('failed')
    // MUTATION: name the pill by its oldest attempt, or leave the count out.
    expect(pillName(out.pills[0]!, lane.attempts)).toBe('3 attempts, newest failed')
    // MUTATION: let the merged pill end past the newest attempt's end.
    expect(out.pills[0]!.to).toBe(Date.parse(ago(4)))
    expect(out.pills[0]!.from).toBe(Date.parse(ago(30)))
  })

  it('puts the end mark right after the pill, never before or among it', () => {
    // The task wrote its end a minute BEFORE the newest attempt's: as read
    // back in run 4, the mark then sat between the pills.
    const early = task('task_crowd', { completed_at: ago(5) })
    const lane = buildLane('task_crowd', early, CROWD_ATTEMPTS, [], NOW)
    const out = lanePills(lane.segs, lane.marks, NOW - DAY, NOW)
    const end = out.marks.find((m) => m.kind === 'failed')!
    // MUTATION: keep the end mark at its own instant and it lands inside the pill.
    expect(end.at).toBeGreaterThanOrEqual(out.pills[out.pills.length - 1]!.to)
    // MUTATION: move the mark to now (the window's end) rather than to the pill.
    expect(end.at).toBe(Date.parse(ago(4)))
  })

  it('splits the same lane into three pills on a 1-hour span', () => {
    const lane = buildLane('task_crowd', CROWD, CROWD_ATTEMPTS, [], NOW)
    const out = lanePills(lane.segs, lane.marks, NOW - 60 * MIN, NOW)
    // MUTATION: size the merge distance by a fixed time (an hour) rather than
    // by one pill at the span's scale and the zoom never splits it.
    expect(out.pills.map((p) => p.segs.length)).toEqual([1, 1, 1])
  })

  it('keeps two attempts hours apart as two pills on a 24 h span', () => {
    const lane = buildLane('task_apart', APART, APART_ATTEMPTS, [], NOW)
    const out = lanePills(lane.segs, lane.marks, NOW - DAY, NOW)
    // MUTATION: merge every attempt of a lane into one pill and this is [2].
    expect(out.pills.map((p) => p.segs.length)).toEqual([1, 1])
  })

  it('lays out no pill past now, with the end mark after the pill, the same at 1440 and 390', () => {
    const lane = buildLane('task_crowd', CROWD, CROWD_ATTEMPTS, [], NOW)
    const since = NOW - DAY
    const out = lanePills(lane.segs, lane.marks, since, NOW)
    const pct = (t: number) => ((t - since) / DAY) * 100
    const shapes: string[] = []
    // A 1440 desktop's track and a 390 phone's.
    for (const trackPx of [1100, 358]) {
      const placed = laneLayout(
        out.pills.map((p) => ({ from: pct(p.from), to: pct(p.to) })),
        out.marks.map((m) => ({ pct: pct(m.at), end: m.kind !== 'cancel_requested' })),
        trackPx,
      )
      const pill = placed.bars[0]!
      const right = ((pill.left + pill.width) / 100) * trackPx
      const markFrom = (placed.marks[0]! / 100) * trackPx + 2
      // MUTATION: drop the track-edge limit from laneLayout and the pill runs past 'now'.
      expect(right, `${trackPx}: pill past now`).toBeLessThanOrEqual(trackPx + 1e-9)
      // MUTATION: keep the end mark at its own instant and it starts inside the pill.
      expect(markFrom, `${trackPx}: mark before the pill's end`).toBeGreaterThan(right)
      expect(markFrom + MARK_PX, `${trackPx}: mark past now`).toBeLessThanOrEqual(trackPx + 1e-9)
      shapes.push(JSON.stringify({ pills: out.pills.map((p) => p.segs.length), order: ['pill', 'mark'] }))
    }
    // MUTATION: give lanePills the track's width and merge by pixels, and the
    // phone merges what the desktop splits.
    expect(lanePills.length).toBe(4)
    expect(shapes[0]).toBe(shapes[1])
  })
})

// ---------------------------------------------------------------------------
// The drawn lane
// ---------------------------------------------------------------------------

const PAGE = {
  tenant_id: 'eng', read_at: new Date(NOW).toISOString(), next_page_token: null, coverage: { scope: 'page' },
  attempts: [...CROWD_ATTEMPTS, ...APART_ATTEMPTS],
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
  api.loadTasks.mockResolvedValue(ok({ tasks: [CROWD, APART] } as TaskPage))
  api.loadTask.mockResolvedValue({ status: 'error', error: { kind: 'server', httpStatus: 404, code: null, message: 'gone' } })
  api.loadWorkflow.mockResolvedValue(ok({ workflow: { workflow_id: 'wf_none', state: 'RUNNING', steps: [] }, tasks: [] } as unknown as WorkflowRead))
  api.loadOutcomes.mockResolvedValue(ok(ledgerFixture()))
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

async function merged(id: string): Promise<HTMLElement> {
  return waitFor(() => {
    const el = lane(id)?.querySelector<HTMLElement>('.tl-seg.is-merged') ?? null
    expect(el, `${id}: no merged pill`).not.toBeNull()
    // Laid out on the measured track, not yet at its instants: the axis
    // measures the track after the first paint.
    // MUTATION: lay the merged pill out at a lone pill's floor, narrower than it paints.
    expect((pctOf(el!.style.width) / 100) * trackPx, `${id}: not laid out at its floor`).toBeGreaterThanOrEqual(PILL_COUNT_MIN_PX - 1e-9)
    return el!
  })
}

describe('c00: the drawn lane', () => {
  it('draws one counted pill named by its count and newest state, the end mark after it, at 1440 and 390', async () => {
    const seen: string[] = []
    for (const px of [1100, 358]) {
      trackPx = px
      render(<TimelineLanesScreen view={null} onView={() => {}} />)
      const pill = await merged('task_crowd')
      const row = lane('task_crowd')!
      // MUTATION: draw each attempt as its own pill again and there are three.
      expect(row.querySelectorAll('.tl-seg.is-hold, .tl-seg.is-cut'), `${px}`).toHaveLength(1)
      // MUTATION: leave the count out of the pill.
      expect(pill.querySelector('.tl-seg-n')?.textContent, `${px}`).toBe('3')
      // MUTATION: name the pill by its oldest attempt, or by 'gen 3' alone.
      expect(pill.getAttribute('aria-label'), `${px}`).toBe('3 attempts, newest failed')
      // MUTATION: style the pill as a park or a fence: it is the newest attempt's, a hold.
      expect(pill.getAttribute('data-seg'), `${px}`).toBe('hold')
      const right = ((pctOf(pill.style.left) + pctOf(pill.style.width)) / 100) * px
      // MUTATION: draw the merged pill at its instants rather than its layout box.
      expect(right, `${px}: pill past now`).toBeLessThanOrEqual(px + 1e-9)
      const mark = row.querySelector<HTMLElement>('.tl-mkr.is-end')!
      const markFrom = (pctOf(mark.style.left) / 100) * px + 2
      // MUTATION: lay the mark out before the pill and it sits among it.
      expect(markFrom, `${px}: the end mark is not after the pill`).toBeGreaterThan(right)
      // The lane's label still counts its attempts.
      // MUTATION: replace the label's note with the pill's count.
      expect(row.querySelector('.tl-lab')!.textContent, `${px}`).toMatch(/3 attempts/)
      // The two attempts hours apart stay two pills.
      // MUTATION: merge every lane's attempts whatever their distance.
      expect(lane('task_apart')!.querySelectorAll('.tl-seg.is-hold'), `${px}`).toHaveLength(2)
      expect(lane('task_apart')!.querySelector('.tl-seg.is-merged'), `${px}`).toBeNull()
      seen.push([...row.querySelector('.tl-trk')!.children].map((c) => c.getAttribute('data-seg') ?? c.getAttribute('data-mark')).join(' '))
      cleanup()
    }
    // MUTATION: merge by the measured track's pixels and the phone draws a
    // different set of pills from the desktop.
    expect(seen[1]).toBe(seen[0])
  })

  it('lists each attempt, its number, state, start and end, on focus, hover and tap', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    const pill = await merged('task_crowd')
    const row = lane('task_crowd')!
    // MUTATION: render the list before it is asked for.
    expect(row.querySelector('.tl-pill-list')).toBeNull()
    // MUTATION: draw the merged pill as a plain span with no focus.
    expect(pill.tagName).toBe('BUTTON')
    fireEvent.focus(pill)
    const items = () => [...row.querySelectorAll('.tl-pill-list li')].map((li) => li.textContent ?? '')
    // MUTATION: list only the newest attempt on focus.
    expect(items()).toHaveLength(3)
    // MUTATION: list newest first, or drop the state, start or end words.
    expect(items()[0]).toMatch(/^gen 1 · failed · \d\d:\d\d → \d\d:\d\d$/)
    expect(items()[2]).toMatch(/^gen 3 · failed · /)
    // MUTATION: leave aria-expanded off and a screen reader is not told the list opened.
    expect(pill.getAttribute('aria-expanded')).toBe('true')
    expect(document.getElementById(pill.getAttribute('aria-controls')!)?.querySelectorAll('li')).toHaveLength(3)
    fireEvent.blur(pill)
    // MUTATION: keep the list open after the focus leaves.
    expect(row.querySelector('.tl-pill-list')).toBeNull()
    fireEvent.mouseEnter(pill)
    // MUTATION: open the list on focus only, not on hover.
    expect(items()).toHaveLength(3)
    fireEvent.mouseLeave(pill)
    expect(row.querySelector('.tl-pill-list')).toBeNull()
    fireEvent.click(pill)
    // MUTATION: drop the click handler and a phone, which has no hover, never sees the list.
    expect(items()).toHaveLength(3)
    fireEvent.mouseLeave(pill)
    // MUTATION: let a mouse leaving close a list a tap opened.
    expect(items()).toHaveLength(3)
    fireEvent.keyDown(pill, { key: 'Escape' })
    // MUTATION: ignore Escape on the pill.
    expect(row.querySelector('.tl-pill-list')).toBeNull()
  })

  it('splits the pill back into three when zoomed to one hour', async () => {
    const view = serializeLanesView(zoomedTo(DEFAULT_LANES_VIEW, NOW - 60 * MIN, NOW))
    render(<TimelineLanesScreen view={view} onView={() => {}} />)
    const row = await waitFor(() => {
      const r = lane('task_crowd')
      expect(r?.querySelectorAll('.tl-seg.is-hold')).toHaveLength(3)
      return r!
    })
    // MUTATION: merge by a fixed time rather than the span's scale and the zoom draws one pill.
    expect(row.querySelector('.tl-seg.is-merged')).toBeNull()
  })

  it('paints the merged pill as a pill at the floor, a pointer over it', () => {
    const host = document.createElement('div')
    host.innerHTML = '<div class="tl-trk"><button class="tl-seg is-hold is-merged"><span class="tl-seg-n">3</span></button></div>'
    document.body.appendChild(host)
    const pill = host.querySelector('.tl-seg')!
    const envs: CascadeEnv[] = [{ width: 1440, theme: 'light' }, { width: 390, theme: 'dark' }]
    for (const env of envs) {
      // The count needs more than a lone pill's 14 px: 5 px padding and the
      // borders left 1 px for '3'. The layout takes the same floor.
      // MUTATION: drop `.tl-seg.is-merged`'s min-width and '3' is clipped to nothing.
      expect(painted(pill, 'min-width', env), `${env.width}`).toBe(`${PILL_COUNT_MIN_PX}px`)
      // MUTATION: drop `cursor: pointer` and the pill does not read as something to open.
      expect(painted(pill, 'cursor', env), `${env.width}`).toBe('pointer')
      // MUTATION: drop `.tl-seg-n`'s weight and the count reads as a label.
      expect(painted(pill.querySelector('.tl-seg-n')!, 'font-weight', env), `${env.width}`).toBe('650')
    }
    host.remove()
  })
})
