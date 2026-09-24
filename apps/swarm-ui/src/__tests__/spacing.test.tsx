// THE SPACING GATE. Fifteen routes, two themes, measured rather than eyeballed.
//
// WHY THIS SUITE EXISTS. The owner's report was "we should look into spacing
// between sections or between vertical columns, elements should not touch
// dividers etc." That is a class of defect rather than a bug, and the class had
// already shipped once: the density pass tightened vertical rhythm across the
// app, and the capacity row let its NAME column collapse to zero while the BAR
// kept a 90px floor, so pool labels rendered "claude-code · 2…". That one was
// found by looking at it. The rest were not going to be.
//
// WHAT IT MEASURES AND WHAT IT CANNOT. `spaceprobe.ts` carries the physics and
// the thresholds, each with the reason it is the number it is. The one thing
// worth repeating here: jsdom has no layout engine, so nothing below is a box
// intersection -- it is the CSS that decides the boxes, read off elements this
// app actually rendered, through the sheet it actually ships, with `var()`
// substituted so the shorthands resolve. A rule that matches nothing produces
// no finding; a rule that is deleted changes the numbers.
//
// HOW TO USE IT WHEN IT FAILS. The failure message names the element, the side,
// what was measured and the floor. Either the CSS is wrong, or the floor is --
// and if it is the floor, change it in `spaceprobe.ts` WITH the reason, next to
// the value, rather than adding the element to a list of exceptions here.

import STYLES from '../styles.css?raw'
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'
import { act, render } from '@testing-library/react'

import { App, SECTIONS } from '../App'
import {
  probe,
  resolveSheet,
  resolveVars,
  stripComments,
  tokenTables,
  type Finding,
  type Report,
} from './spaceprobe'

/**
 * Every route the rail can reach, DERIVED rather than restated.
 *
 * NOT A SAMPLE. `test_nav_headings_agree.py` already holds that every
 * `SectionBody` case has a tab and every tab has a case, so this is the
 * product's whole surface; a screen added without being swept is a screen
 * nothing measures, which is the hole this repository keeps producing.
 *
 * IT USED TO BE A HAND-WRITTEN COPY of the same fifteen routes, under that
 * same comment. On 2026-09-24 two sections were renamed -- `agents` to `work`,
 * `pools` to `capacity` -- and the copy was not, so every one of its nine
 * stale routes resolved through SECTION_ALIASES instead of failing. The sweep
 * still ran, still reported zero findings, and examined 798 shapes where it
 * had examined 1295. Five hundred shapes went unmeasured and the only thing
 * that noticed was the floor assertion at the bottom of this file, which was
 * written for exactly this and is the reason it was caught at all.
 *
 * A list that has to be kept in step by hand is a list that will not be. This
 * one cannot go stale: adding a tab adds a route, and renaming a section
 * renames one.
 */
const ROUTES = SECTIONS.flatMap((s) => s.tabs.map((t) => `${s.id}/${t.id}`))

type Theme = 'dark' | 'light'

/**
 * Let a screen finish loading, on a clock this test controls.
 *
 * WALL-CLOCK SLEEPS MADE THIS SWEEP NON-DETERMINISTIC, and the failure was
 * silent, which is the part worth recording. The fixtures in `api.ts` answer
 * after 30-220ms; a 300ms sleep lands close enough to the longest of those
 * that one run had a screen's table drawn and the next still had its skeleton,
 * and the sweep reported 71 findings instead of 129 with nothing to say that
 * it had looked at less. "The numbers before and after are the evidence" is
 * worth nothing if the numbers move on their own.
 *
 * Polling for "the markup stopped changing" did not fix it and failed the same
 * way: a skeleton that has not started changing yet is indistinguishable from
 * a screen that has finished.
 *
 * Fake timers remove the race instead of widening it. 700ms is advanced in one
 * step: past every fixture delay, and short of the 1s and 5s refresh intervals
 * in `Agents.tsx`, `Overview.tsx`, `App.tsx` and `Dock.tsx`, so each screen
 * sees a fixed number of ticks rather than a function of how busy the machine
 * is.
 *
 * Only the timer functions and `Date` are faked. The default `toFake` list
 * also takes `performance` and `process.hrtime`, which makes a stopwatch in
 * this file read back exactly the number of milliseconds it just advanced --
 * 15 routes x 700ms, every time, whatever the run actually cost. That is how
 * the first attempt to find where this sweep's two minutes were going
 * reported "render phase 10500ms, probe phase 0ms".
 */
