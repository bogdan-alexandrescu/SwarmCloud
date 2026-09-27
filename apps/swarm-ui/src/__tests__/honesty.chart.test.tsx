// The charting honesty rules, asserted against the RENDERED SVG.
//
// WHY THESE ARE RENDER TESTS AND NOT SOURCE GREPS. A previous wave neutered a
// guard in this repository with `false &&` and the suite stayed green, because
// the test only checked that a string appeared in the source. Every claim
// below is read back off the DOM the component actually produced: the `d`
// attribute of the line, the `cy` of a dot, the tick labels the axis emitted.
//
// THE MUTATION THESE EXIST FOR. `TimeSeries.tsx` hard-codes
// `defined={(p) => p.measured}` on the line. Delete that one accessor -- which
// is exactly what happens when a new chart is written straight against the
// library -- and the first test in this file fails: the path stops having two
// subpaths and gains a vertex on the baseline at the absent point's x. That
// vertex IS the bug this product exists to prevent, drawn.

import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { TimeSeries } from '../charts/TimeSeries'
import { absent, measured, type ChartPoint } from '../charts/series'
import { usdText } from '../charts/TokenSpend'

const T0 = Date.UTC(2026, 8, 22, 10, 0, 0)
const HOUR = 3_600_000

/**
 * measured, MEASURED ZERO, absent, measured — the series that breaks naive
 * charting.
 *
 * The largest value is 1.17 rather than a round 1.20 on purpose: 1.20 is
 * already a multiple of d3's default tick step, so a scale rounded outward
 * with `.nice()` would leave it unchanged and the axis test below could not
 * tell the two apart. A fixture that cannot fail the mutation it guards is a
 * fixture that proves nothing.
 */
function mixed(): ChartPoint[] {
  return [
    measured(T0, 'attempt 1', 1.17),
    measured(T0 + HOUR, 'attempt 2', 0),
    absent(T0 + 2 * HOUR, 'attempt 3', 'this attempt finished and recorded no usage.'),
    measured(T0 + 3 * HOUR, 'attempt 4', 0.8),
  ]
}

function chart(points: ChartPoint[], absentCopy = 'an absent measurement, not a free run.') {
  return render(
    <TimeSeries
      points={points}
      title="Token spend, attempt by attempt"
      noun="attempt"
      format={usdText}
      absentCopy={absentCopy}
      zero="anchored"
    />,
  )
}

/** Every vertex the line actually put on the page. */
function vertices(d: string): Array<{ x: number; y: number }> {
  return [...d.matchAll(/[ML]\s*(-?[\d.]+),(-?[\d.]+)/g)].map((m) => ({
    x: Number.parseFloat(m[1] as string),
    y: Number.parseFloat(m[2] as string),
  }))
}

/** How many times the stroke was LIFTED: one `M` starts each subpath. */
function subpaths(d: string): number {
  return (d.match(/M/g) ?? []).length
}

function lineD(container: HTMLElement): string {
  const path = container.querySelector('path.ctl-chart-line')
  expect(path, 'the chart drew no line at all').not.toBeNull()
  const d = path?.getAttribute('d') ?? ''
  expect(d, 'the line has no geometry').not.toEqual('')
  return d
}

