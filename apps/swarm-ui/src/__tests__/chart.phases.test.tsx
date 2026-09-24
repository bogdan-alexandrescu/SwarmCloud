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
//   * a measured sub-second span printed as "0s";
//   * `attempt.created_at` taken as admission. The worker REWRITES it: its
//     `record_attempt_start` replaces the scheduler's attempt document with a
//     non-merge `.set()` that writes `created_at = utcnow()` beside
//     `started_at = utcnow()` (control.py). On every attempt whose worker
//     started, the two are equal, and a chart that read `created_at` as
//     admission drew a recorded 0s cold start and coloured the whole
//     container start as "queue";
//   * an attempt read as still going because `completed_at` is null, when the
//     task has already let go of its lease -- a quota park, a failed dispatch
//     and a reconciler reclaim all leave `completed_at` null for ever;
//   * a total stated over the attempt DOCUMENTS that came back rather than
//     the attempts the task records.
//
// THE FIXTURES ARE IN PRODUCTION SHAPE. A started attempt has `created_at`
// equal to `started_at`, and admission exists only as its `lease_acquired`
// event. The first version of this file built attempts with an admission
// instant in `created_at` -- a shape the API never serves -- and every test
// here passed on it while the chart was wrong on real data.

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

/**
 * An attempt AS THE API SERVES IT once its worker has started: `created_at`
 * is the worker's start, because `record_attempt_start` rewrote it. `start`
 * and `end` are minutes from the fixture epoch; a null end is an attempt with
 * no `finish()` behind it.
 */
function worked(n: number, start: number, end: number | null, over: Partial<AttemptRow> = {}): AttemptRow {
  return attempt(n, {
    created_at: at(start),
    started_at: at(start),
    completed_at: end === null ? null : at(end),
    exit_code: end === null ? null : 0,
    ...over,
  })
}

/** An attempt admitted and never started: its document was never rewritten. */
function admittedOnly(n: number, admitted: number): AttemptRow {
  return attempt(n, {
    created_at: at(admitted),
    started_at: null,
    completed_at: null,
    exit_code: null,
    execution_name: null,
  })
}

/** The scheduler's `lease_acquired` event: the one surviving record of admission. */
function leased(n: number, m: number): TaskEvent {
  return ev('lease_acquired', at(m), `att_${n}`)
}

afterEach(() => {
  vi.useRealTimers()
})

