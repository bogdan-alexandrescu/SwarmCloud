// Queue / cold start / run per attempt, the retry lollipop, and the summed
// agent work -- read back off the rendered SVG, and off the pure derivation
// the chart draws from.
//
// THE DEFECTS THESE EXIST FOR, each one a way this chart could lie:
//
//   * an open interval closed at "now" -- a killed attempt that grows by an
//     hour every time the page is opened;
//   * an open interval closed at the reconciler's reclaim event, which is
//     written minutes after the worker died;
//   * an unknown segment given zero width so its neighbours slide over (the
//     audit's §B5 stacking finding);
//   * a retry's queue started at the previous attempt's end, folding back-off
//     and parks into "queue";
//   * an open attempt's lower bound summed into the total;
//   * a measured sub-second span printed as "0s".

import { render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AttemptDurations } from '../charts/AttemptPhases'
import { phaseExtent, phasesFor, spanText, workSum } from '../duration'
import type { AttemptRow, Task, TaskEvent } from '../types'
import { MIN, at, attempt, ev, task } from './runfixture'

function draw(t: Task, attempts: AttemptRow[], events: TaskEvent[] | null) {
  return render(<AttemptDurations task={t} attempts={attempts} events={events} />)
}

function row(container: HTMLElement, n: number): Element {
  const r = container.querySelector(`[data-testid="phase-row"][data-attempt="${n}"]`)
  expect(r, `attempt ${n} has no row`).not.toBeNull()
  return r!
}

function seg(container: HTMLElement, n: number, phase: string): Element {
  const s = row(container, n).querySelector(`[data-phase="${phase}"]`)
  expect(s, `attempt ${n} has no ${phase} mark at all`).not.toBeNull()
  return s!
}

function rectOf(el: Element): { x: number; w: number } {
  const r = el.querySelector('rect')
  expect(r, 'this segment drew no rect').not.toBeNull()
  return { x: Number(r!.getAttribute('x')), w: Number(r!.getAttribute('width')) }
}

afterEach(() => {
  vi.useRealTimers()
})

describe('redesign-v2 §9, the measured example', () => {
  // task_b208fc8542724268b5f4, the first real claude-code run:
  //   03:46:05 dispatched · 03:49:14 starting (+3m 09s) · 03:49:14 running
  //   (+0.1s) · 03:49:32 succeeded (+18s).
  // Those four instants are §9's. The submission and admission instants are
  // NOT in §9 and are constructed here: submitted 2s before admission, and
  // admission 400ms before the backend call returned -- the order the
  // scheduler writes them in (create_attempt, then dispatch, loop.py).
  const T = (s: string) => `2026-09-22T${s}Z`
  const t = task({ created_at: T('03:46:02.600') })
  const a = attempt(1, {
    created_at: T('03:46:04.600'),
    started_at: T('03:49:14.000'),
    completed_at: T('03:49:32.000'),
  })
  const events = [
    ev('dispatched', T('03:46:05.000'), 'att_1'),
    ev('starting', T('03:49:14.000'), 'att_1'),
    ev('running', T('03:49:14.100'), 'att_1'),
    ev('succeeded', T('03:49:32.000'), 'att_1'),
  ]

  it('draws the wait for a container and the agent as two different segments', () => {
    const { container } = draw(t, [a], events)
    const cold = seg(container, 1, 'cold')
    const run = seg(container, 1, 'run')
    expect(cold.getAttribute('data-kind')).toBe('closed')
    expect(run.getAttribute('data-kind')).toBe('closed')
    expect(Number(cold.getAttribute('data-ms'))).toBe(189_400)
    expect(Number(run.getAttribute('data-ms'))).toBe(18_000)
    expect(cold.querySelector('title')?.textContent).toBe('cold start 3m 9s')
    expect(run.querySelector('title')?.textContent).toBe('run 18s')

    // THE POINT OF THE CHART: the cold start is drawn about ten times the run.
    const ratio = rectOf(cold).w / rectOf(run).w
    expect(ratio).toBeGreaterThan(9)
    expect(ratio).toBeLessThan(12)
  })

  it('cuts the cold start where the backend call returned, 3m 09s before the start', () => {
    const { container } = draw(t, [a], events)
    const cut = row(container, 1).querySelector('[data-testid="dispatched-cut"]')
    expect(cut, 'the dispatched instant is not drawn').not.toBeNull()
    expect(cut!.querySelector('title')?.textContent).toBe('dispatched +400ms')
  })

  it('states the summed work over the one attempt, as a complete total', () => {
    const { container } = draw(t, [a], events)
    const sum = container.querySelector('[data-testid="work-sum"]')!
    expect(sum.textContent).toContain('ran 18s over 1 of 1')
    expect(sum.getAttribute('data-partial')).toBe('no')
  })
})