describe('the chart wrapper, on a series of measured and absent points', () => {
  it('does not draw an absent point as zero, and does not cross it with the line', () => {
    const { container } = chart(mixed())

    // Where the absence was drawn, taken from the rendered band rather than
    // recomputed here: the assertion has to be about the same geometry the
    // reader sees.
    const band = container.querySelector('[data-testid="absent-band"][data-label="attempt 3"]')
    expect(band, 'the absent attempt was not drawn at all').not.toBeNull()
    const bandX = Number.parseFloat(band?.getAttribute('x') ?? 'NaN')
    const bandW = Number.parseFloat(band?.getAttribute('width') ?? 'NaN')
    const absentX = bandX + bandW / 2
    expect(Number.isFinite(absentX)).toBe(true)

    const d = lineD(container)

    // THE STROKE WAS LIFTED. Two runs of measured points, so two subpaths.
    expect(subpaths(d), `the line is one unbroken stroke: ${d}`).toBe(2)

    // NO VERTEX AT THE ABSENCE. With the `defined` accessor removed the line
    // gains one here, on the baseline, reading as a measured $0.00.
    const vs = vertices(d)
    expect(vs.length, `the line has the wrong number of vertices: ${d}`).toBe(3)
    for (const v of vs) {
      expect(
        Math.abs(v.x - absentX),
        `the line has a vertex at the absent attempt's position (${v.x}, ${v.y})`,
      ).toBeGreaterThan(1)
    }

    // And the absence got no dot: a mark at a height is a value.
    expect(
      container.querySelector('[data-testid="measured-dot"][data-label="attempt 3"]'),
    ).toBeNull()
  })

  it('draws a measured zero as a point on the zero rule, visibly unlike an absence', () => {
    const { container } = chart(mixed())

    const zeroDot = container.querySelector('[data-testid="measured-dot"][data-label="attempt 2"]')
    expect(zeroDot, 'the measured zero was not drawn').not.toBeNull()
    expect(zeroDot?.getAttribute('data-value')).toBe('0')

    // It sits ON the zero rule -- that is what makes it read as a measurement
    // of zero rather than as the bottom of the plot.
    const rule = container.querySelector('[data-testid="zero-rule"]')
    expect(rule, 'no zero rule was drawn under an anchored axis').not.toBeNull()
    expect(zeroDot?.getAttribute('cy')).toBe(rule?.getAttribute('y1'))

    // Shape, not colour: a ring, where a measured non-zero is filled and an
    // absence is stripes.
    expect(zeroDot?.getAttribute('class')).toContain('is-zero')
    expect(zeroDot?.querySelector('title')?.textContent).toBe('attempt 2 — $0.00, measured')

    // The absence, by contrast, carries its own reason and no figure.
    const band = container.querySelector('[data-testid="absent-band"][data-label="attempt 3"]')
    expect(band?.querySelector('title')?.textContent).toBe(
      'attempt 3 — not measured. this attempt finished and recorded no usage.',
    )
  })

  it('never renders a total without the coverage it was computed over', () => {
    const { container } = chart(mixed())
    const caption = container.querySelector('.ctl-chart-cov')?.textContent ?? ''

    // 1.17 + 0 + 0.80 over the three that reported, of four.
    expect(caption).toContain('$1.97')
    expect(caption).toContain('3 of 4 attempts that reported')
    expect(container.querySelector('.ctl-chart-cov')?.getAttribute('data-partial')).toBe('yes')

    // And the absences are named under the plot, with the class-level reason.
    const note = container.querySelector('.ctl-chart-note')?.textContent ?? ''
    expect(note).toContain('1 of 4 attempts reported no value')
    expect(note).toContain('an absent measurement, not a free run.')
  })

  it('says so plainly when the series is complete, rather than claiming coverage it lacks', () => {
    const { container } = chart([
      measured(T0, 'attempt 1', 1.2),
      measured(T0 + HOUR, 'attempt 2', 0.8),
    ])
    const caption = container.querySelector('.ctl-chart-cov')?.textContent ?? ''
    expect(caption).toContain('all 2 attempts')
    expect(caption).not.toContain('of 2')
    expect(container.querySelector('.ctl-chart-cov')?.getAttribute('data-partial')).toBe('no')
    expect(container.querySelector('.ctl-chart-note')).toBeNull()
  })

  it('draws no axis and no line when nothing was measured', () => {
    const { container } = chart(
      [
        absent(T0, 'attempt 1', 'this attempt has not finished.'),
        absent(T0 + HOUR, 'attempt 2', 'this attempt never started.'),
      ],
      'no attempt reported a cost.',
    )

    // No plot at all: an axis over an empty domain is a range the data does
    // not support, and every library invents one.
    expect(container.querySelector('svg')).toBeNull()
    expect(container.querySelector('path.ctl-chart-line')).toBeNull()
    expect(container.textContent).toContain('Nothing was measured')
    expect(container.textContent).toContain('None of the 2 attempts reported a value')

    // NO FIGURE ANYWHERE except the count of attempts, which is a measurement
    // of the series rather than of the quantity.
    const digits = (container.textContent ?? '').match(/\d+/g) ?? []
    expect(digits).toEqual(['2'])
  })

  it('never labels the value axis past the largest measurement', () => {
    const { container } = chart(mixed())
    const labels = [...container.querySelectorAll('.ctl-chart-axis .ctl-chart-tick text')]
      .map((t) => t.textContent ?? '')
      .filter((t) => t.startsWith('$'))
      .map((t) => Number.parseFloat(t.slice(1)))

    expect(labels.length, 'the value axis emitted no labels').toBeGreaterThan(1)
    // 1.17 is the largest measurement; 0 is the anchor. Nothing outside.
    expect(Math.max(...labels)).toBe(1.17)
    expect(Math.min(...labels)).toBe(0)

    // AND THE PLOT ITSELF ENDS THERE. Labels alone are not enough: a scale
    // rounded outward with d3's `.nice()` keeps these tick values and still
    // leaves headroom above the data, so the largest measurement stops being
    // the top of the picture and the series reads as smaller than it is.
    // The largest measurement must sit ON the top edge of the plot area.
    const topDot = container.querySelector('[data-testid="measured-dot"][data-label="attempt 1"]')
    expect(
      Number.parseFloat(topDot?.getAttribute('cy') ?? 'NaN'),
      'the largest measurement is not at the top of the plot: the axis has room the data does not support',
    ).toBe(0)
  })

  it('draws a lone measurement between two absences, and draws no line for it', () => {
    const { container } = chart([
      absent(T0, 'attempt 1', 'this attempt never started.'),
      measured(T0 + HOUR, 'attempt 2', 0.42),
      absent(T0 + 2 * HOUR, 'attempt 3', 'this attempt has not finished.'),
    ])

    // A line needs two consecutive measurements. There are none, so there is
    // no line -- rather than a stroke from one absence to the other.
    expect(container.querySelector('path.ctl-chart-line')).toBeNull()
    const dot = container.querySelector('[data-testid="measured-dot"][data-label="attempt 2"]')
    expect(dot, 'the only measurement in the series was not drawn').not.toBeNull()
    // Counted in ONE drawing: the chart draws each width separately (AG-20),
    // and the first root is the wide one.
    const drawing = container.querySelector('svg.ctl-chart-svg')
    expect(drawing!.querySelectorAll('[data-testid="absent-band"]')).toHaveLength(2)
  })

  it('plots the absences at their real instants, not at the edges', () => {
    const { container } = chart(mixed())
    const band = container.querySelector('[data-testid="absent-band"][data-label="attempt 3"]')
    expect(band?.getAttribute('data-at')).toBe(String(T0 + 2 * HOUR))

    // Between the second and fourth attempts, where it happened.
    const x2 = Number.parseFloat(
      container
        .querySelector('[data-testid="measured-dot"][data-label="attempt 2"]')
        ?.getAttribute('cx') ?? 'NaN',
    )
    const x4 = Number.parseFloat(
      container
        .querySelector('[data-testid="measured-dot"][data-label="attempt 4"]')
        ?.getAttribute('cx') ?? 'NaN',
    )
    const bx =
      Number.parseFloat(band?.getAttribute('x') ?? 'NaN') +
      Number.parseFloat(band?.getAttribute('width') ?? 'NaN') / 2
    expect(bx).toBeGreaterThan(x2)
    expect(bx).toBeLessThan(x4)
  })
})
