// AG-20 (#82). THE INSPECTOR CHARTS ARE DRAWN TWICE, NOT SCALED.
//
// THE DEFECT, measured on the live inspector on 2026-09-25. Every inspector
// chart was one SVG with a 640-unit viewBox under `width: 100%`, and the
// inspector is a 400-720px column (INSPECTOR in panes.ts), so the chart was
// scaled down to about 330-430px and its tick text -- `--t-micro`, 12px in
// the chart's own units -- rendered at about 6-8px. That is under the type
// scale's floor, on the one surface where the numbers are the point.
// design-system.md §7.2 already said what to do and nothing did it: "Charts
// are authored twice, not scaled."
//
// THE OWNER'S DECISION, 2026-09-25: author a narrow variant -- a second,
// narrow SVG with fewer ticks, switched by a container or media query, which
// is SSR-safe -- and scale nothing below the 12px floor.
//
// WHAT IS ASSERTED, and each is the mutation that turns it red:
//
//   * every chart root in the five inspector figures comes as a pair, one
//     `.is-wide` and one `.is-narrow`, with the narrow one authored narrower
//     (drop a variant, or ship one SVG);
//   * the narrow one draws no more ticks than the wide one, and on a span
//     that has room for them, fewer (pass the wide tick count to both);
//   * the sheet shows the wide one only when the chart's own box is at least
//     as wide as the wide drawing, shows the narrow one otherwise, and never
//     lets the narrow one shrink below the width it was drawn at -- so tick
//     text is never smaller than `--t-micro` (lower the container threshold,
//     or take the narrow drawing's `min-width` away);
//   * the figure IS the container the query asks about (drop the declaration:
//     a container query naming a container nothing establishes matches
//     nothing, silently);
//   * the checkpoint strip's off-page tray never eats its time axis, in
//     either drawing (let the tray grow with its count again).
//
// THE FIFTH FIGURE IS TOKEN SPEND, and leaving it out is how the first pass
// shipped one scaled chart: `TokenSpendChart` sits in the same Attempts panel
// as the phase bars, drawn through `TimeSeries` at one 640-unit width, and
// this file counted only the four figures that had been changed -- so the
// claim "every inspector chart is drawn twice" was checked against the
// charts it was already true of.
//
// SSR-SAFE BY CONSTRUCTION: both drawings are in the markup and the sheet
// picks one, so nothing here measures the DOM -- which is why the charts were
// scaled in the first place (charts/README.md, the recharts measurement).

import STYLES from '../styles.css?raw'
import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { AttemptDurations } from '../charts/AttemptPhases'
import { CheckpointStrip } from '../charts/CheckpointStrip'
import { DiffstatChart } from '../charts/Diffstat'
import { PeakMemoryChart } from '../charts/PeakMemory'
import { TokenSpendChart } from '../charts/TokenSpend'
import { cascade, type CascadeEnv } from './cssgate'
import { at, attempt, ev, task } from './runfixture'

const GIB = 1024 ** 3

/** The chart roots a figure draws: direct children, not visx's nested text <svg>s. */
function roots(fig: Element): SVGSVGElement[] {
  return [...fig.children].filter((c): c is SVGSVGElement => c.matches('svg.ctl-chart-svg'))
}

function viewBoxWidth(svg: Element): number {
  return Number((svg.getAttribute('viewBox') ?? '').split(/\s+/)[2])
}

function px(v: string | null | undefined): number {
  return Number(/^([\d.]+)px$/.exec(v ?? '')?.[1] ?? Number.NaN)
}

function won(el: Element, prop: string | readonly string[], env: CascadeEnv): string | null {
  const r = cascade(STYLES, el, prop, env)
  expect(r.unsupported, 'selectors the resolver could not evaluate').toEqual([])
  return r.winner?.value ?? null
}

