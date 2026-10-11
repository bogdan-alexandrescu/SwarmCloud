// THE SPACING GATE'S SWEEP. Every route, two themes, measured rather than
// eyeballed -- walked in slices, one `spacing.<group>.test.tsx` per slice.
//
// WHY IT IS SLICED (owner decision 2026-10-09, the CI-FAST follow-up). Vitest
// runs a file inside one worker, so a single file that walks every route is a
// floor under the whole UI job no shard count can lower. Measured over 17 PR
// runs on 2026-10-09: vitest shard 1/4 took a median 469 s against 91-125 s
// for the other three and finished last on every UI run, and this sweep was
// 1,089 of shard 1's 1,670 s in a full local run. Each slice file is now its
// own unit of work for the scheduler.
//
// WHAT HOLDS THE SLICES TOGETHER. `spacing.test.tsx` asserts that every route
// in ROUTES is in exactly one of SPACING_GROUPS, so a tab added without a group
// fails there instead of going unmeasured, and that the per-slice floors add up
// to the 800 shapes the single sweep required. Everything else -- the walk, the
// merge, the exemptions, the assertions -- lives here, once.
//
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
export const ROUTES: readonly string[] = SECTIONS.flatMap((s) => s.tabs.map((t) => `${s.id}/${t.id}`))

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
/**
 * A SECTION'S OWN SHEET (`src/styles/*.css`, imported by its screen) IS PART
 * OF WHAT SHIPS. Vitest's `css: true` injects it into the test document
 * UNRESOLVED, so without this every `var()` in it read as "unresolved" here.
 * Resolved like the main sheet and placed BEFORE it -- the order the app loads
 * them, since main.tsx imports App (and so every screen's sheet) first. The
 * resolved copy comes after the injected one in the document, so it is the
 * one the cascade picks.
 */
const SECTION_SHEETS = Object.values(
  import.meta.glob<string>('../styles/*.css', { query: '?raw', import: 'default', eager: true }),
)
const SHEET = [...SECTION_SHEETS, STYLES].join('\n')