describe('an interval with no recorded end is drawn open, never closed', () => {
  const t = task({ state: 'RUNNING' })
  const a = attempt(1, { created_at: at(0), started_at: at(1), completed_at: null, exit_code: null })
  const events = [
    ev('running', at(1), 'att_1'),
    ev('heartbeat', at(6), 'att_1', { peak_rss_bytes: 1 }),
  ]

  it('bounds a running attempt by its newest heartbeat, and does not read the clock', () => {
    // THE MUTATION THIS EXISTS FOR: close the open run at Date.now(). Opened a
    // day later, the attempt would be a day longer; nothing measured it.
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date(at(7)))
    const first = draw(t, [a], events)
    const soon = seg(first.container, 1, 'run')
    expect(soon.getAttribute('data-kind')).toBe('open')
    expect(Number(soon.getAttribute('data-at-least-ms'))).toBe(5 * MIN)
    first.unmount()

    vi.setSystemTime(new Date(at(60 * 24)))
    const later = draw(t, [a], events)
    const run = seg(later.container, 1, 'run')
    expect(Number(run.getAttribute('data-at-least-ms'))).toBe(5 * MIN)
    expect(run.getAttribute('data-live')).toBe('yes')
    expect(run.querySelector('path.ctl-phase-chevron'), 'an open segment lost its chevron').not.toBeNull()
  })

  it('builds the axis from recorded instants only: the open run ends the domain at its lower bound', () => {
    const p = phasesFor(t, [a], events)
    const extent = phaseExtent(p.rows)
    // 6 minutes after admission is the heartbeat. Nothing past it is drawn.
    expect(extent).toEqual({ lo: -1 * MIN, hi: 6 * MIN, degenerate: false })
  })

  it('does not take a reclaim event as evidence the worker was still running', () => {
    // Attempt 1 was superseded: its worker's last heartbeat is at T+4m and the
    // reconciler fenced it 35 minutes later. "Ran at least until the fence"
    // would stretch a dead attempt by the whole reclaim delay.
    const t2 = task({ state: 'RUNNING', attempt_count: 2 })
    const dead = attempt(1, { created_at: at(0), started_at: at(1), completed_at: null, exit_code: null })
    const next = attempt(2, { created_at: at(45), started_at: at(46), completed_at: null, exit_code: null })
    const p = phasesFor(t2, [dead, next], [
      ev('heartbeat', at(5), 'att_1', { peak_rss_bytes: 1 }),
      ev('generation_fenced', at(40), 'att_1'),
      ev('lease_released', at(40.5), 'att_1'),
    ])
    const run = p.rows[0]!.run!
    expect(run.kind).toBe('open')
    if (run.kind !== 'open') return
    expect(run.atLeastMs).toBe(4 * MIN)
    expect(run.live).toBe(false)
  })

  it('never adds an open attempt’s lower bound into the total', () => {
    const t3 = task({ state: 'RUNNING', attempt_count: 3 })
    const attempts = [
      attempt(1, { created_at: at(0), started_at: at(1), completed_at: at(10) }),
      attempt(2, { created_at: at(20), started_at: at(21), completed_at: at(28) }),
      attempt(3, { created_at: at(40), started_at: at(41), completed_at: null, exit_code: null }),
    ]
    const events = [ev('ready', at(35), null), ev('heartbeat', at(46), 'att_3', {})]
    const sum = workSum(phasesFor(t3, attempts, events))
    expect(sum.ms).toBe(16 * MIN)
    expect(sum.open).toBe(1)
    expect(sum.openAtLeastMs).toBe(5 * MIN)
    expect(sum.partial).toBe(true)

    const { container } = draw(t3, attempts, events)
    const line = container.querySelector('[data-testid="work-sum"]')!
    expect(line.textContent).toContain('ran 16m 0s over 2 of 3')
    expect(line.textContent).toContain('1 open, ≥ 5m 0s')
    expect(line.getAttribute('data-partial')).toBe('yes')
  })

  it('says the total is unknown, not zero, when no attempt has closed', () => {
    const { container } = draw(t, [a], events)
    const line = container.querySelector('[data-testid="work-sum"]')!
    expect(line.querySelector('.ctl-em')?.textContent).toBe('—')
    expect(line.textContent).not.toMatch(/ran 0s/)
  })
})