const CLOCK_MS = 700

async function settle(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(CLOCK_MS)
  })
}

function merge(parts: Report[]): Report {
  const seen = new Map<string, Finding>()
  for (const p of parts)
    for (const f of p.findings) {
      const key = `${f.kind}|${f.where}|${f.side}`
      const prior = seen.get(key)
      // The WORST instance of a shape, not the first: a fix has to clear the
      // hardest case, and reporting the easiest would understate the gap.
      if (prior === undefined || f.measured < prior.measured) seen.set(key, f)
    }
  const u = new Map<string, Report['unresolved'][number]>()
  for (const p of parts) for (const x of p.unresolved) u.set(`${x.what}|${x.value}`, x)
  return {
    findings: [...seen.values()].sort(
      (a, b) =>
        a.kind.localeCompare(b.kind) || a.where.localeCompare(b.where) || a.side.localeCompare(b.side),
    ),
    unresolved: [...u.values()],
    centred: [...new Set(parts.flatMap((p) => p.centred))].sort(),
    elements: parts.reduce((n, p) => n + p.elements, 0),
    bordered: [],
  }
}

/**
 * Render every route once, and read the result through BOTH cascades.
 *
 * Three things in here are not the obvious version, and each replaced one that
 * failed quietly:
 *
 *   SNAPSHOTS, NOT MOUNTED APPS. Every mounted `App` listens for `hashchange`,
 *   so setting the hash for route 15 navigated the fourteen already rendered
 *   there too. The sweep still passed -- it just measured the same screen
 *   fifteen times, and the count fell from 97 to 15 with nothing said.
 *   ONE SHEET SWAP PER THEME, NOT PER ROUTE. Replacing the `<style>` text
 *   makes jsdom re-parse 3,200 rules and drop every cascade it had computed.
 *   Thirty of those took two minutes.
 *   ONE SNAPSHOT ATTACHED AT A TIME. jsdom's `getComputedStyle` cost rises
 *   with the size of the whole document, not just the element: with all
 *   fifteen screens attached the probe phase took 127 seconds for the same
 *   1,068 elements. Detached elements compute no style at all, so each
 *   snapshot is attached for exactly as long as it is being read.
 *
 * And the LIGHT pass measures only the elements the dark pass found a border
 * on. One rule here depends on the theme -- the contrast of a divider against
 * its ground -- and only a bordered element can produce one; whether a border
 * exists does not change with the theme, because the light block in
 * `styles.css` redefines colour tokens and nothing else.
 */
async function sweep(): Promise<Record<Theme, Report>> {
  const sheets: Record<Theme, string> = {
    dark: resolveSheet(STYLES, 'dark'),
    light: resolveSheet(STYLES, 'light'),
  }
  const tables = { dark: tokenTables(STYLES).dark, light: tokenTables(STYLES).light }

  const snapshots: { el: HTMLElement; local: { el: Element; source: string }[] }[] = []
  for (const route of ROUTES) {
    window.location.hash = `#${route}`
    const { container, unmount } = render(<App />)
    await settle()
    const snap = container.cloneNode(true) as HTMLElement
    unmount()
    // A screen's own `<style>` element, resolved like the main sheet. Overview
    // shipped one (`OVERVIEW_CSS`) until U8 folded it into styles.css, and no
    // screen ships one now -- stylesheet.gate.test.ts holds that. The loop
    // stays so that one which comes back is resolved rather than read as "no
    // gap declared", a false finding on whatever screen it lands on.
    snapshots.push({
      el: snap,
      local: [...snap.querySelectorAll('style')].map((el) => ({ el, source: el.textContent ?? '' })),
    })
  }

  const style = document.createElement('style')
  document.head.appendChild(style)

  const dark: Report[] = []
  const light: Report[] = []

  style.textContent = sheets.dark
  const borders: Element[][] = []
  for (const s of snapshots) {
    for (const { el, source } of s.local) el.textContent = resolveVars(stripComments(source), tables.dark)
    document.body.appendChild(s.el)
    const r = probe(s.el)
    dark.push(r)
    borders.push(r.bordered)
    s.el.remove()
  }

  style.textContent = sheets.light
  snapshots.forEach((s, i) => {
    for (const { el, source } of s.local) el.textContent = resolveVars(stripComments(source), tables.light)
    document.body.appendChild(s.el)
    light.push(probe(s.el, borders[i]))
    s.el.remove()
  })

  style.remove()
  window.location.hash = ''
  return { dark: merge(dark), light: merge(light) }
}

