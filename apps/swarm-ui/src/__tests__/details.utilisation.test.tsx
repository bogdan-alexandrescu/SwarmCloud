// CPU AND MEMORY OVER TIME ON DETAILS (lane LIVE-UTIL-UI; part of #1689).
//
// THE OWNER, 2026-10-20: Details showed an attempt's CPU limit, the peak of
// its samples and its mean, and "those will always show maximum when in fact
// the pods do not run at max cpu the entire time". LIVE-UTIL-API (#1696) now
// serves `GET /v1/tasks/{id}/utilisation`: per attempt, newest first, the
// limits, the throttling figures and one point every 5 s of CPU cores, memory
// bytes and the share of the window's CFS periods that were throttled. These
// read the chart and the panel that draw it back off the rendered DOM:
//
//   1. the chart draws every measured point and each series' limit as a line,
//      and says the one-line summary in words;
//   2. the windows the container was CPU-throttled are marked, merged where
//      they touch, and a window whose throttling was not read is not marked;
//   3. nothing measured is said in words -- "no samples yet" while it runs,
//      "not measured" once it ended -- and no axis or zero is drawn;
//   4. the panel reads the route while the task runs, once more when it
//      finishes, and then stops.

import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { UtilisationAttempt, UtilisationPoint, UtilisationRead } from '../utilisation'
import { at } from './runfixture'