/** The five inspector figures, each on a fixture that draws every one of its roots. */
function figures(): { name: string; fig: Element }[] {
  const a = attempt(1, {
    started_at: at(1),
    completed_at: at(20),
    peak_rss_bytes: 2 * GIB,
    checkpoints: ['ck_1', 'ck_2', 'ck_3'],
  })
  const peak = render(
    <PeakMemoryChart
      attempt={a}
      events={[
        ev('heartbeat', at(3.5), 'att_1', { peak_rss_bytes: GIB }),
        ev('heartbeat', at(6), 'att_1', { peak_rss_bytes: null }),
        ev('heartbeat', at(11), 'att_1', { peak_rss_bytes: 1.8 * GIB }),
      ]}
    />,
  ).container.querySelector('figure.ctl-peak')!

  // Two attempts, so the retry lollipop is drawn beside the phase bars.
  const phases = render(
    <AttemptDurations
      task={task({ state: 'SUCCEEDED', attempt_count: 2 })}
      attempts={[
        attempt(1, { created_at: at(1), started_at: at(1), completed_at: at(6), exit_code: 1 }),
        attempt(2, { created_at: at(9), started_at: at(9), completed_at: at(16), exit_code: 0 }),
      ]}
      events={[ev('lease_acquired', at(0), 'att_1'), ev('lease_acquired', at(8), 'att_2')]}
    />,
  ).container.querySelector('figure.ctl-phases')!

  const strip = render(
    <CheckpointStrip
      attempt={a}
      events={[
        ev('checkpoint_completed', at(3), 'att_1', { checkpoint_id: 'ck_1', uri: 'gs://a/ck_1', size_bytes: 1e6 }),
        ev('checkpoint_completed', at(9), 'att_1', { checkpoint_id: 'ck_2', uri: 'gs://a/ck_2', size_bytes: 4e6 }),
      ]}
    />,
  ).container.querySelector('figure.ctl-ckpt-strip')!

  const diff = render(
    <DiffstatChart
      commits={[
        { sha: 'aaaaaaaaaaaa', insertions: 120, deletions: 12, binary_files: 0 },
        { sha: 'bbbbbbbbbbbb', insertions: 4, deletions: 60, binary_files: 1 },
      ]}
      commitCount={2}
    />,
  ).container.querySelector('figure.ctl-diffstat')!

  // Three attempts, one with no cost, so the line, a measured dot and an
  // absence band are all drawn -- the case `TimeSeries` exists for.
  const spend = render(
    <TokenSpendChart
      attempts={[
        attempt(1, { created_at: at(1), cost_usd: 0.25 }),
        attempt(2, { created_at: at(9), cost_usd: null }),
        attempt(3, { created_at: at(16), cost_usd: 0.4 }),
      ]}
      profile="claude-code"
    />,
  ).container.querySelector('figure.ctl-chart[aria-label="Token spend, attempt by attempt"]')!

  return [
    { name: 'peak memory', fig: peak },
    { name: 'attempt phases', fig: phases },
    { name: 'checkpoint cadence', fig: strip },
    { name: 'diffstat', fig: diff },
    { name: 'token spend', fig: spend },
  ]
}

describe('each inspector chart is authored at two widths', () => {
  it('draws every root twice, one wide and one narrow, and nothing else', () => {
    const all = figures()
    expect(all.map((f) => f.name)).toHaveLength(5)
    for (const { name, fig } of all) {
      expect(fig, `${name} did not render`).not.toBeNull()
      const svgs = roots(fig)
      const wide = svgs.filter((s) => s.classList.contains('is-wide'))
      const narrow = svgs.filter((s) => s.classList.contains('is-narrow'))
      expect(wide.length, `${name} has no wide drawing`).toBeGreaterThan(0)
      expect(narrow.length, `${name} draws ${wide.length} wide and ${narrow.length} narrow`).toBe(wide.length)
      expect(wide.length + narrow.length, `${name} has a root that is neither`).toBe(svgs.length)
      wide.forEach((w, i) => {
        const n = narrow[i]!
        // Intrinsic size and viewBox agree, so 1 unit is 1px at the size drawn.
        expect(Number(w.getAttribute('width'))).toBe(viewBoxWidth(w))
        expect(Number(n.getAttribute('width'))).toBe(viewBoxWidth(n))
        expect(viewBoxWidth(n), `${name}: the narrow drawing is not narrower`).toBeLessThan(viewBoxWidth(w))
        // Both are the accessible chart, so both are groups (inspector.charts).
        expect(n.getAttribute('role')).toBe('group')
        expect(n.getAttribute('aria-label')).toBe(w.getAttribute('aria-label'))
        // FEWER TICKS, never more: the narrow axis is not the wide one squeezed.
        const ticks = (s: Element) => s.querySelectorAll('.ctl-chart-tick').length
        expect(ticks(n), `${name}: the narrow drawing carries more ticks`).toBeLessThanOrEqual(ticks(w))
      })
    }
  })

  it('gives the narrow peak-memory axis fewer ticks where the span has room for more', () => {
    const { fig } = figures().find((f) => f.name === 'peak memory')!
    const bottom = (s: Element) => s.querySelectorAll('.visx-axis-bottom .ctl-chart-tick').length
    const [wide] = roots(fig).filter((s) => s.classList.contains('is-wide'))
    const [narrow] = roots(fig).filter((s) => s.classList.contains('is-narrow'))
    expect(bottom(wide!), 'the wide axis drew too few ticks for this check to mean anything').toBeGreaterThan(2)
    expect(bottom(narrow!)).toBeLessThan(bottom(wide!))
  })
})