function lines(r: Report): string[] {
  return r.findings.map((f) => `${f.kind} ${f.where} [${f.side}] ${f.measured} < ${f.floor} — ${f.detail}`)
}

/**
 * Boxes whose sideways inset comes from being centred in a fixed width.
 *
 * The probe refuses to guess a glyph's advance width without a font, so it
 * names these rather than exempting them silently. Each entry needs a reason,
 * and the reason has to be that the geometry is right, not that the finding
 * was inconvenient.
 */
const CENTRED_SIDEWAYS: string[] = [
  // EMPTY, AND WHAT MOVED IS THE ENCODING RATHER THAN THE GEOMETRY.
  //
  // This list held `button.ctl-q-glyph [left]` and `[right]`: a 20px circle
  // around one 12px `?`, whose sideways inset is (20 − 2 − advance) / 2 and
  // therefore unmeasurable without a font, so the probe named it instead of
  // exempting it silently.
  //
  // §B5.3 of `styles.css` took the circle's BORDER away — six of these shipped
  // on the Overview, one beside every panel title, and they were the largest
  // single non-panel contributor to the box count the owner's verdict is
  // about. The glyph is now a `--surface-2` disc with `border: 0`, and this
  // probe measures ink-to-BORDER: with no border drawn there is no inset to
  // fail to measure, so the two entries are gone rather than moved.
  //
  // It stays as a named, typed constant rather than being inlined as `[]`,
  // because the assertion it feeds is an equality: the next box that centres
  // its content inside a fixed width lands here and has to be argued for, the
  // way these two were.
]

describe('spacing', () => {
  beforeAll(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
  })
  afterAll(() => {
    vi.useRealTimers()
  })

  it('finds nothing touching, nothing under the divider floors, no corner off the scale', async () => {
    const r = await sweep()

    // PRINTED BEFORE ANYTHING IS ASSERTED. Four assertions follow and the
    // first one to fail hides the rest, which is the wrong shape for a probe:
    // the useful artefact is the whole picture, and "the numbers before and
    // after" cannot be read off a run that stopped at the first difference.
    for (const [theme, one] of [['DARK', r.dark], ['LIGHT', r.light]] as const) {
      const byKind = new Map<string, number>()
      for (const f of one.findings) byKind.set(f.kind, (byKind.get(f.kind) ?? 0) + 1)
      console.log(
        [
          `${theme}: ${one.elements} shapes examined, ${one.findings.length} findings` +
            `, ${one.unresolved.length} unresolved, ${one.centred.length} centred`,
          ...[...byKind].sort().map(([k, n]) => `  ${k} (${n})`),
          ...lines(one).map((l) => `    ${l}`),
          ...one.unresolved.map((u) => `    unresolved ${u.what} = ${u.value}`),
        ].join('\n'),
      )
    }

    // THE PROBE HAS TO HAVE LOOKED AT SOMETHING. A sweep that renders nothing
    // -- a broken fixture, a route list that stopped matching the router --
    // produces zero findings, which is indistinguishable from a clean app.
    // This is the guard that keeps "no findings" meaning what it says.
    expect(r.dark.elements, 'the sweep examined almost nothing').toBeGreaterThan(800)

    // Nothing the probe could not turn into a number. A unit it has no px for,
    // a gradient where a colour was expected, or a control still wearing the
    // browser's own `buttonface` border all land here, and all of them are
    // things somebody has to look at rather than things to skip.
    expect(r.dark.unresolved.map((u) => `${u.what} = ${u.value}`)).toEqual([])
    expect(r.light.unresolved.map((u) => `${u.what} = ${u.value}`)).toEqual([])

    expect(r.dark.centred).toEqual(CENTRED_SIDEWAYS)
    expect(r.light.centred).toEqual(CENTRED_SIDEWAYS)

    expect(lines(r.dark)).toEqual([])
    expect(lines(r.light)).toEqual([])
  }, 240000)
})
