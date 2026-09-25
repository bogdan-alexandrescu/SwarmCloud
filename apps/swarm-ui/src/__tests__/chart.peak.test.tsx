// Peak RSS reached by T+n: a step line through a running maximum, read back
// off the `d` attribute of the path it drew.
//
// THE DEFECTS THESE EXIST FOR:
//
//   * a diagonal between two readings -- a smoothed line claims the peak rose
//     steadily, which nothing measured (redesign-v2 §4 row 4: never smoothed);
//   * a stroke across a heartbeat that carried no reading;
//   * the end-of-attempt figure joined to the line, which draws a stroke across
//     heartbeats that may exist and be off the page;
//   * `elapsed_seconds` used as the x position, which the worker writes as 0
//     before the agent child exists.

import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { EVENT_PAGE_LIMIT } from '../api'
import { PeakMemoryChart, peakReadings } from '../charts/PeakMemory'
import { absent, measured, stepAfter } from '../charts/series'
import type { TaskEvent } from '../types'
import { MIN, at, attempt, ev } from './runfixture'

const GIB = 1024 ** 3

/** Every subpath of a `d` attribute, as its vertices. */
function subpaths(d: string): Array<Array<{ x: number; y: number }>> {
  return d
    .split('M')
    .filter((s) => s.trim() !== '')
    .map((s) =>
      [...`M${s}`.matchAll(/[ML]\s*(-?[\d.]+),(-?[\d.]+)/g)].map((m) => ({
        x: Number.parseFloat(m[1] as string),
        y: Number.parseFloat(m[2] as string),
      })),
    )
}

function heartbeats(): TaskEvent[] {
  return [
    ev('heartbeat', at(3.5), 'att_1', { peak_rss_bytes: 1 * GIB, elapsed_seconds: 90 }),
    // THE WORKER'S OWN SHAPE for a heartbeat whose sampler had nothing yet:
    // `peak_rss_bytes: null` (lifecycle.py, `usage.peak_rss_bytes if usage
    // else None`). An absent reading, not zero bytes.
    ev('heartbeat', at(6), 'att_1', { peak_rss_bytes: null, elapsed_seconds: 240 }),
    ev('heartbeat', at(8.5), 'att_1', { peak_rss_bytes: 1.5 * GIB, elapsed_seconds: 390 }),
    ev('heartbeat', at(11), 'att_1', { peak_rss_bytes: 1.8 * GIB, elapsed_seconds: 540 }),
  ]
}

describe('the step geometry', () => {
  it('holds each reading until the next, and adds no corner beside an absence', () => {
    const v = stepAfter([
      measured(0, 'a', 1),
      measured(10, 'b', 2),
      absent(20, 'c', 'no reading'),
      measured(30, 'd', 3),
      measured(40, 'e', 4),
    ])
    expect(v.map((p) => [p.at, p.measured ? p.value : null])).toEqual([
      [0, 1],
      [10, 1], // held
      [10, 2],
      [20, null],
      [30, 3],
      [40, 3], // held
      [40, 4],
    ])
  })
})

describe('Peak RSS reached by T+n', () => {
  const a = attempt(1, { started_at: at(1), completed_at: at(20), peak_rss_bytes: 2 * GIB })

  it('is titled as a running maximum, never as memory in use', () => {
    const { container } = render(<PeakMemoryChart attempt={a} events={heartbeats()} />)
    expect(container.querySelector('.ctl-chart-title')?.textContent).toBe('Peak RSS reached by T+n')
  })

  it('draws only horizontal and vertical strokes, broken at the reading that is missing', () => {
    const { container } = render(<PeakMemoryChart attempt={a} events={heartbeats()} />)
    const path = container.querySelector('[data-testid="step-line"]')
    expect(path, 'no step line was drawn').not.toBeNull()
    const parts = subpaths(path!.getAttribute('d') ?? '')

    // The lone first reading, then the run after the absence: two subpaths.
    // One subpath would mean the stroke crossed the heartbeat with no reading.
    expect(parts).toHaveLength(2)
    for (const part of parts) {
      for (let i = 1; i < part.length; i++) {
        const p = part[i - 1]!
        const q = part[i]!
        const flat = Math.abs(p.y - q.y) < 1e-6
        const upright = Math.abs(p.x - q.x) < 1e-6
        expect(flat || upright, `a diagonal from ${p.x},${p.y} to ${q.x},${q.y}`).toBe(true)
      }
    }
    // ONE DRAWING'S BANDS. Each width is drawn separately (AG-20), so the
    // absence is a band in the wide drawing and a band in the narrow one;
    // the claim is one band per drawing, read off the wide one.
    expect(container.querySelectorAll('svg.is-wide [data-testid="absent-band"]')).toHaveLength(1)
    expect(container.querySelectorAll('svg.is-narrow [data-testid="absent-band"]')).toHaveLength(1)
  })

  it('draws the end-of-attempt figure as its own mark, not joined to the line', () => {
    const { container } = render(<PeakMemoryChart attempt={a} events={heartbeats()} />)
    const exit = container.querySelector('[data-testid="exit-mark"]')
    expect(exit, 'the at-exit figure is not drawn').not.toBeNull()
    const exitX = Number((exit!.getAttribute('d') ?? '').match(/^M(-?[\d.]+)/)?.[1])
    const lastX = Math.max(
      ...subpaths(container.querySelector('[data-testid="step-line"]')!.getAttribute('d') ?? '')
        .flat()
        .map((p) => p.x),
    )
    expect(lastX).toBeLessThan(exitX)
    expect(container.querySelector('[data-testid="peak-cov"]')?.textContent).toContain('at exit 2.00 GiB')
  })

  it('places a reading by when it was taken, not by the child clock the worker zeroes', () => {
    // `elapsed_seconds: 0` is what the worker writes before the agent child
    // exists, whatever the time. This heartbeat was taken 3 minutes in.
    const { points } = peakReadings(a, [
      ev('heartbeat', at(4), 'att_1', { peak_rss_bytes: GIB, elapsed_seconds: 0 }),
    ])
    expect(points).toHaveLength(1)
    expect(points[0]!.at).toBe(3 * MIN)
  })

  it('draws nothing at all for an attempt with no heartbeat on the page', () => {
    const { container } = render(<PeakMemoryChart attempt={a} events={[]} />)
    expect(container.innerHTML).toBe('')
  })

  it('says the page is full when it is, because the line then ends where the page does', () => {
    const filler = Array.from({ length: EVENT_PAGE_LIMIT - 4 }, (_, i) =>
      ev('checkpoint_started', at(1 + i / 1000), 'att_1'),
    )
    const { container } = render(
      <PeakMemoryChart attempt={a} events={[...heartbeats(), ...filler]} />,
    )
    const flag = container.querySelector('.ctl-chart-flag')
    expect(flag?.textContent).toBe('page full')
    expect(flag?.getAttribute('aria-label') ?? '').toMatch(/may exist/)

    const short = render(<PeakMemoryChart attempt={a} events={heartbeats()} />)
    expect(short.container.querySelector('.ctl-chart-flag')).toBeNull()
  })
})