describe('the sheet picks one drawing and never shrinks it below the type floor', () => {
  it('shows the wide drawing only when the chart is at least that wide', () => {
    for (const { name, fig } of figures()) {
      const [wide] = roots(fig).filter((s) => s.classList.contains('is-wide'))
      const [narrow] = roots(fig).filter((s) => s.classList.contains('is-narrow'))
      const W = viewBoxWidth(wide!)
      // THE INSPECTOR'S OWN WIDTHS: 400px min, 480 default, 720 max, less
      // the drawer's gutters. One below the wide drawing, one at it.
      const below: CascadeEnv = { width: 1440, container: W - 1 }
      const at640: CascadeEnv = { width: 1440, container: W }
      expect(won(wide!, 'display', below), `${name}: the wide drawing shows in a box narrower than it`).toBe('none')
      expect(won(narrow!, 'display', below), `${name}: the narrow drawing is hidden where it is needed`).not.toBe('none')
      expect(won(wide!, 'display', at640), `${name}: the wide drawing never shows`).not.toBe('none')
      expect(won(narrow!, 'display', at640), `${name}: both drawings show at once`).toBe('none')
    }
  })

  it('never lets the narrow drawing shrink below the width it was drawn at', () => {
    for (const { name, fig } of figures()) {
      const [narrow] = roots(fig).filter((s) => s.classList.contains('is-narrow'))
      const N = viewBoxWidth(narrow!)
      const floor = px(won(narrow!, 'min-width', { width: 390, container: 300 }))
      expect(floor, `${name}: the narrow drawing may be scaled below ${N}px`).toBeGreaterThanOrEqual(N)
    }
  })

  it('draws tick text at --t-micro, which the floors above keep at 12px or more', () => {
    const tick = document.createElement('div')
    tick.innerHTML =
      '<figure class="ctl-chart"><svg class="ctl-chart-svg is-narrow"><g class="ctl-chart-tick"><text>1</text></g></svg></figure>'
    document.body.appendChild(tick)
    try {
      const text = tick.querySelector('text')!
      expect(won(text, ['font-size', 'font'], { width: 390, container: 300 })).toBe('var(--t-micro)')
      const root = /--t-micro:\s*([\d.]+)px/.exec(STYLES)
      expect(Number(root?.[1]), 'the micro step is not 12px; the floor above is measured against it').toBe(12)
    } finally {
      tick.remove()
    }
  })

  it('is asked of the chart\'s own box: the figure is the size container the query names', () => {
    const containers = new Set(
      [...STYLES.matchAll(/@container\s+([a-zA-Z][\w-]*)\s*\(min-width:\s*(\d+)px\)/g)].map((m) => m[1]),
    )
    for (const { name, fig } of figures()) {
      const declared = won(fig, 'container', { width: 1440 })
      expect(declared, `${name}'s figure is not a size container`).toMatch(/^[a-zA-Z][\w-]*\s*\/\s*inline-size$/)
      const containerName = declared!.split('/')[0]!.trim()
      expect(containers.has(containerName), `no @container query asks about \`${containerName}\``).toBe(true)
    }
  })
})