describe('an unknown segment removes itself and moves nothing else', () => {
  const t = task({ attempt_count: 2, created_at: at(-2) })
  const attempts = [
    attempt(1, { created_at: at(0), started_at: at(1), completed_at: at(10) }),
    attempt(2, { created_at: at(20), started_at: at(23), completed_at: at(30) }),
  ]

  it('draws a retry’s queue as absent when its ready event is not on the page', () => {
    const { container } = draw(t, attempts, [])
    const q = seg(container, 2, 'queue')
    expect(q.getAttribute('data-kind')).toBe('absent')
    expect(q.querySelector('rect')?.getAttribute('fill') ?? '').toMatch(/^url\(#ctl-hatch-/)
    expect(q.querySelector('title')?.textContent).toBe('queue —')
    // The legend names the hatch, because one is drawn.
    expect(container.querySelector('.ctl-chart-legend')?.textContent).toContain('not measured')
  })

  it('keeps both rows’ cold starts on admission, and each run flush against its cold start', () => {
    // THE §B5 STACKING DEFECT, as geometry. Row 1 has a measured queue and
    // row 2 has none. Stacked from the left edge, row 2's cold start would
    // begin where row 1's queue does. Anchored at admission, both begin at
    // the same x.
    const { container } = draw(t, attempts, [])
    const cold1 = rectOf(seg(container, 1, 'cold'))
    const cold2 = rectOf(seg(container, 2, 'cold'))
    const run2 = rectOf(seg(container, 2, 'run'))
    expect(cold2.x).toBeCloseTo(cold1.x, 6)
    // The 2px surface gap between touching segments, and nothing else.
    expect(run2.x - (cold2.x + cold2.w)).toBeCloseTo(2, 6)
    expect(Number(seg(container, 2, 'cold').getAttribute('data-ms'))).toBe(3 * MIN)
    expect(Number(seg(container, 2, 'run').getAttribute('data-ms'))).toBe(7 * MIN)
  })

  it('starts a retry’s queue at the ready event, not at the previous attempt’s end', () => {
    // Attempt 1 ended at T+10m; the task was put back in line at T+15m
    // (after back-off); attempt 2 was admitted at T+20m. Queue is 5 minutes,
    // not 10.
    const p = phasesFor(t, attempts, [ev('ready', at(15), null)])
    const q = p.rows[1]!.queue
    expect(q.kind).toBe('closed')
    if (q.kind === 'closed') expect(q.ms).toBe(5 * MIN)
  })

  it('refuses an interval whose end is recorded before its start', () => {
    const skewed = [attempt(1, { created_at: at(0), started_at: at(5), completed_at: at(4) })]
    const { container } = draw(task(), skewed, [])
    const run = seg(container, 1, 'run')
    expect(run.getAttribute('data-kind')).toBe('absent')
    const line = container.querySelector('[data-testid="work-sum"]')!
    expect(line.textContent).toContain('1 unreadable')
    expect(line.getAttribute('data-partial')).toBe('yes')
  })
})

describe('the retry lollipop', () => {
  it('draws one stem per attempt: a dot at a recorded run, an arrowhead at a lower bound, a ring for never ran', () => {
    const t = task({ state: 'RUNNING', attempt_count: 4 })
    const attempts = [
      attempt(1, { created_at: at(0), started_at: at(1), completed_at: at(10) }),
      // Admitted and never started, then superseded: it did no work.
      attempt(2, { created_at: at(12), started_at: null, completed_at: null, exit_code: null }),
      attempt(3, { created_at: at(20), started_at: at(21), completed_at: at(24) }),
      attempt(4, { created_at: at(30), started_at: at(31), completed_at: null, exit_code: null }),
    ]
    const { container } = draw(t, attempts, [ev('heartbeat', at(33), 'att_4', {})])
    const lollies = [...container.querySelectorAll('[data-testid="lolly"]')]
    expect(lollies.map((l) => l.getAttribute('data-kind'))).toEqual([
      'closed',
      'never-ran',
      'closed',
      'open',
    ])
    expect(Number(lollies[0]!.getAttribute('data-ms'))).toBe(9 * MIN)
    // The open attempt has NO dot: a dot is a value, and it has none.
    expect(lollies[3]!.querySelector('circle')).toBeNull()
    expect(lollies[3]!.querySelector('path.ctl-lolly-open')).not.toBeNull()
    // Never ran: the measured-zero ring, which is a fact about the attempt.
    expect(lollies[1]!.querySelector('circle.is-zero')).not.toBeNull()

    const line = container.querySelector('[data-testid="work-sum"]')!
    expect(line.textContent).toContain('ran 12m 0s over 3 of 4')
    expect(line.textContent).toContain('1 never ran')
  })

  it('is not drawn for a task with a single attempt', () => {
    const { container } = draw(task(), [attempt(1)], [])
    expect(container.querySelector('[data-testid="lolly"]')).toBeNull()
  })
})

describe('spans are written the way they were measured', () => {
  it('keeps a measured sub-second span as milliseconds, and only a zero as 0s', () => {
    expect(spanText(100)).toBe('100ms')
    expect(spanText(0)).toBe('0s')
    expect(spanText(189_400)).toBe('3m 9s')
    expect(spanText(-5_000)).toBe('−5s')
  })
})