const api = vi.hoisted(() => ({ loadUtilisation: vi.fn() }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

const { UtilisationChart } = await import('../charts/Utilisation')
const { UtilisationPanel, UTILISATION_POLL_MS } = await import('../UtilisationPanel')

const GIB = 1024 ** 3

/** One 5-second point, `i` windows after the first, at minute 0. */
function point(i: number, cpu: number | null, mem: number | null, thr: number | null): UtilisationPoint {
  return { at: at(i / 12), cpu_cores: cpu, memory_bytes: mem, throttled_share: thr }
}

function attemptOf(points: UtilisationPoint[], over: Partial<UtilisationAttempt> = {}): UtilisationAttempt {
  return {
    attempt_id: 'att_1',
    generation: 1,
    cpu_limit_cores: 4,
    memory_limit_bytes: 8 * GIB,
    throttled_seconds: 3.2,
    peak_throttled_share: 0.5,
    mean_throttled_share: 0.09,
    points,
    ...over,
  }
}

/** Every subpath of a `d` attribute, as its vertices. */
function vertices(d: string): Array<{ x: number; y: number }> {
  return [...d.matchAll(/[ML]\s*(-?[\d.]+),(-?[\d.]+)/g)].map((m) => ({
    x: Number.parseFloat(m[1] as string),
    y: Number.parseFloat(m[2] as string),
  }))
}

/** The wide drawing: a count read off one drawing, not both (charts/README.md). */
function wide(container: HTMLElement): SVGSVGElement {
  const svg = container.querySelector<SVGSVGElement>('svg.is-wide')
  if (svg === null) throw new Error('no wide drawing')
  return svg
}

const FIVE = [
  point(0, 4, 2 * GIB, 0),
  point(1, 1, 2.5 * GIB, 0.5),
  point(2, 1, 3 * GIB, 0.4),
  point(3, 1, 3 * GIB, 0),
  point(4, 1.5, 3.5 * GIB, 0.2),
]

describe('the utilisation chart draws the points and the limits', () => {
  // MUTATION: draw the CPU line through the first point only, or drop the
  // limit line, or scale the CPU axis to the data's max so the 4-core limit
  // is off the plot -- the vertex count, the limit line or its y fails.
  it('draws one vertex per measured point and each limit as a line at the top of its axis', () => {
    const { container } = render(<UtilisationChart attempt={attemptOf(FIVE)} live={false} />)
    const svg = wide(container)
    const cpu = svg.querySelector('[data-testid="util-cpu-line"]')
    const mem = svg.querySelector('[data-testid="util-mem-line"]')
    expect(vertices(cpu?.getAttribute('d') ?? '')).toHaveLength(5)
    expect(vertices(mem?.getAttribute('d') ?? '')).toHaveLength(5)

    const cpuLimit = svg.querySelector('[data-testid="util-cpu-limit"]')
    const memLimit = svg.querySelector('[data-testid="util-mem-limit"]')
    expect(cpuLimit?.getAttribute('data-value')).toBe('4')
    expect(memLimit?.getAttribute('data-value')).toBe(String(8 * GIB))
    // The limit is the largest value on each axis, so it sits at the top of
    // its plot and every point is at or under it.
    const cpuTop = Number(cpuLimit?.getAttribute('y1'))
    for (const v of vertices(cpu?.getAttribute('d') ?? '')) expect(v.y).toBeGreaterThanOrEqual(cpuTop)
    const memTop = Number(memLimit?.getAttribute('y1'))
    for (const v of vertices(mem?.getAttribute('d') ?? '')) expect(v.y).toBeGreaterThan(memTop)
  })

  // MUTATION: count an unread point as 0 cores in the mean, or take the
  // share at the limit over every point rather than the measured ones.
  it('says the CPU summary in one line, from the measured points', () => {
    const { container } = render(<UtilisationChart attempt={attemptOf([...FIVE, point(5, null, null, null)])} live={false} />)
    const line = container.querySelector('[data-testid="util-summary"]')
    expect(line?.textContent).toBe('CPU: mean 1.7 of 4 cores · at the limit 20% of the time · throttled 9%')
  })

  // MUTATION: join the line across a point with no CPU reading.
  it('breaks the line at a point the worker could not read, and marks it absent', () => {
    const points = [point(0, 1, GIB, 0), point(1, 1.2, GIB, 0), point(2, null, GIB, 0), point(3, 2, GIB, 0), point(4, 2.2, GIB, 0)]
    const { container } = render(<UtilisationChart attempt={attemptOf(points)} live={false} />)
    const d = wide(container).querySelector('[data-testid="util-cpu-line"]')?.getAttribute('d') ?? ''
    expect(d.split('M').filter((s) => s.trim() !== '')).toHaveLength(2)
    expect(wide(container).querySelectorAll('[data-testid="util-cpu-absent"]')).toHaveLength(1)
  })
})

describe('throttled windows are marked', () => {
  // MUTATION: mark nothing, mark every window, or mark a window whose
  // throttling was not read (null) as throttled.
  it('shades each throttled window, merging the ones that touch', () => {
    const points = [
      point(0, 1, GIB, 0),
      point(1, 4, GIB, 0.5),
      point(2, 4, GIB, 0.4),
      point(3, 1, GIB, 0),
      point(4, 4, GIB, 0.2),
      point(5, 1, GIB, null),
      point(6, 1, GIB, 0),
    ]
    const { container } = render(<UtilisationChart attempt={attemptOf(points)} live={false} />)
    const bands = [...wide(container).querySelectorAll('[data-testid="util-throttled"]')]
    expect(bands).toHaveLength(2)
    const x = (b: Element) => Number(b.getAttribute('x'))
    const w = (b: Element) => Number(b.getAttribute('width'))
    // The first band covers windows 1 and 2 (minute 0 to 2/12), the second
    // window 4 alone: twice as wide as the second.
    expect(w(bands[0] as Element)).toBeCloseTo(2 * w(bands[1] as Element), 5)
    expect(x(bands[0] as Element)).toBeLessThan(x(bands[1] as Element))
    expect(container.querySelector('[data-testid="util-summary"]')?.textContent).toContain('throttled 9%')
  })
})

describe('nothing measured is said in words', () => {
  // MUTATION: render the chart over an empty series, or say "0 cores", or
  // say "not measured" for a running attempt that has not beaten yet.
  it('says "no samples yet" for a running attempt with no points', () => {
    const { container } = render(<UtilisationChart attempt={attemptOf([])} live={true} />)
    expect(container.querySelector('svg')).toBeNull()
    expect(container.textContent).toContain('no samples yet')
    expect(container.textContent).not.toMatch(/\b0(\.0)? (of|cores)/)
  })

  it('says "not measured" for a finished attempt with no points', () => {
    const { container } = render(<UtilisationChart attempt={attemptOf([])} live={false} />)
    expect(container.querySelector('svg')).toBeNull()
    expect(container.textContent).toContain('not measured')
  })

  it('says "not measured" when every point came back unread, and draws no zeros', () => {
    const { container } = render(
      <UtilisationChart attempt={attemptOf([point(0, null, null, null), point(1, null, null, null)])} live={false} />,
    )
    expect(container.querySelector('svg')).toBeNull()
    expect(container.textContent).toContain('not measured')
    expect(container.textContent).not.toMatch(/mean 0\b/)
  })

  it('says "not measured" when there is no attempt at all', () => {
    const { container } = render(<UtilisationChart attempt={null} live={false} />)
    expect(container.querySelector('svg')).toBeNull()
    expect(container.textContent).toContain('not measured')
  })
})

describe('the panel polls while the task runs and stops when it finishes', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    api.loadUtilisation.mockReset()
    const data: UtilisationRead = { task_id: 'tsk_util', read_at: at(1), attempts: [attemptOf(FIVE)] }
    api.loadUtilisation.mockResolvedValue({ status: 'ok', data, fetchedAt: Date.now() })
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  // MUTATION: keep the interval running after the task is terminal, or poll
  // only once while it runs, or skip the final read at the finish.
  it('reads on the live cadence, once more at the finish, then never again', async () => {
    const view = render(<UtilisationPanel taskId="tsk_util" live={true} />)
    await act(async () => {})
    expect(api.loadUtilisation).toHaveBeenCalledTimes(1)
    expect(api.loadUtilisation).toHaveBeenLastCalledWith('tsk_util')

    await act(async () => {
      await vi.advanceTimersByTimeAsync(UTILISATION_POLL_MS)
    })
    expect(api.loadUtilisation).toHaveBeenCalledTimes(2)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(UTILISATION_POLL_MS)
    })
    expect(api.loadUtilisation).toHaveBeenCalledTimes(3)

    // The task finishes: one read for the last beat's points, then none.
    view.rerender(<UtilisationPanel taskId="tsk_util" live={false} />)
    await act(async () => {})
    expect(api.loadUtilisation).toHaveBeenCalledTimes(4)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(UTILISATION_POLL_MS * 6)
    })
    expect(api.loadUtilisation).toHaveBeenCalledTimes(4)
  })

  // MUTATION: put the summary only inside the folded chart, so Details shows
  // nothing about use until the disclosure is opened.
  it('says the CPU summary on the disclosure itself, so it is read unopened', async () => {
    const { container } = render(<UtilisationPanel taskId="tsk_util" live={true} />)
    await act(async () => {})
    const head = container.querySelector('[data-testid="util-headline"]')
    expect(head?.closest('summary')).not.toBeNull()
    // QA1 overview-agents-24/25: the summary says what the chart adds, time at
    // the limit and throttling; the samples' mean is the chart's own caption.
    expect(head?.textContent).toBe('CPU and memory over time · at the limit 20% of the time · throttled 9%')
  })

  // MUTATION: start an interval for a task that was already finished.
  it('reads a finished task once and does not poll it', async () => {
    render(<UtilisationPanel taskId="tsk_util" live={false} />)
    await act(async () => {})
    await act(async () => {
      await vi.advanceTimersByTimeAsync(UTILISATION_POLL_MS * 6)
    })
    expect(api.loadUtilisation).toHaveBeenCalledTimes(1)
  })

  // MUTATION: draw the chart (or zeros) when the read failed.
  it('says the read failed in words rather than drawing anything', async () => {
    api.loadUtilisation.mockResolvedValue({
      status: 'error',
      error: { kind: 'upstream_degraded', httpStatus: 503, code: 'unavailable', message: 'down' },
    })
    const { container } = render(<UtilisationPanel taskId="tsk_util" live={false} />)
    await act(async () => {})
    expect(container.querySelector('svg')).toBeNull()
    expect(container.textContent).toContain('not read')
  })
})