describe('the checkpoint strip keeps its time axis whatever the tray holds', () => {
  // THE DEFECT the first narrow drawing brought with it. The tray of
  // checkpoints whose events are off the page was `20 + 12n` units wide with
  // no upper bound, subtracted from the plot: 300 - 28 - (20 + 12n) is zero at
  // n = 21 and negative past it, and `linearScale` does not clamp, so the
  // track, the dots and the axis were drawn backwards into the margin with the
  // tray on top of them. The worker checkpoints every 120s, so an attempt of
  // about 42 minutes whose event read failed puts 21 in the tray. The 640
  // drawing broke the same way at 50.
  //
  // WHAT IS ASSERTED, in both drawings: the axis keeps a real width (at least
  // two fifths of the drawing), every placed dot sits on it, the tray starts
  // after it and ends inside the drawing, and every off-page checkpoint is
  // still accounted for -- drawn, or in the tray's count -- never dropped.
  //
  // MUTATION: size the tray by its count again (`20 + 12n`), or draw every
  // off-page checkpoint with no count.
  const OFF = 25
  function strip(): Element {
    const off = Array.from({ length: OFF }, (_, i) => `ck_${String(i + 1).padStart(2, '0')}`)
    const a = attempt(1, {
      started_at: at(1),
      completed_at: at(60),
      checkpoints: ['ck_on', ...off],
    })
    return render(
      <CheckpointStrip
        attempt={a}
        events={[
          ev('checkpoint_completed', at(5), 'att_1', { checkpoint_id: 'ck_on', uri: 'gs://a/ck_on', size_bytes: 1e6 }),
        ]}
      />,
    ).container.querySelector('figure.ctl-ckpt-strip')!
  }

  function translateX(el: Element | null): number {
    return Number(/translate\(\s*(-?[\d.]+)/.exec(el?.getAttribute('transform') ?? '')?.[1] ?? Number.NaN)
  }

  it('gives the time axis a real width and keeps every placed dot on it, in both drawings', () => {
    const svgs = roots(strip())
    expect(svgs.length, 'the strip drew no pair to check').toBe(2)
    for (const svg of svgs) {
      const W = viewBoxWidth(svg)
      const track = svg.querySelector('line.ctl-ckpt-track')
      expect(track, `${W}: no track`).not.toBeNull()
      const x1 = Number(track!.getAttribute('x1'))
      const x2 = Number(track!.getAttribute('x2'))
      expect(x1).toBe(0)
      expect(x2, `${W}: the time axis has no width, or runs backwards`).toBeGreaterThan(0)
      expect(x2, `${W}: the tray took more than three fifths of the drawing`).toBeGreaterThanOrEqual(W * 0.4)
      const dots = [...svg.querySelectorAll('[data-testid="ckpt-dot"]')]
      expect(dots.length, `${W}: the one placed checkpoint was not drawn`).toBe(1)
      for (const d of dots) {
        const cx = Number(d.getAttribute('cx'))
        expect(cx, `${W}: a placed dot is off the axis`).toBeGreaterThanOrEqual(0)
        expect(cx, `${W}: a placed dot is off the axis`).toBeLessThanOrEqual(x2)
      }
    }
  })

  it('starts the tray after the axis, ends it inside the drawing, and counts every checkpoint in it', () => {
    for (const svg of roots(strip())) {
      const W = viewBoxWidth(svg)
      const x2 = Number(svg.querySelector('line.ctl-ckpt-track')!.getAttribute('x2'))
      const plot = translateX(svg.querySelector('svg > g'))
      const tray = svg.querySelector('[data-testid="ckpt-tray"]')
      expect(tray, `${W}: no tray`).not.toBeNull()
      const trayX = translateX(tray)
      expect(trayX, `${W}: the tray sits on the axis`).toBeGreaterThan(x2)
      const rings = [...tray!.querySelectorAll('[data-testid="ckpt-offpage"]')]
      for (const r of rings) {
        const right = plot + trayX + Number(r.getAttribute('cx')) + Number(r.getAttribute('r'))
        expect(right, `${W}: a tray ring is drawn past the edge of the drawing`).toBeLessThanOrEqual(W)
      }
      const more = tray!.querySelector('[data-testid="ckpt-more"]')
      // The figure is the <text>; the <g> around it carries the sentence.
      const figure = more?.querySelector('text')?.textContent ?? ''
      const counted = more === null ? 0 : Number(/^\+(\d+)$/.exec(figure.trim())?.[1] ?? Number.NaN)
      expect(rings.length + counted, `${W}: an off-page checkpoint was dropped rather than counted`).toBe(OFF)
      if (more !== null) {
        expect(more.getAttribute('role')).toBe('img')
        expect(more.getAttribute('aria-label') ?? '').toMatch(new RegExp(`${counted} more`))
      }
    }
  })
})