async function sweep(routes: readonly string[]): Promise<Record<Theme, Report>> {
  const sheets: Record<Theme, string> = {
    // Every shipped sheet: SHEET holds src/styles/*.css (the canonical
    // components' rules in components.css among them) as well as styles.css.
    dark: resolveSheet(SHEET, 'dark'),
    light: resolveSheet(SHEET, 'light'),
  }
  const tables = { dark: tokenTables(SHEET).dark, light: tokenTables(SHEET).light }

  const snapshots: { el: HTMLElement; local: { el: Element; source: string }[] }[] = []
  for (const route of routes) {
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

/**
 * FINDINGS THE OWNER DECIDED AGAINST, BY NAME. Each entry is one decision,
 * with its date and its reason, and matches exactly the boxes it was made
 * about -- never a kind of finding app-wide. The header's advice still holds
 * everywhere else: fix the CSS or argue the floor in `spaceprobe.ts`.
 */
interface Exemption {
  name: string
  why: string
  matches: (f: Finding) => boolean
}

/** The classes on a finding's box, read back off its signature (`tag.a.b`). */
function classesOf(f: Finding): string[] {
  return f.where.split('.').slice(1)
}

/**
 * THE APP FRAME: the spine and the panel touch. The picked mock-up
 * (navigation.html, Variant 2) draws them with no gutter, the panel's own
 * edge being the only seam, so `.sk-side` lays them out with no gap
 * (owner decision 2026-10-01). Only that one boundary: any other pair of
 * surfaces that touch is still a finding.
 */
const APP_FRAME: Exemption = {
  name: 'app frame (spine | panel)',
  why: 'the picked mock-up draws the spine and the panel touching',
  matches: (f) => f.kind === 'surfaces-touch' && f.where === 'div.sk-side' && f.side === 'column',
}

/**
 * THE NAV CHROME'S HAIRLINES: the mock-up's own border colours. The owner
 * picked the Navigation mock-up's hairlines -- `--f-ln` (#dde5ee light,
 * #1d3049 dark; `--sk-ln` here) on the panel edge, the tenant block, the
 * page-kids rule, the footer rule, the admin tag and the theme switch, and
 * `--f-acl` (`--sk-acl`) on the environment pill and copy-id -- over the 3:1
 * divider rule, on 2026-10-01
 * (https://claude.ai/artifact/PbJhVd2CPmY2EgBz1NTAov). Exactly those
 * selectors, and only their divider contrast.
 */
const NAV_CHROME_HAIRLINE_CLASSES = ['sk-panel', 'sk-tenant', 'sk-kids', 'sk-pfoot', 'sk-adm', 'sk-theme', 'sk-pill', 'sk-cp']
const NAV_CHROME_HAIRLINES: Exemption = {
  name: 'nav chrome hairlines',
  why: "the owner picked the mock-up's hairlines over the 3:1 rule on 2026-10-01",
  matches: (f) =>
    f.kind === 'divider-under-floor' && classesOf(f).some((c) => NAV_CHROME_HAIRLINE_CLASSES.includes(c)),
}

const EXEMPTIONS: readonly Exemption[] = [APP_FRAME, NAV_CHROME_HAIRLINES]

function lines(r: Report): string[] {
  return r.findings
    .filter((f) => !EXEMPTIONS.some((e) => e.matches(f)))
    .map((f) => `${f.kind} ${f.where} [${f.side}] ${f.measured} < ${f.floor} — ${f.detail}`)
}

/**
 * Boxes whose sideways inset comes from being centred in a fixed width.
 *
 * The probe refuses to guess a glyph's advance width without a font, so it
 * names these rather than exempting them silently. Each entry needs a reason,
 * and the reason has to be that the geometry is right, not that the finding
 * was inconvenient.
 */
export const CENTRED_SIDEWAYS: string[] = [
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

export interface SpacingGroup {
  name: string
  routes: readonly string[]
  /**
   * The slice's share of the old `> 800` floor: about half of the dark shapes
   * the slice examined on 2026-10-09 (work 257, runs 334, submit 297, setup
   * 184, capacity 290, accounts 328, admin 249 -- 1,939 in all, against 2,009
   * for the single walk; SPACING_GROUPS says where the 70 went), so a screen
   * that stops rendering its body trips it, while a screen growing or
   * shrinking a few cards does not. They sum to 930, over the 800 the single sweep held.
   */
  floor: number
}

/**
 * THE SLICES, BY ROUTE GROUP, NAMED RATHER THAN DERIVED. Deriving them (say,
 * one per section) would make "every route is in exactly one group" true by
 * construction and so prove nothing; listing them makes a new tab a failure in
 * `spacing.test.tsx` until somebody decides which slice measures it. Grouped
 * by what the screens are for, and balanced by measured cost so no slice
 * becomes the new long pole.
 *
 * A WALK CARRIES STATE FROM ONE ROUTE TO THE NEXT, AND A SLICE IS A SHORTER
 * WALK, so the slices examine fewer shapes than the single sweep did: 1,939
 * dark and 459 light against 2,009 and 466 (2026-10-09). Measured by diffing
 * every route's markup between the two walks, the gap is repetition, not lost
 * coverage. Reading Workflows fills the Work rail's "Recent" switcher
 * (`RecentWorkflows` in Spine.tsx, `.sk-recent`), which renders on Workflows
 * itself and then on every Work route after it in the same walk. The single
 * sweep measured it on the eight Work routes after Workflows; the slices do on
 * two of them, because `runs` walks Workflows first so it is also measured
 * beside Runs and Timeline. The rest is one fixture pool row that moves
 * between Pools' two tables with the clock. Every tag-and-class pair the
 * single walk rendered is rendered by some slice.
 */
export const SPACING_GROUPS: readonly SpacingGroup[] = [
  { name: 'work', routes: ['overview/now', 'work/running'], floor: 120 },
  { name: 'runs', routes: ['work/workflows', 'work/runs', 'work/timeline'], floor: 160 },
  { name: 'submit', routes: ['work/new', 'work/new-workflow', 'work/new-issue'], floor: 140 },
  // Automate › Schedules (docs/schedules.md §6.1) rides with Work's setup pages:
  // one route, and a slice is a file of its own (the slice test).
  { name: 'setup', routes: ['work/repositories', 'work/setup', 'work/access', 'automate/schedules'], floor: 90 },
  { name: 'capacity', routes: ['capacity/pools', 'capacity/catalogue', 'capacity/profiles'], floor: 140 },
  { name: 'accounts', routes: ['capacity/holders', 'capacity/accounts', 'capacity/quota'], floor: 160 },
  { name: 'admin', routes: ['admin/limits', 'admin/tenants', 'admin/counts', 'admin/people'], floor: 120 },
]

/** The old single sweep's floor, which the slices' floors must add up to. */
export const SPACING_FLOOR_TOTAL = 800

/** Register one slice's sweep. Each `spacing.<group>.test.tsx` is this call. */
export function spacingGroup(name: string): void {
  const group = SPACING_GROUPS.find((g) => g.name === name)
  if (group === undefined) throw new Error(`no spacing group named ${name}`)

  describe(`spacing: ${group.name}`, () => {
    beforeAll(() => {
      vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
    })
    afterAll(() => {
      vi.useRealTimers()
    })

    it('finds nothing touching, nothing under the divider floors, no corner off the scale', async () => {
      const r = await sweep(group.routes)

      // PRINTED BEFORE ANYTHING IS ASSERTED. Four assertions follow and the
      // first one to fail hides the rest, which is the wrong shape for a probe:
      // the useful artefact is the whole picture, and "the numbers before and
      // after" cannot be read off a run that stopped at the first difference.
      for (const [theme, one] of [['DARK', r.dark], ['LIGHT', r.light]] as const) {
        const byKind = new Map<string, number>()
        for (const f of one.findings) byKind.set(f.kind, (byKind.get(f.kind) ?? 0) + 1)
        console.log(
          [
            `${group.name} ${theme}: ${one.elements} shapes examined, ${one.findings.length} findings` +
              `, ${one.unresolved.length} unresolved, ${one.centred.length} centred`,
            ...[...byKind].sort().map(([k, n]) => `  ${k} (${n})`),
            ...lines(one).map((l) => `    ${l}`),
            ...one.findings.flatMap((f) => {
              const e = EXEMPTIONS.find((x) => x.matches(f))
              return e === undefined ? [] : [`    exempt (${e.name}: ${e.why}) ${f.kind} ${f.where} [${f.side}]`]
            }),
            ...one.unresolved.map((u) => `    unresolved ${u.what} = ${u.value}`),
          ].join('\n'),
        )
      }

      // THE PROBE HAS TO HAVE LOOKED AT SOMETHING. A sweep that renders nothing
      // -- a broken fixture, a route list that stopped matching the router --
      // produces zero findings, which is indistinguishable from a clean app.
      // This is the guard that keeps "no findings" meaning what it says. It was
      // one `> 800` over every route; it is now one floor per slice, and
      // `spacing.test.tsx` holds that the floors sum to at least 800, so all
      // slices passing still means more than 800 shapes were examined.
      expect(r.dark.elements, 'the sweep examined almost nothing').toBeGreaterThan(group.floor)

      // Nothing the probe could not turn into a number. A unit it has no px for,
      // a gradient where a colour was expected, or a control still wearing the
      // browser's own `buttonface` border all land here, and all of them are
      // things somebody has to look at rather than things to skip.
      expect(r.dark.unresolved.map((u) => `${u.what} = ${u.value}`)).toEqual([])
      expect(r.light.unresolved.map((u) => `${u.what} = ${u.value}`)).toEqual([])

      // Per slice, and equal to the single sweep's assertion only while the
      // list is empty: an entry argued back onto it must be keyed to the slice
      // whose routes render that box, or every other slice fails on it.
      expect(r.dark.centred).toEqual(CENTRED_SIDEWAYS)
      expect(r.light.centred).toEqual(CENTRED_SIDEWAYS)

      expect(lines(r.dark)).toEqual([])
      expect(lines(r.light)).toEqual([])
      // THE TIMEOUT'S HISTORY BELOW IS THE SINGLE SWEEP'S, over every route; a
      // slice takes a fraction of it. Kept at 900 s so the split changes what a
      // file holds, not what it is allowed to take.
      // 360 s, not 240: the sweep measured 200-211 s alone and in a full run on
      // the SwarmCloud container (2026-10-02, after O1 and H1 added cards to
      // Overview and Help), and one loaded full run went past 240 s. A timeout
      // is not what this test asserts; the findings are.
      // 480 s, not 360: on 2026-10-04 the same container measured 349 s alone
      // for origin/main 8455be3 and 383 s alone after lane U12 (same 1742
      // shapes, the same exempt findings) -- the run page's stacked cards and
      // the Submit card's recent reads cost about 10%, and 360 s left no room.
      // 600 s, not 360 (agent Details v3, 2026-10-04): the base it was cut for
      // measured 347 s alone on the SwarmCloud container -- 13 s of headroom --
      // and the v3 branch, which adds details.css to every element's cascade
      // and the Details tab's cards to the DOM it sweeps, measured 397 s alone
      // with identical findings (dark 1741 shapes / 13, light 422 / 12).
      // Both landed in one tree (#572 merged into U12, 2026-10-05): each added
      // about 10% alone, so the sum is near 440 s, still inside 600.
      // 900 s, not 600 (#780 OB8, 2026-10-08): on the SwarmCloud container
      // origin/main 891d53e measured 623 s ALONE -- already past 600, with its
      // findings complete (dark 1808 shapes / 13, light 431 / 12, all exempt).
      // OB8 adds the Setup and Access routes and Overview's setup card: 691 s
      // alone, the same 13 and 12 exempt findings over 1952 and 457 shapes.
      // 900 leaves about 30% for a loaded run.
    }, 900000)
  })
}