describe('redesign-v2 §9, the measured example, in the shape the API serves it', () => {
  // task_b208fc8542724268b5f4, the first real claude-code run:
  //   03:46:05 dispatched · 03:49:14 starting (+3m 09s) · 03:49:14 running
  //   (+0.1s) · 03:49:32 succeeded (+18s).
  // Those four instants are §9's. The attempt document is the one the worker
  // leaves: `created_at` and `started_at` both 03:49:14, because
  // `record_attempt_start` rewrote the scheduler's document when it started.
  //
  // The submission and `lease_acquired` instants are NOT in §9 and are
  // constructed here: submitted 2s before admission, and admission 400ms
  // before the backend call returned -- the order the scheduler writes them
  // in (create_attempt, lease_acquired, then dispatch; loop.py).
  const T = (s: string) => `2026-09-22T${s}Z`
  const t = task({ created_at: T('03:46:02.600') })
  const a = attempt(1, {
    created_at: T('03:49:14.000'),
    started_at: T('03:49:14.000'),
    completed_at: T('03:49:32.000'),
  })
  const events = [
    ev('lease_acquired', T('03:46:04.600'), 'att_1'),
    ev('dispatched', T('03:46:05.000'), 'att_1'),
    ev('starting', T('03:49:14.000'), 'att_1'),
    ev('running', T('03:49:14.100'), 'att_1'),
    ev('succeeded', T('03:49:32.000'), 'att_1'),
  ]

  it('draws the wait for a container and the agent as two different segments', () => {
    const { container } = draw(t, [a], events)
    const queue = seg(container, 1, 'queue')
    const cold = seg(container, 1, 'cold')
    const run = seg(container, 1, 'run')
    expect(cold.getAttribute('data-kind')).toBe('closed')
    expect(run.getAttribute('data-kind')).toBe('closed')
    expect(Number(cold.getAttribute('data-ms'))).toBe(189_400)
    expect(Number(run.getAttribute('data-ms'))).toBe(18_000)
    expect(cold.querySelector('title')?.textContent).toBe('cold start 3m 9s')
    expect(run.querySelector('title')?.textContent).toBe('run 18s')
    // The queue is submission to ADMISSION -- two seconds -- and not
    // submission to the worker's start, which would colour the whole
    // container start as time spent waiting in line.
    expect(queue.getAttribute('data-kind')).toBe('closed')
    expect(Number(queue.getAttribute('data-ms'))).toBe(2_000)

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

describe('admission comes from the lease_acquired event, because the worker rewrites created_at', () => {
  it('draws a started attempt’s cold start as not measured when its lease_acquired event is off the page, never as a recorded 0s', () => {
    const t = task({ created_at: at(-1) })
    const a = worked(1, 3, 3.3)
    const { container } = draw(t, [a], [ev('succeeded', at(3.3), 'att_1')])
    const cold = seg(container, 1, 'cold')
    expect(cold.getAttribute('data-kind')).toBe('absent')
    expect(cold.querySelector('title')?.textContent).not.toBe('cold start 0s')
    expect(cold.getAttribute('aria-label') ?? '').toMatch(/lease_acquired/)
    // The queue ends at admission, which is unknown, so it has no end either
    // -- NOT the four minutes from submission to the worker's start.
    expect(seg(container, 1, 'queue').getAttribute('data-kind')).toBe('absent')
    // The run is still measured: both of its ends are the worker's own.
    expect(container.querySelector('[data-testid="run-figure"]')?.textContent).toBe('18s')
    expect(container.querySelector('[data-testid="work-sum"]')?.textContent).toContain('ran 18s over 1 of 1')
  })

  it('does not place a run whose admission is unknown anywhere on the admission axis', () => {
    // Where the run starts relative to admission IS the cold start, and that
    // is exactly what is unknown. Drawing the run from the admission rule
    // would be the §B5 defect again: an unknown segment given zero width.
    const t = task({ created_at: at(-1) })
    const a = worked(1, 3, 3.3)
    const { container } = draw(t, [a], [])
    expect(seg(container, 1, 'run').querySelector('rect'), 'an unplaced run was drawn at a position').toBeNull()
    expect(phaseExtent(phasesFor(t, [a], []).rows)).toEqual({ lo: 0, hi: 0, degenerate: true })
  })

  it('takes a never-started attempt’s admission from its document, which nothing rewrote', () => {
    const t = task({ state: 'FAILED', created_at: at(-1) })
    const p = phasesFor(t, [admittedOnly(1, 0)], [])
    const r = p.rows[0]!
    expect(r.queue.kind).toBe('closed')
    if (r.queue.kind === 'closed') expect(r.queue.ms).toBe(1 * MIN)
    expect(r.cold.kind).toBe('open')
    expect(r.neverRan).toBe(true)
  })

  it('ends a retry’s queue at its lease_acquired event, not at the worker’s start', () => {
    // Attempt 1 ended at T+10m; the task was put back in line at T+15m and
    // admitted again at T+20m; the container reported in at T+23m. Queue is
    // 5 minutes and cold start 3 -- not a queue of 8 and a cold start of 0.
    const t = task({ attempt_count: 2, created_at: at(-2) })
    const attempts = [worked(1, 1, 10), worked(2, 23, 30)]
    const p = phasesFor(t, attempts, [leased(1, 0), ev('ready', at(15), null), leased(2, 20)])
    const q = p.rows[1]!.queue
    const c = p.rows[1]!.cold
    expect(q.kind).toBe('closed')
    if (q.kind === 'closed') expect(q.ms).toBe(5 * MIN)
    expect(c.kind).toBe('closed')
    if (c.kind === 'closed') expect(c.ms).toBe(3 * MIN)
  })
})

describe('an interval with no recorded end is drawn open, never closed', () => {
  // RUNNING and holding THIS attempt's lease: the one shape in which an
  // attempt with no finish time is still going.
  const t = task({ state: 'RUNNING', current_lease_id: 'lse_1' })
  const a = worked(1, 1, null)
  const events = [
    leased(1, 0),
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
    // Attempt 1 was superseded: its worker's last heartbeat is at T+5m and the
    // reconciler fenced it 35 minutes later. "Ran at least until the fence"
    // would stretch a dead attempt by the whole reclaim delay.
    const t2 = task({ state: 'RUNNING', attempt_count: 2, current_lease_id: 'lse_2' })
    const dead = worked(1, 1, null)
    const next = worked(2, 46, null)
    const p = phasesFor(t2, [dead, next], [
      leased(1, 0),
      ev('heartbeat', at(5), 'att_1', { peak_rss_bytes: 1 }),
      ev('generation_fenced', at(40), 'att_1'),
      ev('lease_released', at(40.5), 'att_1'),
      leased(2, 45),
    ])
    const run = p.rows[0]!.run!
    expect(run.kind).toBe('open')
    if (run.kind !== 'open') return
    expect(run.atLeastMs).toBe(4 * MIN)
    expect(run.live).toBe(false)
  })

  it('never adds an open attempt’s lower bound into the total', () => {
    const t3 = task({ state: 'RUNNING', attempt_count: 3, current_lease_id: 'lse_3' })
    const attempts = [worked(1, 1, 10), worked(2, 21, 28), worked(3, 41, null)]
    const events = [
      leased(1, 0),
      leased(2, 20),
      ev('ready', at(35), null),
      leased(3, 40),
      ev('heartbeat', at(46), 'att_3', {}),
    ]
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

describe('an attempt whose task no longer holds its lease is over, whatever completed_at says', () => {
  it('reads a parked attempt as over: not live, and never "Running"', () => {
    // CONTRACT invariant 4, the quota path: the worker checkpoints, parks the
    // task, releases the lease and exits. `park()` writes the TASK and never
    // touches the attempt document, so its `completed_at` stays null for ever.
    const t = task({ state: 'PARKED', park_reason: 'QUOTA_EXHAUSTED', current_lease_id: null, updated_at: at(41) })
    const events = [leased(1, 0), ev('heartbeat', at(40), 'att_1', {}), ev('parked', at(41), 'att_1')]
    const { container } = draw(t, [worked(1, 1, null)], events)
    const run = seg(container, 1, 'run')
    expect(run.getAttribute('data-kind')).toBe('open')
    expect(run.getAttribute('data-live')).toBe('no')
    expect(run.getAttribute('aria-label') ?? '').not.toMatch(/Running/)
    expect(Number(run.getAttribute('data-at-least-ms'))).toBe(39 * MIN)
  })

  it('reads a failed dispatch as over: admitted, never started, and no container coming', () => {
    // `return_to_ready_after_failed_dispatch` (scheduler store.py) releases
    // the lease and puts the task back to READY. No container will ever
    // report in for this attempt.
    const t = task({ state: 'READY', current_lease_id: null, updated_at: at(0.2) })
    const events = [leased(1, 0), ev('lease_released', at(0.2), null, { reason: 'dispatch_failed' })]
    const { container } = draw(t, [admittedOnly(1, 0)], events)
    const cold = seg(container, 1, 'cold')
    expect(cold.getAttribute('data-live')).toBe('no')
    expect(cold.getAttribute('aria-label') ?? '').not.toMatch(/not started yet/)
    expect(container.querySelector('[data-testid="run-figure"]')?.textContent).toBe('never ran')
    expect(container.querySelector('[data-testid="work-sum"]')?.textContent).toContain('1 never ran')
  })

  it('reads a reclaimed attempt as over before its replacement is admitted', () => {
    // The reconciler's repair moves the task to READY and clears
    // `current_lease_id` without touching the attempt document.
    const t = task({ state: 'READY', current_lease_id: null, updated_at: at(40) })
    const p = phasesFor(t, [worked(1, 1, null)], [
      leased(1, 0),
      ev('heartbeat', at(5), 'att_1', {}),
      ev('generation_fenced', at(40), 'att_1'),
      ev('ready', at(40), null),
    ])
    const run = p.rows[0]!.run!
    expect(run.kind).toBe('open')
    if (run.kind !== 'open') return
    expect(run.live).toBe(false)
    expect(run.atLeastMs).toBe(4 * MIN)
  })

  it('does not take a task read older than the attempt as evidence the attempt ended', () => {
    // The page reads the task and the attempts in parallel. A task read that
    // landed just BEFORE admission shows READY with no lease, and an attempts
    // read that landed just after it returns the new attempt. That task
    // document is older than the attempt, so it says nothing about it.
    const t = task({ state: 'READY', current_lease_id: null, updated_at: at(-0.5) })
    const p = phasesFor(t, [admittedOnly(1, 0)], [leased(1, 0)])
    const r = p.rows[0]!
    expect(r.neverRan).toBe(false)
    expect(r.cold.kind === 'open' && r.cold.live).toBe(true)
  })
})

describe('an unknown segment removes itself and moves nothing else', () => {
  const t = task({ attempt_count: 2, created_at: at(-2) })
  const attempts = [worked(1, 1, 10), worked(2, 23, 30)]
  // Both admissions are on the page; the `ready` that re-queued attempt 2 is not.
  const leases = [leased(1, 0), leased(2, 20)]

  it('draws a retry’s queue as absent when its ready event is not on the page', () => {
    const { container } = draw(t, attempts, leases)
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
    const { container } = draw(t, attempts, leases)
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
    const p = phasesFor(t, attempts, [...leases, ev('ready', at(15), null)])
    const q = p.rows[1]!.queue
    expect(q.kind).toBe('closed')
    if (q.kind === 'closed') expect(q.ms).toBe(5 * MIN)
  })

  it('refuses an interval whose end is recorded before its start', () => {
    const skewed = [worked(1, 5, 4)]
    const { container } = draw(task(), skewed, [leased(1, 0)])
    const run = seg(container, 1, 'run')
    expect(run.getAttribute('data-kind')).toBe('absent')
    const line = container.querySelector('[data-testid="work-sum"]')!
    expect(line.textContent).toContain('1 unreadable')
    expect(line.getAttribute('data-partial')).toBe('yes')
  })
})

describe('the sum counts the attempts the task records, not the documents that came back', () => {
  // The task counts three attempts and the attempts read returned two: the
  // route is newest-first and capped, and a document can be missing. A total
  // over the two is a total over a partial response.
  const t = task({ attempt_count: 3, created_at: at(-1) })
  const attempts = [worked(2, 21, 28), worked(3, 41, 50)]
  const events = [leased(2, 20), leased(3, 40)]

  it('says over 2 of 3, and partial, when two of three documents came back', () => {
    const { container } = draw(t, attempts, events)
    const line = container.querySelector('[data-testid="work-sum"]')!
    expect(line.textContent).toContain('ran 16m 0s over 2 of 3')
    expect(line.textContent).toContain('1 not returned')
    expect(line.getAttribute('data-partial')).toBe('yes')
  })

  it('does not start the oldest RETURNED attempt’s queue at submission, since it may not be the first', () => {
    // Stated from submission, the oldest document's "queue" here would be
    // 21 minutes -- attempt 1's whole life folded into it.
    const { container } = draw(t, attempts, events)
    expect(seg(container, 1, 'queue').getAttribute('data-kind')).toBe('absent')
  })
})

describe('the retry lollipop', () => {
  it('draws one stem per attempt: a dot at a recorded run, an arrowhead at a lower bound, a ring for never ran', () => {
    const t = task({ state: 'RUNNING', attempt_count: 4, current_lease_id: 'lse_4' })
    const attempts = [
      worked(1, 1, 10),
      // Admitted and never started, then superseded: it did no work.
      admittedOnly(2, 12),
      worked(3, 21, 24),
      worked(4, 31, null),
    ]
    const events = [leased(1, 0), leased(2, 12), leased(3, 20), leased(4, 30), ev('heartbeat', at(33), 'att_4', {})]
    const { container } = draw(t, attempts, events)
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
    const { container } = draw(task(), [worked(1, 1, 10)], [leased(1, 0)])
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
