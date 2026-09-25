// THE SHELL (§B2, §B3), THE DENSITY PASS (§B4.3), THE DOCK (§B18) AND THE
// MEASURED ZERO (§B20) -- every one of them as a rendered assertion.
//
// WHY NOT A SOURCE GREP. A verifier on an earlier wave neutered a guard in
// this app with `false &&` and the suite stayed green, because the test only
// checked that a string appeared in the SOURCE. So every claim below is made
// against a DOM this app actually produced, or against the computed style the
// SHIPPED `styles.css` actually resolves to. `vitest.config.ts` sets
// `css: true` for exactly this: with the default, `styles.css?raw` would
// resolve to the empty string, the injected sheet would have zero rules, and
// every negative assertion would pass vacuously.
//
// THE ONE THING THAT IS NOT A DOM ASSERTION, and why. jsdom implements the
// cascade but not layout and not media-query evaluation, so a
// `@media (min-width: 1100px)` block never applies -- there is no viewport to
// match it against. The two breakpoint claims below are therefore read out of
// the CSSOM (`CSSMediaRule.conditionText` and the declarations inside it),
// exactly as `brand.test.tsx` already does. What the layout LOOKS like at
// 1100px is not verified here, and nothing in this repository can verify it
// offline.
//
// jsdom ALSO DOES NOT RESOLVE `var()`. `getComputedStyle(el).maxWidth` answers
// the literal `var(--app-max)`, and a shorthand carrying a custom property is
// not expanded into its longhands at all -- which is why `styles.css` writes
// the two rules this file measures (`.section + .section`'s hairline and the
// track axis) as longhands, with the reason beside them.

import STYLES from '../styles.css?raw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import { App } from '../App'
import {
  DOCK,
  DOCK_COLLAPSED,
  INSPECTOR,
  clampPane,
  percentile,
  readPane,
  summariseProbes,
  writePane,
} from '../panes'
import type { ProbeRecord } from '../fetch'
import { elapsed, formatDuration, timeAgo } from '../types'
import { cascade, declarations, flatRules, splitTop, type CascadeEnv } from './cssgate'
import { task } from './runfixture'

/**
 * Put the SHIPPED stylesheet into the document so `getComputedStyle` answers
 * with the real cascade. A hand-written copy of the rule under test would be
 * the fixture-that-proves-nothing problem, so the file itself is read.
 */
function withStyles(): HTMLStyleElement {
  const el = document.createElement('style')
  el.textContent = STYLES
  document.head.appendChild(el)
  return el
}

function token(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

/** The rules in the sheet, flattened, including the ones inside media blocks. */
function allRules(sheet: CSSStyleSheet): CSSStyleRule[] {
  const out: CSSStyleRule[] = []
  const walk = (rules: CSSRuleList) => {
    for (const r of rules) {
      if (r.constructor.name === 'CSSMediaRule') walk((r as CSSMediaRule).cssRules)
      else if (r.constructor.name === 'CSSStyleRule') out.push(r as CSSStyleRule)
    }
  }
  walk(sheet.cssRules)
  return out
}

function probe(over: Partial<ProbeRecord> = {}): ProbeRecord {
  return {
    path: '/v1/capacity',
    lastStatus: 200,
    lastKind: null,
    lastLatencyMs: 120,
    lastAttemptAt: Date.now(),
    lastSuccessAt: Date.now(),
    ...over,
  }
}

// ===========================================================================
// §B2 -- the information architecture
// ===========================================================================

describe('B2: the section question is printed once, not on every pane', () => {
  it('no longer renders the question under the tab strip', () => {
    render(<App />)
    // The element the sentence used to live in. It was drawn on all fifteen
    // routes -- five copies for Pools alone -- and §B2 moves it behind the
    // head's `?`. Putting it back is what this turns red on.
    expect(document.querySelector('.ctl-section-q')).toBeNull()
  })

  it('carries the question behind the head glyph, and opens it on FOCUS', async () => {
    render(<App />)
    const glyph = document.querySelector<HTMLButtonElement>('.ctl-head .ctl-q-glyph')
    expect(glyph, 'the head has no section `?`').not.toBeNull()

    // Nothing is on screen until it is asked for: that is the whole move.
    expect(document.querySelector('.ctl-q-card')).toBeNull()

    // FOCUS, not hover. Hover does not exist on a touch device and does not
    // survive a screenshot, so a hover-only explanation is unavailable to the
    // people most likely to need it.
    fireEvent.focus(glyph!)
    await waitFor(() => expect(document.querySelector('.ctl-q-card')).not.toBeNull())

    // The question itself, not a paraphrase of it. Overview is the landing
    // section, so its question is the one on screen at mount.
    expect(document.querySelector('.ctl-q-card')?.textContent ?? '').toContain(
      'Is the platform healthy right now',
    )
  })
})

describe('B2: the attempt timeline is a view mode, not a screen', () => {
  it('is reachable only as a pane of one agent, and the pane strip is there', () => {
    // §B2: "AttemptTimeline.tsx stops being a screen and becomes a view mode
    // inside the agent inspector. It answers no question that 'what is
    // running, what did it produce' does not already own."
    window.location.hash = '#work/task/t-1/attempts'
    render(<App />)

    const drawer = document.querySelector('.ctl-drawer')
    expect(drawer, 'the agent inspector did not open').not.toBeNull()
    const panes = [...drawer!.querySelectorAll('[role="tab"]')].map((b) => b.textContent?.trim())
    expect(panes).toEqual(['Detail', 'Attempts'])
    expect(
      drawer!.querySelector('[role="tab"][aria-selected="true"]')?.textContent?.trim(),
    ).toBe('Attempts')

    // And it is NOT in the rail: a section tab pointing at it would make it a
    // destination again. (test_nav_headings_agree.py holds the other half of
    // this -- every SectionBody case must have a tab, and vice versa.)
    const railTabs = [...document.querySelectorAll('.ctl-rail [role="tab"]')].map((b) =>
      (b.firstChild?.textContent ?? '').trim(),
    )
    expect(railTabs).not.toContain('Attempts')
    window.location.hash = ''
  })
})

describe('B2: provenance is context, not a destination', () => {
  it('draws no data-source footer inside the page', () => {
    render(<App />)
    // `.sources` was a `<footer>` of sixteen cards at the bottom of every
    // route. The CELLS are not deleted -- they are in the dock -- but the
    // footer is, and re-adding it is what this catches.
    expect(document.querySelector('footer.sources')).toBeNull()
  })
})

// ===========================================================================
// §B3 -- layout: regions, with widths
// ===========================================================================

describe('B3: the rail', () => {
  it('is a 200px grid column, and the page keeps its gutter tokens', () => {
    const style = withStyles()
    const { container } = render(<div className="app" />)
    const app = getComputedStyle(container.querySelector('.app')!)

    expect(app.display).toBe('grid')
    expect(app.gridTemplateColumns).toContain('var(--rail-w)')
    expect(token('--rail-w')).toBe('200px')

    // Still the content column the product header lines its wordmark up with.
    expect(app.maxWidth).toBe('var(--app-max)')
    expect(app.padding).toContain('var(--app-pad)')
    style.remove()
  })

  it('renders the three sections under Overview AND every section tab, at all times', () => {
    render(<App />)
    const rail = document.querySelector('.ctl-rail')
    expect(rail, 'no rail').not.toBeNull()

    // Four rail entries, in one fixed order, so a position means one thing.
    const sections = [...rail!.querySelectorAll('.ctl-rail-group > .ctl-nav-link')].map(
      (b) => b.textContent?.trim(),
    )
    // THREE SECTIONS AND A LANDING SCREEN, not six sections. `Runtimes` became
    // a pane of Capacity and `History` dissolved -- Timeline into Work,
    // Platform counts into Admin. The measurement behind it is ux-plan.md
    // §1.4: fifteen screens grouped by which subsystem owned the data, so four
    // of a reader's five questions were spread over ten destinations.
    //
    // WHAT THIS ASSERTION IS FOR IS THE ORDER AND THE COUNT, not the collapse.
    // The rail's whole value is that a position means one thing, so a section
    // appearing, disappearing or moving has to be a decision someone took
    // rather than a diff nobody read.
    //
    // `Work` and `Capacity`, not `Agents` and `Pools`. Both of those named the
    // section after its own first tab, and because both sections have more
    // than one tab the rail drew the name twice -- `Agents > Agents`,
    // `Pools > Pools` -- and so did the breadcrumb. The assertion below on
    // `Holders` is the other half: the tab could drop the word `Capacity` only
    // once the section carried it.
    expect(sections).toEqual(['Overview', 'Work', 'Capacity', 'Admin'])

    // NO SECTION MAY BE NAMED AFTER ONE OF ITS OWN TABS. The regression this
    // file exists to catch, stated as the rule rather than as one spelling of
    // it, so a future section cannot reintroduce it under a different name.
    const railGroups = [...rail!.querySelectorAll('.ctl-rail-group')]
    for (const g of railGroups) {
      const name = g.querySelector('.ctl-nav-link')?.textContent?.trim()
      const own = [...g.querySelectorAll('[role="tab"]')].map((b) =>
        (b.firstChild?.textContent ?? '').trim(),
      )
      expect(own, `section "${name}" is named after one of its own tabs`).not.toContain(name)
    }

    // THE PROPERTY THAT MATTERS: the second level does not appear on demand.
    // Capacity's six tabs are in the DOM while Overview is the open section,
    // so the rail's geometry is a constant rather than something you re-read.
    // The first text node, not `textContent`: an admin-gated tab appends the
    // word "admin" as a marker span, and folding that into the label would
    // make this assert on the marker rather than on the name.
    const tabs = [...rail!.querySelectorAll('[role="tab"]')].map((b) =>
      (b.firstChild?.textContent ?? '').trim(),
    )
    // THE THREE PANES THE COLLAPSE MOVED, each named here rather than left to
    // the count below: a screen that loses its tab is still reachable by hash
    // and still passes every routing test, so the only thing that notices is
    // an assertion that says the tab exists.
    expect(tabs, 'Runtimes lost its tab when its section dissolved').toContain('Runtimes')
    expect(tabs, 'Timeline lost its tab when History dissolved').toContain('Timeline')
    expect(tabs, 'Platform counts lost its tab when History dissolved').toContain('Platform counts')
    // "Profile headroom", not "Runner profiles". The rename is what pays for
    // Runtimes and this pane sharing a section: two adjacent tabs with
    // near-synonymous labels is how a reader takes a per-tenant figure for a
    // platform one, and the old pair was exactly that. Asserting the NEW name
    // here is also what stops a revert being silent -- `Runner profiles` would
    // otherwise route, render and read fine.
    expect(tabs).toContain('Profile headroom')
    expect(tabs).not.toContain('Runner profiles')
    expect(tabs).toContain('Holders')
    expect(tabs).toContain('Tenants')
    // Work(5) + Capacity(6) + Admin(3). Overview has one pane and draws no
    // second level -- one tab under one section is a duplicate of the section.
    expect(tabs.length).toBe(14)
  })

  it('keeps the utility corner the heading test reads', () => {
    render(<App />)
    const util = document.querySelector('.ctl-nav-util')
    expect(util, 'tests/unit/control_plane/test_nav_headings_agree.py finds the utility button by this class').not.toBeNull()
    expect(util!.querySelector('button')?.textContent?.trim()).toBe('API reads')
  })
})

describe('B3: the head', () => {
  it('carries a breadcrumb and the age of the newest successful read', () => {
    render(<App />)
    const head = document.querySelector('.ctl-head')
    expect(head, 'no head region').not.toBeNull()
    expect(head!.querySelector('.ctl-crumb')?.textContent ?? '').toContain('Overview')

    // NOTHING HAS LOADED is a different sentence from "0s ago", and this
    // suite is offline, so it is the true one here. A head that rendered a
    // zero age against no successful read would be the defining bug of this
    // product, in the frame.
    const age = head!.querySelector('.ctl-head-age')?.textContent ?? ''
    expect(age).toContain('nothing has loaded')
    expect(age).not.toMatch(/\d/)
  })
})

describe('B3: the inspector is a column, not an overlay, where two panes fit', () => {
  it('declares the third grid column inside a min-width block', () => {
    const style = withStyles()
    const media = [...style.sheet!.cssRules]
      .filter((r): r is CSSMediaRule => r.constructor.name === 'CSSMediaRule')
      .filter((r) => /min-width:\s*1100px/.test(r.conditionText))

    expect(media.length, 'nothing is keyed to the 1100px two-pane threshold').toBeGreaterThan(0)

    const text = media.map((m) => [...m.cssRules].map((r) => r.cssText).join('\n')).join('\n')
    expect(text).toContain('has-inspector')
    expect(text).toContain('var(--inspector-w)')
    // Below it, the drawer stays the fixed overlay it already draws, which is
    // the right answer where two panes genuinely do not fit.
    expect(text).toContain('position: sticky')
    style.remove()
  })

  it('clamps a remembered width, and survives storage that throws', () => {
    expect(INSPECTOR.min).toBe(400)
    expect(INSPECTOR.max).toBe(720)

    expect(clampPane(12, INSPECTOR.min, INSPECTOR.max)).toBe(400)
    expect(clampPane(9000, INSPECTOR.min, INSPECTOR.max)).toBe(720)
    expect(clampPane(512, INSPECTOR.min, INSPECTOR.max)).toBe(512)

    // `Number.parseInt('')` is NaN, and NaN survives Math.min/Math.max --
    // which reaches the DOM as `width: NaNpx`, which the browser DROPS, which
    // leaves a pane at its content width with no way to drag it back.
    expect(clampPane(Number.NaN, INSPECTOR.min, INSPECTOR.max)).toBe(400)

    const throwing = {
      getItem() {
        throw new Error('SecurityError: access is denied for this document')
      },
      setItem() {
        throw new Error('SecurityError: access is denied for this document')
      },
    }
    expect(readPane(INSPECTOR, throwing)).toBe(INSPECTOR.initial)
    expect(() => writePane(INSPECTOR, 500, throwing)).not.toThrow()

    // A stored value out of range is clamped rather than trusted.
    const store = new Map<string, string>([[INSPECTOR.key, '5000']])
    expect(readPane(INSPECTOR, { getItem: (k) => store.get(k) ?? null })).toBe(720)
    expect(readPane(INSPECTOR, { getItem: () => 'four hundred' })).toBe(INSPECTOR.initial)
  })
})

// ===========================================================================
// §B4.3 -- density and spacing
// ===========================================================================

describe('B4.3: the six moves', () => {
  it('move 1: regions are separated by a hairline, not by 28px of nothing', () => {
    const style = withStyles()
    const { container } = render(
      <>
        <section className="section" />
        <section className="section" />
      </>,
    )
    const [first, second] = [...container.querySelectorAll('.section')]
    // The FIRST region has no rule above it: a hairline with nothing on the
    // other side of it reads as content that failed to load.
    expect(getComputedStyle(first!).borderTopWidth).not.toBe('1px')
    expect(getComputedStyle(second!).borderTopWidth).toBe('1px')
    // `--line-soft`, not `--ctl-hairline`. The sheet now carries two divider
    // weights (see the token block in `styles.css`): `--line` for a component
    // boundary, which SC 1.4.11 holds at 3:1, and `--line-soft` for the rule
    // between two repeats of one thing, which this is. The old single token
    // measured 1.18-1.40:1 against the surfaces while the comment above this
    // very rule claimed it was "now at 3:1"; `spacing.test.tsx` measures both
    // weights on rendered elements so that claim cannot go stale again.
    expect(getComputedStyle(second!).borderTopColor).toBe('var(--line-soft)')
    style.remove()
  })

  it('move 2: one track primitive, one height, one radius', () => {
    const style = withStyles()
    const { container } = render(
      <>
        <span className="ctl-track" />
        <span className="ctl-util-track" />
        <span className="sr-bar" />
        <span className="coverage" />
      </>,
    )
    for (const sel of ['.ctl-track', '.ctl-util-track', '.sr-bar', '.coverage']) {
      const s = getComputedStyle(container.querySelector(sel)!)
      expect(s.height, `${sel} does not use the one track height`).toBe('var(--track-h)')
      expect(s.borderRadius, `${sel} has its own radius`).toBe('var(--track-radius)')
    }
    expect(token('--track-h')).toBe('8px')
    style.remove()
  })

  it('move 3: a bar is capped and packed left, not stretched to the glass', () => {
    const style = withStyles()
    const chart = render(
      <div className="chart">
        <div className="col" />
      </div>,
    )
    const col = getComputedStyle(chart.container.querySelector('.col')!)
    expect(col.maxWidth).toBe('72px')
    expect(getComputedStyle(chart.container.querySelector('.chart')!).justifyContent).toBe(
      'flex-start',
    )

    const row = render(<div className="split-row" />)
    const grid = getComputedStyle(row.container.querySelector('.split-row')!)
    // No `1fr` in the middle: a fraction is what stretched the bar to whatever
    // was left, which on a wide screen is 600px of bar beside a two-digit
    // number.
    expect(grid.gridTemplateColumns).not.toContain('1fr')
    expect(grid.gridTemplateColumns).toContain('280px')
    expect(grid.justifyContent).toBe('start')
    style.remove()
  })

  it('move 4: a four-character tab does not take a quarter of the row', () => {
    const style = withStyles()
    const { container } = render(
      <div className="tabs">
        <button>All</button>
      </div>,
    )
    const b = getComputedStyle(container.querySelector('button')!)
    // jsdom expands `flex` when no custom property is involved, so the
    // longhand is the honest read; the shorthand is checked as a fallback for
    // a parser that does not.
    expect(b.flexGrow || b.flex).toMatch(/^0/)
    style.remove()
  })

  it('move 5: the page is not centred in a 1100px lane', () => {
    const style = withStyles()
    const { container } = render(<div className="app" />)
    const max = token('--app-max')
    expect(getComputedStyle(container.querySelector('.app')!).maxWidth).toBe('var(--app-max)')
    expect(Number.parseInt(max, 10)).toBeGreaterThanOrEqual(1600)
    style.remove()
  })

  it('move 6: --ctl-s4 is gone, and nothing still names it', () => {
    const style = withStyles()
    expect(token('--ctl-s4')).toBe('')
    const users = allRules(style.sheet!)
      .filter((r) => r.cssText.includes('var(--ctl-s4)'))
      .map((r) => r.selectorText)
    expect(users, 'a rule still reads the retired spacing step').toEqual([])
    // Four steps, and they are the four the brief names.
    expect([token('--ctl-s1'), token('--ctl-s2'), token('--ctl-s3'), token('--ctl-s5')]).toEqual([
      '4px',
      '8px',
      '12px',
      '28px',
    ])
    style.remove()
  })
})

describe('one control, one appearance', () => {
  it('draws `.retry` the same inside a state panel and outside one', () => {
    // `.retry` is the product's "try that again" button and it is rendered in
    // five files -- ErrorBoundary, Shell twice, Overview and PlatformCounts.
    // It was styled as `.state .retry`, scoped to the panel it happened to be
    // written for first, and PlatformCounts' "Run the count" is not inside a
    // `.state` at all: the same control had two appearances, one of which was
    // whatever the browser draws by default. The spacing probe found it by
    // failing to resolve `buttonface` to a colour.
    //
    // This is the claim the probe CANNOT make -- it measures geometry and
    // contrast, not "these two are the same button" -- so it is made here, on
    // the shipped sheet, against two elements the cascade actually reached.
    const style = withStyles()
    const { container } = render(
      <div className="app">
        <div className="state">
          <button className="retry">Try again</button>
        </div>
        <button className="retry">Run the count</button>
      </div>,
    )
    const both = [...container.querySelectorAll<HTMLElement>('.retry')]
    expect(both.length).toBe(2)
    const [inside, outside] = [both[0]!, both[1]!]
    const read = (el: HTMLElement) => {
      const s = getComputedStyle(el)
      return [s.background, s.borderTopColor, s.borderTopWidth, s.borderRadius, s.padding, s.font].join(' | ')
    }
    expect(read(outside)).toBe(read(inside))
    // And what they agree on is the PRODUCT's surface, not the user agent's.
    // An unstyled `<button>` computes `background: none` and a `buttonface`
    // border here; reaching `--surface-2` means this sheet's rule got to it.
    expect(read(outside)).toContain('var(--surface-2)')
    style.remove()
  })
})

describe('D2: compact data inside generous chrome', () => {
  it('holds the row height inside the 28-32px band the owner settled on', () => {
    const style = withStyles()
    const h = Number.parseInt(token('--row-h'), 10)
    expect(h).toBeGreaterThanOrEqual(28)
    expect(h).toBeLessThanOrEqual(32)

    const { container } = render(
      <div className="ctl-table">
        <table>
          <tbody>
            <tr>
              <td>a</td>
            </tr>
          </tbody>
        </table>
      </div>,
    )
    expect(getComputedStyle(container.querySelector('tr')!).height).toBe('var(--row-h)')
    style.remove()
  })

  it('does NOT squeeze the chrome with it', () => {
    const style = withStyles()
    // The resolution: compact DATA, generous CHROME. A panel's padding is
    // named for its job so it cannot be mistaken for a rhythm step and
    // tightened along with the rows -- which is the naive compact pass that
    // makes "very hard to read" worse.
    expect(Number.parseInt(token('--ctl-pad-chrome'), 10)).toBeGreaterThanOrEqual(
      Number.parseInt(token('--ctl-s3'), 10),
    )
    const { container } = render(<div className="ctl-empty" />)
    expect(getComputedStyle(container.querySelector('.ctl-empty')!).padding).toBe(
      'var(--ctl-pad-chrome)',
    )
    style.remove()
  })

  it('gives a control in the frame a target taller than a data row', () => {
    const style = withStyles()
    const { container } = render(<button className="ctl-nav-link" />)
    expect(
      Number.parseInt(getComputedStyle(container.querySelector('button')!).minHeight, 10),
    ).toBeGreaterThanOrEqual(30)
    style.remove()
  })
})

// ===========================================================================
// §B18 -- telemetry behind a disclosure
// ===========================================================================

describe('B18: the strip collapses to one line and expands on click', () => {
  it('summarises routes, p95, failures and the admin gate -- separately', () => {
    const s = summariseProbes([
      probe({ path: '/v1/capacity', lastLatencyMs: 100 }),
      probe({ path: '/v1/tasks', lastLatencyMs: 200 }),
      probe({
        path: '/v1/admin/leases',
        lastKind: 'admin_required',
        lastStatus: 403,
        lastLatencyMs: 300,
        lastSuccessAt: null,
      }),
      probe({
        path: '/v1/stats',
        lastKind: 'unreachable',
        lastStatus: null,
        lastLatencyMs: 4000,
        lastSuccessAt: null,
      }),
    ])
    expect(s.routes).toBe(4)
    // THE ADMIN GATE IS NOT A FAILURE. A non-admin genuinely cannot read
    // /v1/admin/*, and a console that counts that as a fault reports itself
    // broken every time a non-admin opens it.
    expect(s.adminOnly).toBe(1)
    expect(s.failed).toBe(1)
    // Nearest-rank over four samples: ceil(0.95 * 4) = 4, the largest.
    expect(s.p95Ms).toBe(4000)
    expect(s.expired).toBe(false)

    expect(percentile([], 95)).toBeNull()
    expect(percentile([5], 95)).toBe(5)
    // Nearest rank, never interpolated: an interpolated p95 is a latency no
    // request had, and every number on these screens has to be one something
    // actually measured.
    expect(percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50)).toBe(5)
  })

  it('draws one line at rest and the cells only once asked', async () => {
    const { Dock } = await import('../Dock')
    const { noteFixtureProbe } = await import('../fetch')
    noteFixtureProbe('/v1/capacity', 120, true)
    noteFixtureProbe('/v1/admin/leases', 300, false)

    const { container } = render(<Dock />)
    const line = container.querySelector<HTMLButtonElement>('.ctl-dock-line')
    expect(line, 'the dock draws no collapsed line').not.toBeNull()
    expect(line!.getAttribute('aria-expanded')).toBe('false')

    // The facts that decide whether to open it are ON the collapsed line.
    const facts = line!.textContent ?? ''
    expect(facts).toMatch(/route/)
    expect(facts).toMatch(/p95/)
    expect(facts).toMatch(/failed/)

    // COLLAPSED MEANS COLLAPSED: no cells, which is the ~200px this reclaims
    // on all fifteen routes.
    expect(container.querySelector('.source-cells')).toBeNull()

    fireEvent.click(line!)
    await waitFor(() => expect(container.querySelector('.source-cells')).not.toBeNull())
    expect(line!.getAttribute('aria-expanded')).toBe('true')

    // The cells are not deleted: every route that was in the footer is here.
    const paths = [...container.querySelectorAll('.s-path')].map((e) => e.textContent)
    expect(paths).toContain('/v1/capacity')
    expect(paths).toContain('/v1/admin/leases')

    // And the p95's meaning is written down where it is expanded, because a
    // p95 over one sample per route is not a p95 over requests.
    expect(container.querySelector('.ctl-dock-foot')?.textContent ?? '').toContain(
      'last attempt of each route',
    )
  })

  /**
   * RE-POINTED (§3.4, §11.3). This used to assert `position: fixed` and
   * `bottom: 0` -- the OVERLAY encoding. The dock is now a row of
   * `.ctl-frame`'s grid instead, so those two declarations are gone and
   * asserting them would pin the bug: an opaque fixed bar over a scrolling
   * document has content behind it at some scroll offset, always, and two
   * passes of reservations (`.app`'s bottom padding, then
   * `scroll-padding-bottom`) failed to buy that space back.
   *
   * WHAT MOVED: "always at the bottom of the viewport" was expressed by
   * `position: fixed; bottom: 0` and is now expressed by the frame being
   * exactly one viewport tall with the dock as its second row. So the
   * assertion moves to the frame -- and it asserts the two `min-*: 0` that
   * make it work, because each was a real defect. Without `min-height: 0` the
   * scroller sizes to its content and pushes the dock off-screen; without
   * `min-width: 0` the frame overflows sideways (caught only in a 390px
   * screenshot -- jsdom has no layout engine, see §11.5).
   *
   * The dock's OWN guarantees -- 28px at rest, 120px-70vh when dragged -- are
   * unchanged and still asserted below.
   */
  it('is a row of the frame, 28px at rest, and resizes within 120px-70vh', () => {
    const style = withStyles()

    const frame = render(<div className="ctl-frame" />)
    const f = getComputedStyle(frame.container.querySelector('.ctl-frame')!)
    expect(f.display).toBe('grid')
    // Row 1 takes what is left, row 2 is the dock at its own height.
    expect(f.gridTemplateRows).toBe('minmax(0, 1fr) auto')
    expect(f.height).toBe('100%')

    const scroll = render(<div className="ctl-scroll" />)
    const s = getComputedStyle(scroll.container.querySelector('.ctl-scroll')!)
    expect(s.overflow).toBe('auto')
    // jsdom normalises `0` to `0px`; either spelling is the same rule.
    expect(s.minHeight).toMatch(/^0(px)?$/)
    expect(s.minWidth).toMatch(/^0(px)?$/)

    // NOT AN OVERLAY ANY MORE. `position: fixed` here is the defect, so this
    // asserts its absence rather than trusting the frame rules above.
    const { container } = render(<div className="ctl-dock" />)
    const dock = getComputedStyle(container.querySelector('.ctl-dock')!)
    expect(dock.position).not.toBe('fixed')

    const line = render(<button className="ctl-dock-line" />)
    expect(getComputedStyle(line.container.querySelector('button')!).minHeight).toBe(
      `${DOCK_COLLAPSED}px`,
    )

    expect(clampPane(10, DOCK.min, 600)).toBe(120)
    expect(clampPane(10_000, DOCK.min, 600)).toBe(600)
    style.remove()
  })

  it('renders nothing at all before any route has been called', async () => {
    // A bar summarising no reads would be a claim about a measurement nobody
    // has made. `Dock` is imported fresh so the module registry is empty.
    vi.resetModules()
    const { Dock } = await import('../Dock')
    const { container } = render(<Dock />)
    expect(container.querySelector('.ctl-dock')).toBeNull()
  })
})

// ===========================================================================
// §B20 -- a zero bar must look measured
// ===========================================================================

describe('B20: a measured zero and an unrendered track are different marks', () => {
  it('draws an axis on a measured track and none on an unmeasured one', () => {
    const style = withStyles()
    const { container } = render(
      <>
        <span className="ctl-util-track" data-k="measured" />
        <span className="ctl-util-track is-unknown" data-k="unknown" />
      </>,
    )
    const measured = getComputedStyle(container.querySelector('[data-k="measured"]')!)
    const unknown = getComputedStyle(container.querySelector('[data-k="unknown"]')!)

    // THE AXIS. A 3px rule down the left edge of every track that is drawn
    // against a ceiling somebody read -- so `0 / 10` renders as a visible mark
    // on a scale rather than as an empty box indistinguishable from a widget
    // that failed to render.
    expect(measured.borderLeftWidth).toBe('var(--track-axis)')
    expect(measured.borderLeftStyle).toBe('solid')
    expect(token('--track-axis')).toBe('3px')
    // Not the track's own background: an axis the colour of the track is not
    // an axis.
    expect(measured.borderLeftColor).toBe('var(--text-dim)')
    expect(measured.borderLeftColor).not.toBe(measured.backgroundColor)

    // AND NOT ON AN UNMEASURED ONE. There is no scale for the axis to start:
    // the hatch is this sheet's mark for "not a measurement", and giving it an
    // axis as well would say the opposite of what it means.
    expect(unknown.borderLeftWidth).toBe('0px')
    expect(unknown.background).toContain('var(--ctl-hatch)')
    style.remove()
  })

  it('a count the response did not carry is hatched, not drawn as zero', async () => {
    // The screen that had the defect in its sharpest form: a state missing
    // from a partial /v1/stats drew a zero-width fill on a plain track, which
    // is exactly the mark the axis now reserves for a MEASURED zero.
    vi.resetModules()
    const loadStats = vi.fn()
    // Platform counts reads the session to price its first run (AH-9). Who is
    // asking is not what this test is about, so the read simply fails.
    const loadMe = vi.fn(async () => ({
      status: 'error' as const,
      error: { kind: 'upstream_degraded' as const, httpStatus: 503, code: null, message: 'no session' },
    }))
    vi.doMock('../api', () => ({ loadStats, loadMe }))
    const { PlatformCountsScreen } = await import('../PlatformCounts')
    const { REAL_STATES } = await import('../types')

    const counts: Record<string, number> = {}
    for (const s of REAL_STATES) counts[s] = 3
    const measuredZero = REAL_STATES[0]!
    const absent = REAL_STATES[1]!
    counts[measuredZero] = 0
    delete counts[absent]

    loadStats.mockResolvedValue({
      status: 'ok',
      data: {
        tenant_id: 'eng',
        tasks_by_state: counts,
        dispatch_paused: false,
        limits: {},
        generated_at: '2026-09-22T10:00:00Z',
      },
      fetchedAt: Date.now(),
      serverAt: '2026-09-22T10:00:00Z',
    })

    const style = withStyles()
    const { container } = render(<PlatformCountsScreen />)
    screen.getByRole('button', { name: 'Run the count' }).click()
    await waitFor(() => expect(container.querySelector('.sr-bar')).not.toBeNull())

    const rowFor = (state: string) =>
      [...container.querySelectorAll('.split-row')].find(
        (r) => r.querySelector('.sr-name')?.textContent === state,
      )!

    const zeroBar = rowFor(measuredZero).querySelector('.sr-bar')!
    const absentBar = rowFor(absent).querySelector('.sr-bar')!

    // A measured zero keeps the axis: it is a result.
    expect(zeroBar.className).not.toContain('is-unknown')
    expect(getComputedStyle(zeroBar).borderLeftWidth).toBe('var(--track-axis)')

    // An absent count loses it, and is hatched, matching the em dash beside it.
    expect(absentBar.className).toContain('is-unknown')
    expect(getComputedStyle(absentBar).borderLeftWidth).toBe('0px')
    expect(rowFor(absent).querySelector('.sr-n')?.textContent).toBe('—')
    style.remove()
    vi.doUnmock('../api')
  })
})

// -- the capacity row's name column ---------------------------------------
//
// The compact density pass clipped it on the DEPLOYED console: "claude-code ·
// 2…", "codex · 10 can …", "mock · 20 can s…", "team · 0 assign…". The name
// track was `minmax(0, 1fr)` while the bar held a 90px floor, so under
// pressure -- a 200px rail plus three columns on Overview -- the label
// collapsed first.
//
// A bar that loses 40px still shows its proportion. A name that loses 40px
// stops saying which pool the row is about, and the pool name is the only part
// of that row you cannot infer from the others.
//
// This reads the SHIPPED stylesheet, the same way every other assertion in
// this file does, rather than a hand-written copy of the rule.
// §B6.2 RE-POINT. THE CLAIM IS UNCHANGED AND IS NOW STRONGER; ONLY THE
// ENCODING MOVED, from a grid track list to a flex basis.
//
// What moved and why: a floored track stops the name being SQUEEZED, and that
// is all it does. It cannot stop the name being TRUNCATED, because a grid has
// no way to say "and if you still do not fit, take another line" -- so the
// row's four columns summed to ~441px of minimum inside a 348px card, the
// floor was overrun anyway, and `.ctl-util-name` shipped at 79.33px rendering
// `mock · 1…` where the value was 15 (inventory F1). `.ctl-util` is a wrapping
// flex line now, so this reads the name's BASIS instead of the first track,
// and it asserts the thing the old encoding could not: that the name has no
// ellipsis to fall back on, because there is no longer a case where it needs
// one.
describe('the capacity row protects the name, not the bar', () => {
  /**
   * RE-POINTED, AND WHAT MOVED IS THE SUBJECT RATHER THAN THE CLAIM.
   *
   * This used to select its rule with `STYLES.split('.ctl-util {')`, which
   * matches the first occurrence of that SUBSTRING -- and `.drawer .ctl-util {`
   * contains it and is declared ~2,400 lines earlier in the sheet. So from the
   * moment the drawer's override was added, this test has been grading the
   * drawer's four-column variant and the primitive it names has gone
   * unmeasured. It stayed green because that variant happened to floor its
   * name too.
   *
   * The claim below is unchanged and is now made against the rule it names: an
   * anchored `^.ctl-util {` picks the top-level declaration only. The drawer's
   * variant gets its own assertion underneath, because it no longer HAS a
   * four-track row to floor -- see the second test.
   */
  const ruleFor = (selector: string): string => {
    const at = new RegExp(`^${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*\\{`, 'm').exec(
      STYLES,
    )
    expect(at, `styles.css must declare a top-level ${selector} rule`).not.toBeNull()
    return STYLES.slice(at!.index).split('}')[0] ?? ''
  }

  it('floors the name and lets the row wrap rather than truncating it', () => {
    // THE ROW WRAPS; IT IS NOT A GRID OF FIXED TRACKS. Two lanes rebuilt this
    // rule in the same merge and it briefly carried both `display: grid` and
    // `display: flex` -- flex won at runtime, so the assertion has to be made
    // against flex or it grades a layout nothing renders.
    const rule = ruleFor('.ctl-util')
    expect(
      /flex-wrap:\s*wrap/.test(rule),
      'the row must be able to take a second line instead of squeezing a column to nothing',
    ).toBe(true)

    const nameBasis = /\.ctl-util > \.ctl-util-name \{([^}]*)\}/.exec(STYLES)
    expect(nameBasis, '.ctl-util must size its name column').not.toBeNull()
    const basis = /flex:\s*([^;]+);/.exec(nameBasis?.[1] ?? '')?.[1]?.trim() ?? ''
    expect(
      basis.endsWith(' 0'),
      'the NAME must not be able to reach zero width',
    ).toBe(false)
    expect(basis).toMatch(/\d+(ch|px)\s*$/)

    // And the fallback the floor used to need is gone: nothing may ellipse a
    // string whose content is a measurement.
    const name = /\n\.ctl-util-name \{([^}]*)\}/.exec(STYLES)
    expect(name, 'styles.css must declare a .ctl-util-name rule').not.toBeNull()
    expect(
      /text-overflow:\s*ellipsis/.test(name?.[1] ?? ''),
      'the name carries a measurement and must never be cut',
    ).toBe(false)
  })

  /**
   * THE DRAWER'S VARIANT MAKES THE SAME PROMISE IN A DIFFERENT SHAPE, and this
   * is where the promise now lives for it.
   *
   * F2 of `docs/audits/2026-09-23/overflow-inventory.md` measured the old
   * four-column version failing it at the drawer's own default width: `cpu not
   * sampled` rendered `cpu not sam…`, `never measured` rendered `never mea…`,
   * `latest heartbeat 3m ago` lost 77%. Four columns need 431px of content in a
   * 413px row, so no re-weighting of the tracks fixes it -- it only chooses
   * which of the four is cut, and the two that were being cut were the WORDS
   * that say a figure was never measured.
   *
   * So the row is two lines here: name and figure, then bar and provenance.
   * The assertion is therefore structural rather than about a floor -- the
   * named areas have to exist and the bar has to be on the second line, which
   * is the arrangement in which both the hatch and the words survive.
   */
  it('gives the drawer row two lines, so neither the hatch nor the words is cut', () => {
    const rule = ruleFor('.drawer .ctl-util')
    const areas = /grid-template-areas:\s*([^;]+);/.exec(rule)
    expect(areas, '.drawer .ctl-util must name its areas').not.toBeNull()
    const flat = (areas?.[1] ?? '').replace(/\s+/g, ' ').trim()
    expect(flat, 'the name and the figure share the first line').toContain("'name figure'")
    expect(flat, 'the bar and the provenance share the second').toContain("'track by'")
  })

  /**
   * AND THE TEMPLATE ABOVE HAS TO BE A TEMPLATE, WHICH IT WAS NOT.
   *
   * The test above passed on a branch where F2 was NOT fixed, and the reason is
   * worth keeping: `.drawer .ctl-util` declared `grid-template-areas` and four
   * `grid-area`s, and took its `display` from `.ctl-util` -- which §B6.2 had
   * just changed from `grid` to `flex; flex-wrap: wrap`. Every grid property in
   * the rule was therefore inert. The row fell back to the primitive's wrapping
   * flex line with `.ctl-util-by` pinned at a 128px basis, so `latest heartbeat
   * 3m ago` was still cut, under a comment saying it had been fixed.
   *
   * WHY THIS READS THE PARSED CSSOM AND NOT THE SOURCE, AND NOT
   * `getComputedStyle` EITHER. Two traps, one on each side:
   *
   *   - A SOURCE REGEX passes with the declaration deleted. The rule's own
   *     comment now contains the words `display: grid` several times, because
   *     it explains why the declaration has to be there. That is the `false &&`
   *     from this file's header arriving through prose instead of through code.
   *   - `getComputedStyle` CANNOT BE TRUSTED FOR THIS ONE. jsdom applies
   *     matching rules in SOURCE ORDER and does not weigh specificity -- the
   *     test two above, "beats a case-shifting ANCESTOR by inheritance, not by
   *     specificity", is named after that limitation. `.ctl-util` is declared
   *     ~2,300 lines AFTER `.drawer .ctl-util`, so jsdom would answer `flex`
   *     for an element a browser computes `grid` for, and the assertion would
   *     be red on a correct stylesheet.
   *
   * `CSSStyleRule.style` is the PARSED declaration block: comments are gone by
   * the time it exists, and no cascade is involved. It answers exactly the
   * question this needs answered -- does this rule declare a display at all.
   *
   * THE MUTATION THIS CATCHES: delete `display: grid;` from
   * `.drawer .ctl-util` and leave the four grid properties. Every one of them
   * goes inert, the drawer row silently becomes the primitive's flex line, and
   * `latest heartbeat 3m ago` starts being cut again. Nothing else in the suite
   * notices -- the test above it passes, because the template it greps for is
   * still written down.
   */
  it('makes the drawer row an actual grid, not a grid template on a flex box', () => {
    const style = withStyles()
    // TOP-LEVEL RULES ONLY, and that is not tidiness. `allRules` flattens the
    // media blocks in deliberately, and `@media (max-width: 899px)` carries a
    // second `.ctl-util { gap: … }` -- so an unfiltered lookup finds two rules
    // with this selector and the "exactly one" guard below fails on a correct
    // sheet. The rule under test is the unconditional one.
    const rules = [...style.sheet!.cssRules].filter(
      (r): r is CSSStyleRule => r.constructor.name === 'CSSStyleRule',
    )

    const drawerRow = rules.filter((r) => r.selectorText === '.drawer .ctl-util')
    expect(drawerRow.length, 'styles.css must declare exactly one .drawer .ctl-util').toBe(1)
    expect(
      drawerRow[0]!.style.getPropertyValue('display'),
      'the drawer row declares grid areas, so it must BE a grid or every one of them is inert',
    ).toBe('grid')
    // The precondition, so this cannot pass against a sheet that failed to
    // load: the grid properties it is protecting are really in that rule.
    expect(drawerRow[0]!.style.getPropertyValue('grid-template-areas')).not.toBe('')

    // The primitive it overrides is still the wrapping flex line, which is the
    // right answer on Overview's 348px card and the wrong one here. Asserted so
    // that "make them the same" is a change somebody has to argue for.
    const primitive = rules.filter((r) => r.selectorText === '.ctl-util')
    expect(primitive.length, 'styles.css must declare exactly one top-level .ctl-util').toBe(1)
    expect(primitive[0]!.style.getPropertyValue('display')).toBe('flex')
    expect(primitive[0]!.style.getPropertyValue('flex-wrap')).toBe('wrap')
    style.remove()
  })
})

// THE REST OF THE 2026-09-23 OVERFLOW INVENTORY, AS ASSERTIONS.
//
// F1 and F2 are above. F9 and F11 are the workflow canvas and belong to another
// lane. What is left is F3's backstop, F4, F5 and F10 -- each one a rule whose
// absence is invisible in jsdom (there is no layout engine here, so none of
// these defects can be SEEN) but whose presence is exactly what the inventory's
// pixel measurements asked for.
//
// Every claim below names the mutation it catches, because a test that cannot
// say what breaking it looks like is a test nobody can trust when it goes red.
describe('the overflow inventory, as rules that cannot be quietly dropped', () => {
  /** The top-level rule for a selector. Anchored: a bare `.ctl-util {` search
   *  matches `.drawer .ctl-util {` first, which is how the test above spent
   *  several commits grading the wrong rule. */
  const ruleFor = (selector: string): string => {
    const at = new RegExp(`^${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*\\{`, 'm').exec(
      STYLES,
    )
    expect(at, `styles.css must declare a top-level ${selector} rule`).not.toBeNull()
    return STYLES.slice(at!.index).split('}')[0] ?? ''
  }

  /**
   * F3 — the container's own cut has to signal.
   *
   * The inventory named `div.row.clickable > span.agent` by its computed value:
   * `overflow: hidden; text-overflow: clip`. At 1110px with the inspector open
   * it clipped six task ids mid-character -- `460409c`, `92eb480`, `19d91b8` --
   * and a truncated id is not an unreadable id, it is a different and equally
   * plausible one.
   *
   * The column-dropping stages fixed the measured band, so the children now
   * ellipse before this box ever clips. This is the backstop, and it is worth
   * declaring because the case that reaches it is unbounded: `--inspector-w` is
   * DRAGGABLE, so a reader can make the list narrower than any breakpoint and
   * narrower than `.id`'s own `3ch` floor.
   *
   * THE MUTATION THIS CATCHES: remove `text-overflow: ellipsis` from
   * `.row .agent`. It reverts to the UA initial value, `clip`, which is the
   * exact computed value the inventory measured.
   */
  it('never lets the agent cell cut a string without saying so', () => {
    const rule = ruleFor('.row .agent')
    expect(
      /text-overflow:\s*ellipsis/.test(rule),
      'the agent cell clips its overflow, so it has to mark the cut',
    ).toBe(true)
    // And the id keeps a floor, so the ellipsis reads as loss rather than as an
    // empty cell -- the other half of F3.
    const id = ruleFor('.row .agent .id')
    expect(/min-width:\s*3ch/.test(id), 'the id keeps enough width to show it was cut').toBe(true)
    expect(/text-overflow:\s*ellipsis/.test(id)).toBe(true)
  })

  /**
   * F4 — the drawer's ✕ needed a header to sit on.
   *
   * `position: sticky` in a scroll container 5155px taller than its viewport,
   * `z-index: auto`, an opaque fill and no reserved gutter: the probe caught the
   * 32px square painting over three different sentences at three different
   * scroll positions in one session.
   *
   * Raising the z-index alone would not have fixed it -- whichever way the
   * stacking contest goes, a sentence has a hole in it. So the drawer gets a
   * sticky band of its own background, full width, zero-space in flow, and the
   * button sits ON it.
   *
   * THE MUTATIONS THIS CATCHES: delete `.drawer::before`; drop its
   * `position: sticky` so it scrolls away with the first screenful; take the
   * `z-index` back off `.drawer-close`; write any of the band's four lengths as
   * a literal instead of deriving it from the drawer's own two tokens, so that
   * changing the drawer's padding moves the band off the button; or put
   * `float: right` back on the button, which is the subtle one — floated, it
   * shares its line with `.ctl-subnav`, and a band tall enough to cover it then
   * covers the pane tabs as well.
   */
  it('gives the drawer a header band for its close button to sit on', () => {
    // Injected for `token('--gutter')` alone: the band's horizontal bleed has
    // to equal the drawer's own side padding, and that one IS a token.
    const style = withStyles()
    const band = ruleFor('.drawer::before')
    expect(/content:\s*''/.test(band), 'the band has to be generated to exist at all').toBe(true)
    expect(
      /position:\s*sticky/.test(band),
      'a band that scrolls away leaves the button back over bare text',
    ).toBe(true)
    // Its own background, or content shows straight through it.
    expect(/background:\s*var\(--bg\)/.test(band)).toBe(true)

    // THE THREE RELATIONSHIPS, NOT THE FIVE NUMBERS.
    //
    // The band is five literals -- a height, three margins and the drawer's own
    // top padding -- because a custom property cannot be used here: the spacing
    // probe resolves every `var()` in this sheet against a table built from
    // `:root` alone, so a token on a component throws `is used and never
    // declared`, and `calc(var(--x) * -1)` lands in the unresolved list
    // `spacing.test.tsx` asserts is empty. Literals are what that tooling can
    // read; this is what stops them drifting apart.
    const len = (rule: string, prop: string): number => {
      const raw = new RegExp(`(?:^|[;{\\s])${prop}:\\s*(-?[\\d.]+)px\\s*;`, 'm').exec(rule)?.[1]
      expect(raw, `the rule must declare ${prop} as a px length`).toBeDefined()
      return Number(raw)
    }
    // `.drawer`'s padding is a shorthand whose second value is `var(--gutter)`,
    // so it is read as written and the first token taken.
    const padTop = Number(
      /padding:\s*(-?[\d.]+)px\s/.exec(ruleFor('.drawer'))?.[1] ?? Number.NaN,
    )
    expect(Number.isFinite(padTop), '.drawer must open its padding with a px length').toBe(true)
    const gutter = Number(/^(-?[\d.]+)px$/.exec(token('--gutter'))?.[1] ?? Number.NaN)
    expect(Number.isFinite(gutter), '--gutter must be a px length').toBe(true)

    const height = len(band, 'height')
    const top = len(band, 'margin-top')
    const bottom = len(band, 'margin-bottom')
    const closeH = len(ruleFor('.drawer-close'), 'height')

    expect(
      height - padTop,
      'the band is one row of chrome: taller and it eats the pane tabs, shorter and the ✕ hangs off it',
    ).toBe(closeH)
    expect(top, 'the top margin lifts the band onto the padding it was measured against').toBe(
      -padTop,
    )
    expect(
      height + top + bottom,
      'the band must contribute NOTHING to flow, or every panel in the drawer shifts down',
    ).toBe(0)
    // Bled to the drawer's own edges, or content shows through beside it.
    expect(len(band, 'margin-left')).toBe(-gutter)
    expect(len(band, 'margin-right')).toBe(-gutter)

    const close = ruleFor('.drawer-close')
    expect(
      /float:\s*right/.test(close),
      'floated, the ✕ shares its line with the pane tabs and the band covers both',
    ).toBe(false)

    // THE STICKY INSET CANCELS THE DRAWER'S PADDING (AG-31). A sticky box's
    // `top` is measured from the scroll container's CONTENT edge, so at `top:
    // 0` the band stuck 18px below the drawer's top and the content showed
    // through above it. MUTATION: put `top: 0` back.
    expect(len(band, 'top'), 'the band sticks at the drawer\'s edge, not 18px inside it').toBe(-padTop)

    // RE-POINTED (AG-30): this asserted `z-index: 2` on the ✕, which was the
    // right number until the band's own 1 turned out to tie with the sticky
    // table head (`.ctl-table thead th`, z 1, later in the DOM) -- the head
    // then painted over the band. What the number stood for is an ORDER, so
    // the order is what is asserted now, derived from the sheet: every sticky
    // table head < the band < the ✕ < the inspector's drag handle.
    // MUTATIONS: the band back to 1; the ✕ at or under the band; the grip
    // under the ✕.
    const z = (rule: string): number => {
      const raw = /(?:^|[;{\s])z-index:\s*(\d+)/.exec(rule)?.[1]
      expect(raw, 'the rule must state a numeric z-index, not default to auto').toBeDefined()
      return Number(raw)
    }
    const heads = flatRules(STYLES).filter(
      (r) => /thead th/.test(r.selector) && /position:\s*sticky/.test(r.body) && /z-index/.test(r.body),
    )
    expect(heads.length, 'no sticky table head found; the ordering below would be vacuous').toBeGreaterThan(0)
    for (const h of heads) {
      expect(z(band), `the band must paint over \`${h.selector}\``).toBeGreaterThan(z(h.body))
    }
    expect(z(close), 'the ✕ must rank above the band it sits on').toBeGreaterThan(z(band))
    expect(
      z(ruleFor('.ctl-inspector-grip')),
      'the drag handle must stay above the band and the ✕ at the drawer\'s top edge',
    ).toBeGreaterThan(z(close))
    style.remove()
  })

  /**
   * F5 — two cells reading `/v1/a…` for two different routes.
   *
   * `/v1/admin/dispatch` and `/v1/admin/tenants` both rendered `/v1/a…`, both
   * `403 · admin only`, in the same panel; the three `/v1/tasks/{id}/*` routes
   * all rendered `/v1/tasks/…`. A panel whose entire job is to say WHICH route
   * did what had four pairs of cells nobody can tell apart.
   *
   * Widening the track alone could not have fixed it: `.source`'s second track
   * is `auto` and a grid gives an `auto` track its max-content BEFORE a `1fr`
   * track gets anything, so every pixel added went to the status label. The
   * path takes the whole first line instead.
   *
   * THE MUTATION THIS CATCHES: put the track minimum back to 210px; or drop
   * `grid-column: 1 / -1` from `.s-path` so the status shares its line again;
   * or put `text-overflow: ellipsis` back on `.s-path-t`.
   */
  it('gives a route path a line of its own and never ellipses it', () => {
    const cells = ruleFor('.source-cells')
    const min = /minmax\(min\(100%,\s*(\d+)px\)/.exec(cells)?.[1]
    expect(
      Number(min ?? 0),
      'the longest route in this registry needs 253px of cell; 210 gave it 93',
    ).toBeGreaterThanOrEqual(260)

    const path = ruleFor('.s-path')
    expect(
      /grid-column:\s*1\s*\/\s*-1/.test(path),
      'the path spans the cell, or the auto status track eats the widening',
    ).toBe(true)

    const text = ruleFor('.s-path-t')
    expect(
      /text-overflow:\s*ellipsis/.test(text),
      'a truncated route is a different, equally plausible route',
    ).toBe(false)
    expect(
      /overflow-wrap:\s*anywhere/.test(text),
      'a path has no spaces, so the only honest alternative to a cut is a break',
    ).toBe(true)
  })

  /**
   * F10 — the dock summary dropped its caveats and kept its reassurance.
   *
   * `13 routes · p95 400ms · 0 failed · 2 admin-only · newest just now` got
   * 279.6px at 390pt and rendered `… · 0 failed · 2 …`. What fell off was the
   * admin-only count and the read age -- the two facts that tell a console that
   * is fine from one that is stale or half-blind. `0 failed` survived.
   *
   * Two assertions, because the fix has two halves and each fails differently:
   * the sheet must not ellipse, and the markup must give every fact a nowrap box
   * so the line breaks BETWEEN facts rather than inside `2 admin-only`.
   *
   * THE MUTATION THIS CATCHES: put `text-overflow: ellipsis; white-space:
   * nowrap` back on `.ctl-dock-facts`; or unwrap the facts in `Dock.tsx` so the
   * strip is one text run again.
   */
  it('wraps the dock summary between facts instead of ellipsing its tail', async () => {
    const facts = ruleFor('.ctl-dock-facts')
    expect(
      /text-overflow:\s*ellipsis/.test(facts),
      'the tail of this strip is the admin-only count and the read age',
    ).toBe(false)
    expect(/white-space:\s*nowrap/.test(facts), 'the strip itself has to be able to wrap').toBe(
      false,
    )
    const one = ruleFor('.ctl-dock-fact')
    expect(
      /white-space:\s*nowrap/.test(one),
      'each fact is one unit, or the break lands inside `2 admin-only`',
    ).toBe(true)

    // AND THE MARKUP ACTUALLY USES IT. The rule above is inert if `Dock.tsx`
    // renders the facts as bare text -- which is the state this fix started
    // from, and is invisible to a stylesheet assertion.
    vi.resetModules()
    const { Dock } = await import('../Dock')
    const { noteFixtureProbe } = await import('../fetch')
    noteFixtureProbe('/v1/capacity', 120, true)
    const { container } = render(<Dock />)
    const boxes = [...container.querySelectorAll('.ctl-dock-facts .ctl-dock-fact')]
    expect(
      boxes.length,
      'routes, p95, failed and the age are four facts and each needs its own box',
    ).toBeGreaterThanOrEqual(4)
    // The age is the fact the ellipsis used to eat, so it is the one checked by
    // name rather than by count.
    const text = boxes.map((b) => b.textContent ?? '').join(' ')
    expect(text, 'the read age is the fact that went missing at 390pt').toMatch(
      /newest|nothing has loaded/,
    )
  })
})

// ===========================================================================
// THE 2026-09-25 VISUAL QA PASS, AS RULES THE CASCADE HAS TO PICK
// ===========================================================================
//
// Each `it` is one box from the QA epics (#81-#87), named by its id, and each
// states the mutation that turns it red. They are asked of `cascade` (in
// `cssgate.ts`), NOT of `getComputedStyle`, and that choice is the point: jsdom
// orders rules by source position alone and applies no `@media` block, and
// several of these defects were exactly a specificity or an order that jsdom
// would have reported the other way round -- `.state p` beating `.checked-at`,
// `.limit-edit input` beating `.acct-wide`, a phone rule written above the base
// rule it had to beat. `stylesheet.gate.test.ts` proves the resolver on
// fixtures with known answers before anything here relies on it.
//
// WHAT NONE OF THIS CAN SEE: a pixel. These assert the rule a browser would
// choose and the value it would use; whether "queued 13m 18s" then fits in
// 15ch of a given font is arithmetic in the sheet's comments, not a
// measurement, and the QA screenshots are the only evidence of the rendering.

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

/** `elapsed()`'s queued form and its bare durations, at the edges of each unit. */
const NOW = Date.parse('2026-09-25T12:00:00Z')
const SPANS = [59_000, 59 * 60_000 + 59_000, 23 * 3_600_000 + 59 * 60_000, 99 * 86_400_000 + 23 * 3_600_000]
const queued = (ms: number): string =>
  elapsed(task({ created_at: new Date(NOW - ms).toISOString(), started_at: null, completed_at: null }), NOW).text

describe('the 2026-09-25 visual QA, as rules the cascade has to pick', () => {
  const hosts: HTMLElement[] = []
  afterEach(() => {
    for (const h of hosts.splice(0)) h.remove()
  })

  /** A fixture to ask the cascade about. Attached, so nothing about it is special. */
  function fragment(html: string): HTMLElement {
    const host = document.createElement('div')
    host.innerHTML = html
    document.body.appendChild(host)
    hosts.push(host)
    return host
  }

  function pick(host: Element, sel: string): Element {
    const el = host.querySelector(sel)
    expect(el, `the fixture has no ${sel}`).not.toBeNull()
    return el!
  }

  /** The value the cascade chooses. A selector the resolver could not
   *  evaluate fails here by name rather than being read as "no rule". */
  function won(
    el: Element,
    prop: string | readonly string[],
    env: CascadeEnv,
    pseudo: string | null = null,
  ): string | null {
    const r = cascade(STYLES, el, prop, env, pseudo)
    expect(r.unsupported, 'selectors the resolver could not evaluate').toEqual([])
    return r.winner?.value ?? null
  }

  /** The track that follows `[name]` in a grid template. */
  function trackAfter(template: string | null, name: string): string {
    const parts = splitTop(template ?? '', ' ')
    const at = parts.indexOf(`[${name}]`)
    expect(at, `the template names no [${name}] line: ${template}`).toBeGreaterThanOrEqual(0)
    return parts[at + 1] ?? ''
  }
  const minmax = (track: string): [string, string] => {
    const m = /^minmax\((.*)\)$/.exec(track)
    if (m === null) return [track, track]
    const [lo, hi] = splitTop(m[1]!)
    return [lo ?? '', hi ?? '']
  }
  const ch = (v: string | null | undefined): number => Number(/^([\d.]+)ch$/.exec(v ?? '')?.[1] ?? Number.NaN)
  const px = (v: string | null | undefined): number => Number(/^([\d.]+)px$/.exec(v ?? '')?.[1] ?? Number.NaN)
  /** `flex-shrink`, out of whichever of `flex` / `flex-shrink` won. */
  const shrinkOf = (el: Element, env: CascadeEnv): number => {
    const w = cascade(STYLES, el, ['flex', 'flex-shrink'], env).winner
    if (w === null) return 1
    if (w.property === 'flex-shrink') return Number(w.value)
    if (w.value === 'none') return 0
    const parts = w.value.split(/\s+/)
    return parts.length >= 2 && /^[\d.]+$/.test(parts[1]!) ? Number(parts[1]) : 1
  }

  it('AG-11: sizes the run list\'s [age] track to the longest thing the cell prints', () => {
    // DERIVED FROM `elapsed()`, so a new unit or a longer word moves the floor.
    const longest = Math.max(...SPANS.map((ms) => queued(ms).length))
    const bare = Math.max(...SPANS.map((ms) => formatDuration(ms).length))
    expect(queued(SPANS[2]!), 'the queued form this track exists for').toBe('queued 23h 59m')
    expect(longest).toBeGreaterThan(bare)

    // `ch`, because the cell is tabular: a px width cannot be compared with a
    // character count at all, and 76px was a guess that cut "queued 1…".
    // MUTATION: `[age] minmax(0, 76px)` back in either template.
    const list = fragment('<div class="app"><div class="rows"><div class="row"><span class="when">x</span></div></div></div>')
    const open = fragment(
      '<div class="app has-inspector"><div class="rows"><div class="row"><span class="when">x</span></div></div></div>',
    )
    for (const [label, row] of [
      ['the full row', pick(list, '.row')],
      ['the row beside the inspector', pick(open, '.row')],
    ] as const) {
      const age = trackAfter(won(row, 'grid-template-columns', WIDE), 'age')
      expect(ch(minmax(age)[1]), `${label}: [age] is ${age}, under ${longest} characters`).toBeGreaterThanOrEqual(longest)
    }

    // THE 1101-1200 BAND holds the DURATION and wraps the word "queued" above
    // it, because 15ch there comes out of the name. MUTATION: drop the
    // `white-space: normal`, or size the track under a bare duration.
    const band: CascadeEnv = { width: 1150 }
    const age = trackAfter(won(pick(open, '.row'), 'grid-template-columns', band), 'age')
    expect(ch(minmax(age)[1]), `the 1200 stage's [age] is ${age}`).toBeGreaterThanOrEqual(bare)
    expect(won(pick(open, '.when'), 'white-space', band)).toBe('normal')
  })

  it('AG-13: at 390 the age wraps in a fixed track and the name keeps the whole id', () => {
    const bare = Math.max(...SPANS.map((ms) => formatDuration(ms).length))
    const f = fragment(
      '<div class="rows"><div class="row"><span class="agent"><b>browser</b><span class="id">0961e42e</span></span><span class="when">x</span></div></div>',
    )
    const age = trackAfter(won(pick(f, '.row'), 'grid-template-columns', PHONE), 'age')
    // Each row is its own grid, so a content-sized track moves the name column
    // from row to row. MUTATION: `[age] auto` back.
    expect(age, 'an `auto` [age] hands the name\'s width to "queued 13m 18s"').not.toMatch(/auto|content/)
    expect(ch(minmax(age)[1])).toBeGreaterThanOrEqual(bare)
    expect(won(pick(f, '.when'), 'white-space', PHONE)).toBe('normal')
    // `shortTaskId` prints at most 8 characters (Agents.tsx `.slice(0, 8)`),
    // and the id is mono, so 8ch is the whole id. MUTATION: drop the floor.
    expect(ch(won(pick(f, '.id'), 'min-width', PHONE))).toBeGreaterThanOrEqual(8)
  })

  it('AG-27: every line clamp has the box it needs, and the phone why-line has one', () => {
    // A SHEET-WIDE PROPERTY, because `-webkit-line-clamp` alone is inert
    // anywhere: it needs a `-webkit-box`, a vertical orient and a clip.
    // MUTATION: delete any one of the three from `.row .why`.
    const clamps = flatRules(STYLES).filter((r) =>
      declarations(r.body).some((d) => d.property === '-webkit-line-clamp'),
    )
    expect(clamps.length, 'no line clamp found; this check would be vacuous').toBeGreaterThan(0)
    for (const r of clamps) {
      const d = new Map(declarations(r.body).map((x) => [x.property, x.value]))
      expect(d.get('display'), `${r.selector} clamps without display: -webkit-box`).toBe('-webkit-box')
      expect(d.get('-webkit-box-orient'), `${r.selector} clamps without a vertical orient`).toBe('vertical')
      expect(d.get('overflow'), `${r.selector} clamps and clips nothing`).toBe('hidden')
    }
    const f = fragment('<div class="row"><span class="why">five lines of error</span></div>')
    expect(won(pick(f, '.why'), 'display', PHONE)).toBe('-webkit-box')
  })

  it('AG-32: the why line sits closer to its own row than to the next one', () => {
    // MUTATION: `gap: var(--ctl-s3)` back on `.row`.
    const f = fragment('<div class="rows"><div class="row"></div></div>')
    const w = cascade(STYLES, pick(f, '.row'), ['row-gap', 'gap'], WIDE).winner
    const rowGap = w?.property === 'gap' ? splitTop(w.value, ' ')[0] : w?.value
    expect(rowGap).toBe('var(--ctl-s1)')
  })

  it('AG-15: an attempt count over its ceiling is drawn as a fault, in both lists', () => {
    // MUTATION: delete the rule, or drop either of its two class names -- the
    // markup halves are written in parallel and either spelling must land.
    const f = fragment(
      '<div class="row"><span class="try spent">83/3</span><span class="try is-over">4/3</span></div>' +
        '<div class="ctl-table wf-table"><table><tbody><tr><td class="is-over">4 of 3</td><td class="spent">5 of 3</td></tr></tbody></table></div>',
    )
    for (const sel of ['.try.spent', '.try.is-over', 'td.is-over', 'td.spent']) {
      expect(won(pick(f, sel), 'color', WIDE), `${sel}`).toBe('var(--bad)')
      expect(won(pick(f, sel), ['font-weight', 'font'], WIDE), `${sel}`).toBe('600')
    }
  })

  it('AG-16: the run list has a head row on the same grid, in the column-head register', () => {
    // The head is a `.row`, so it takes the template and the breakpoints from
    // `.row` itself; only its register is asserted here. MUTATION: delete the
    // head rules, or lower the cell rule to (0,2,0) so `.row .owner` wins.
    for (const html of [
      '<div class="rows"><div class="row is-head"><span class="owner">owner</span><span class="try">tries</span><span class="when">age</span></div><div class="row clickable"></div></div>',
      // The same row found by what it is, with no class: the one `.row` in a
      // list that is not a control.
      '<div class="rows"><div class="row"><span class="owner">owner</span><span class="try">tries</span><span class="when">age</span></div><div class="row clickable"></div></div>',
    ]) {
      const f = fragment(html)
      for (const cell of ['.owner', '.try', '.when']) {
        const el = pick(f, `.row:first-child ${cell}`)
        expect(won(el, ['font', 'font-size'], WIDE), `head ${cell}`).toContain('var(--t-micro)')
        expect(won(el, 'color', WIDE), `head ${cell}`).toBe('var(--text-faint)')
      }
      // And it drops what a row drops, because it is one.
      expect(won(pick(f, '.row:first-child .owner'), 'display', PHONE)).toBe('none')
    }
  })

  it('AG-17: the open agent\'s row is marked with a surface step and an ink rule', () => {
    // MUTATION: delete the rule, or paint it `--info`.
    const f = fragment(
      '<div class="rows"><div class="row clickable" aria-current="true"></div><div class="row clickable" aria-current="false"></div></div>',
    )
    const [on, off] = [...f.querySelectorAll('.row')]
    expect(won(on!, ['background', 'background-color'], WIDE)).toBe('var(--surface-2)')
    expect(won(on!, 'box-shadow', WIDE)).toContain('var(--text)')
    expect(won(off!, ['background', 'background-color'], WIDE), 'aria-current="false" is not current').toBeNull()
  })

  it('AG-18: the liveness word is ink in every state; the mark carries the tone', () => {
    // MUTATION: put back any of `.liveness.live|quiet|silent .lv-word { color: … }`.
    for (const state of ['live', 'quiet', 'silent', 'finished', 'not-started']) {
      const f = fragment(`<span class="liveness ${state}"><i></i><span class="lv-word">${state}</span></span>`)
      expect(won(pick(f, '.lv-word'), 'color', WIDE), state).toBe('var(--text)')
    }
  })

  it('AG-22: a markdown artifact is capped like the text viewer and scrolls inside itself', () => {
    const f = fragment('<div class="art-md"></div><pre class="art-text"></pre>')
    const cap = won(pick(f, '.art-text'), 'max-height', WIDE)
    expect(cap, 'the text viewer lost its own cap; nothing to compare with').not.toBeNull()
    // MUTATION: drop `max-height` or `overflow` from `.art-md`.
    expect(won(pick(f, '.art-md'), 'max-height', WIDE)).toBe(cap)
    expect(won(pick(f, '.art-md'), ['overflow', 'overflow-y'], WIDE)).toMatch(/^(auto|scroll)$/)
  })

  it('AG-26: tables in the inspector stack by the drawer\'s width, from the same rules', () => {
    // THE TWO BLOCKS ARE ONE LAYOUT. The page's block is a viewport query and
    // the drawer's a container query, and CSS cannot put one set of rules
    // under both -- so they are held identical here, rule by rule and
    // declaration by declaration. MUTATION: change a declaration in either
    // block, or add a stacked rule to one of them only.
    const stackedIn = (condition: string): string[] =>
      flatRules(STYLES)
        .filter((r) => r.conditions.length === 1 && r.conditions[0] === condition && r.selector.includes('.is-stacked'))
        .map((r) => `${splitTop(r.selector).join(', ')} { ${declarations(r.body).map((d) => `${d.property}: ${d.value}`).join('; ')} }`)
    const containers = flatRules(STYLES)
      .flatMap((r) => r.conditions)
      .filter((c) => c.startsWith('@container'))
    const drawerCondition = [...new Set(containers)].find((c) => /max-width:\s*899px/.test(c))
    expect(drawerCondition, 'no container query restates the stacked layout').toBeDefined()
    const page = stackedIn('@media (max-width: 899px)')
    expect(page.length, 'the page\'s stacked block was not found').toBeGreaterThan(10)
    expect(stackedIn(drawerCondition!)).toEqual(page)

    // The container it names is DECLARED, on the drawer. A container query
    // naming a container nothing establishes matches nothing, silently.
    const name = /^@container\s+([a-zA-Z][\w-]*)\s+\(/.exec(drawerCondition!)?.[1]
    expect(name, 'the stacked container query names no container').toBeDefined()
    const drawer = flatRules(STYLES).find((r) => r.conditions.length === 0 && r.selector === '.ctl-drawer')
    const declared = new Map(declarations(drawer?.body ?? '').map((d) => [d.property, d.value]))
    const container = declared.get('container') ?? `${declared.get('container-name') ?? ''} / ${declared.get('container-type') ?? ''}`
    expect(container, '`.ctl-drawer` must be the size container the query names').toMatch(
      new RegExp(`^${name!}\\s*/\\s*inline-size$`),
    )
    // The inspector is never wider than the threshold, so its tables always
    // stack -- the claim the block's comment makes, checked against panes.ts.
    expect(INSPECTOR.max).toBeLessThanOrEqual(899)

    // And the cascade agrees: the same cell stacks in the drawer at 1440 and
    // stays a table cell on the page at 1440.
    const f = fragment(
      '<div class="ctl-drawer drawer"><div class="ctl-table is-stacked"><table><tbody><tr><td data-label="Object">gs://x</td></tr></tbody></table></div></div>',
    )
    const td = pick(f, 'td')
    expect(won(td, 'display', { width: 1440, container: INSPECTOR.initial })).toBe('grid')
    expect(won(td, 'display', WIDE), 'with no container in play, a 1440 page keeps its tables').not.toBe('grid')

    // The prefix line: a path has no spaces, so it breaks anywhere or overflows.
    const p = fragment('<div class="drawer"><p class="muted small">Prefix <code class="mono">gs://bucket/t/tsk_0123/checkpoints/</code></p></div>')
    expect(won(pick(p, 'code'), 'overflow-wrap', WIDE)).toBe('anywhere')
  })

  it('AH-8: an invalid ceiling is marked on its field, and its message takes its own line', () => {
    // MUTATION: drop the `[aria-invalid]` rule, or the message's `flex-basis`.
    const f = fragment(
      '<div class="app"><span class="limit-edit"><input type="number" aria-invalid="true"><button>save</button><span class="warn-text">0–100000</span></span></div>',
    )
    expect(won(pick(f, 'input'), ['border-color', 'border'], WIDE)).toBe('var(--warn)')
    const basis = cascade(STYLES, pick(f, '.warn-text'), ['flex-basis', 'flex'], WIDE).winner
    expect(basis?.value, 'the message has to wrap under the field, not widen the cell').toMatch(/(^|\s)100%$/)
  })

  it('AH-19: Pool limits\' operand figures align right in a fixed tabular track', () => {
    // MUTATION: drop the operand grid, or lower it to (0,3,0) so the
    // primitive's flex row wins by order.
    const f = fragment(
      '<ul class="ctl-facts is-rows adm-operands"><li class="ctl-fact"><b>anthropic · u-bogdan</b>25</li></ul>',
    )
    const li = pick(f, 'li')
    expect(won(li, 'display', WIDE)).toBe('grid')
    const template = won(li, 'grid-template-columns', WIDE) ?? ''
    const last = splitTop(template, ' ').at(-1)
    expect(ch(last), `the figure track is ${last}; it must be a fixed width in characters`).toBeGreaterThanOrEqual(6)
    expect(won(li, 'justify-items', WIDE)).toBe('end')
    expect(won(li, 'font-variant-numeric', WIDE)).toBe('tabular-nums')
    expect(won(pick(f, 'b'), 'justify-self', WIDE), 'the key stays left').toBe('start')
  })

  it('CH-4: the absent and stale marks put their words on a solid fill and keep the hatch as a band', () => {
    // The contrast half is `test_ui_contrast.py`, which now measures a hatch
    // stripe by stripe. This is the silhouette half: the hatch did not simply
    // go away. MUTATION: drop the `::before` band from either mark.
    const f = fragment('<span class="ctl-mark is-absent">not measured</span><span class="ctl-stale-mark">2m old</span>')
    for (const sel of ['.ctl-mark.is-absent', '.ctl-stale-mark']) {
      expect(won(pick(f, sel), ['background', 'background-color'], WIDE), sel).toBe('var(--surface-2)')
      expect(won(pick(f, sel), 'content', WIDE, 'before'), `${sel} draws no band`).toBe("''")
      expect(won(pick(f, sel), ['background', 'background-image'], WIDE, 'before'), sel).toBe('var(--ctl-hatch)')
    }
  })

  it('CH-5: every link is ink with a quiet underline, and the accent only on hover', () => {
    // MUTATION: put `color: var(--info)` back on any of the five, or delete the
    // `:where(a)` fallback so an unclassed anchor is the browser's blue.
    const f = fragment(
      '<div class="app">' +
        '<p class="sub">read 2s ago <button>refresh</button></p>' +
        '<div class="ctl-dock-tools"><a href="#a">API reads</a></div>' +
        '<p class="ctl-panel-note"><a href="#b">why</a></p>' +
        '<p class="wb-more"><a href="#c">widen</a></p>' +
        '<div class="wf-inspect-head"><a class="wf-inspect-run" href="#d">run</a></div>' +
        '<p><a href="#e">What these mean</a></p>' +
        '</div>',
    )
    const links = [
      pick(f, '.sub button'),
      pick(f, '.ctl-dock-tools a'),
      pick(f, '.ctl-panel-note a'),
      pick(f, '.wb-more a'),
      pick(f, '.wf-inspect-run'),
      pick(f, 'p:last-child a'),
    ]
    for (const link of links) {
      const name = link.textContent
      expect(won(link, 'color', WIDE), `"${name}" at rest`).toBe('var(--text)')
      expect(won(link, 'text-decoration-color', WIDE), `"${name}" underline`).toBe('var(--line-soft)')
      expect(won(link, 'color', { ...WIDE, states: ['hover'] }), `"${name}" on hover`).toBe('var(--info)')
    }
  })

  it('CH-6: API reads and Help mark the current page the way a section does', () => {
    // MUTATION: `color: var(--info)` back on `.ctl-nav-util button.is-on`, or
    // drop the rule's surface step or its rule.
    const f = fragment(
      '<nav class="ctl-rail"><div class="ctl-nav-util"><button class="is-on" aria-current="page">API reads</button></div></nav>',
    )
    const b = pick(f, 'button')
    expect(won(b, 'color', WIDE)).toBe('var(--text)')
    expect(won(b, ['background', 'background-color'], WIDE)).toBe('var(--surface-2)')
    expect(won(b, ['border-left-color', 'border-left', 'border-color', 'border'], WIDE)).toBe('var(--text)')
    // Below 900px the strip is horizontal and the rule is the bottom edge.
    expect(won(b, ['border-bottom-color', 'border-bottom', 'border-color', 'border'], PHONE)).toBe('var(--text)')
  })

  it('CH-7: the `?` glyph is as tall as it is wide, not stretched by the button floor', () => {
    // `:where(.app button)` gives every button a 28px minimum, and a minimum
    // beats a smaller height. MUTATION: delete the glyph's own `min-height`.
    const f = fragment('<div class="app"><button class="ctl-q-glyph">?</button></div>')
    const g = pick(f, 'button')
    const height = px(won(g, 'height', WIDE))
    expect(height).toBeGreaterThan(0)
    expect(px(won(g, 'min-height', WIDE)), 'min-height over the height makes the disc a pill').toBeLessThanOrEqual(height)
  })

  it('CH-8: every control reaches 44px at 390, by height or by hit area, with the type unchanged', () => {
    const f = fragment(
      '<div class="app">' +
        '<nav class="ctl-rail"><button class="ctl-nav-link">Work</button>' +
        '<div class="ctl-rail-tabs"><button role="tab">Agents</button></div>' +
        '<div class="ctl-nav-util"><button>?</button></div></nav>' +
        '<div class="ctl-seg"><button>Live</button></div>' +
        '<span class="limit-edit"><input type="number"><button>save</button></span>' +
        '<div class="wb-controls"><select></select></div>' +
        '<button class="sbf-go">Send</button>' +
        '<div class="wfb-step"><button class="sbf-offer">url</button></div>' +
        '<button class="ctl-q-glyph">?</button><button class="ov-refresh">refresh</button>' +
        '<a class="ov-link" href="#x">open</a><button class="sbf-mini">remove</button>' +
        '</div>',
    )
    // MUTATION: move any of these phone rules above the base rule it has to
    // beat -- which is how `.ctl-seg > button` shipped -- or delete it.
    for (const sel of [
      '.ctl-nav-link',
      '.ctl-rail-tabs button',
      '.ctl-nav-util button',
      '.ctl-seg > button',
      '.limit-edit input',
      '.limit-edit button',
      '.wb-controls select',
      '.sbf-go',
      '.wfb-step .sbf-offer',
    ]) {
      expect(px(won(pick(f, sel), 'min-height', PHONE)), `${sel} at 390`).toBeGreaterThanOrEqual(44)
    }
    // The segment keeps its desktop size: the target is a phone rule.
    expect(px(won(pick(f, '.ctl-seg > button'), 'min-height', WIDE))).toBeLessThan(44)
    // A word or a disc that must not grow gets an empty, centred hit area.
    for (const sel of ['.ctl-q-glyph', '.ov-refresh', '.ov-link', '.sbf-mini']) {
      const el = pick(f, sel)
      expect(won(el, 'position', PHONE), `${sel} is not the hit area's containing block`).toBe('relative')
      expect(won(el, 'content', PHONE, 'after'), `${sel} has no hit area`).toBe("''")
      for (const axis of ['width', 'height']) {
        expect(won(el, axis, PHONE, 'after'), `${sel} hit area ${axis}`).toMatch(/44px/)
      }
    }
  })

  it('CH-9: the help card casts the elevation token, not the dark theme\'s literal', () => {
    // MUTATION: the literal `0 8px 24px rgb(0 0 0 / .28)` back.
    const f = fragment('<div class="ctl-q-card"></div>')
    expect(won(pick(f, 'div'), 'box-shadow', WIDE)).toBe('var(--ctl-shadow-pop)')
    // And no elevation token is declared for nothing: a named shadow that no
    // rule reads is how the light theme kept the dark value.
    // Comments out first: the sheet quotes these names in its own prose.
    const code = STYLES.replace(/\/\*[\s\S]*?\*\//g, ' ')
    const names = [...new Set([...code.matchAll(/(--ctl-shadow[\w-]*)\s*:/g)].map((m) => m[1]!))]
    expect(names.length).toBeGreaterThan(0)
    const used = flatRules(STYLES).map((r) => r.body).join('\n')
    for (const n of names) expect(used, `${n} is declared and never used`).toContain(`var(${n})`)
  })

  it('CH-10: "Checked just now." is the micro step inside a state panel too', () => {
    // `.state p` (0,1,1) beat `.checked-at` (0,1,0) on font-size, so it
    // rendered at the lead step. MUTATION: drop `.state p.checked-at`.
    const f = fragment('<div class="state"><p class="checked-at">Checked just now.</p></div>')
    expect(won(pick(f, 'p'), ['font-size', 'font'], WIDE)).toContain('var(--t-micro)')
  })

  it('CH-11: every key in a stacked record is left-aligned, numeric field or not', () => {
    // MUTATION: drop `text-align: left` from the key's `::before`; the numeric
    // cells then inherit their value's right alignment into the key again.
    const f = fragment(
      '<div class="ctl-table is-stacked"><table><tbody><tr>' +
        '<td class="is-num" data-label="Ceiling (units)">4</td><td data-label="Set by">operator</td>' +
        '</tr></tbody></table></div>' +
        '<div class="table-wrap is-stacked"><table class="pools"><tbody><tr>' +
        '<td class="n" data-label="Fits">3</td></tr></tbody></table></div>',
    )
    for (const td of f.querySelectorAll('td')) {
      const own = won(td, 'text-align', PHONE, 'before')
      // Not declared on the key means inherited from the cell, as a browser does.
      const key = own ?? won(td, 'text-align', PHONE)
      expect(key, `the key of "${td.getAttribute('data-label')}"`).toBe('left')
    }
  })

  it('CH-12: stacked tags and scopes are as wide as their words, and identities wrap', () => {
    const f = fragment(
      '<div class="table-wrap is-stacked"><table class="pools"><tbody><tr>' +
        '<td data-label="Scope"><span class="scope tenant">this tenant</span></td>' +
        '<td data-label="Status"><span class="tag full">none registered</span></td>' +
        '<td data-label="Identity" class="mono">swarm-u-bogdan@saga-agents-staging.iam.gserviceaccount.com</td>' +
        '</tr></tbody></table></div>',
    )
    // MUTATION: take `.tag` or `.scope` out of the justify-self list.
    expect(won(pick(f, '.scope'), 'justify-self', PHONE)).toBe('start')
    expect(won(pick(f, '.tag'), 'justify-self', PHONE)).toBe('start')
    expect(won(pick(f, '.scope'), ['margin-left', 'margin'], PHONE)).toBe('0')
    // MUTATION: drop `overflow-wrap: anywhere` from the identity cells.
    expect(won(pick(f, 'td.mono'), 'overflow-wrap', PHONE)).toBe('anywhere')
  })

  it('CH-14: the rail\'s items scroll into view clear of the fade', () => {
    // MUTATION: drop `scroll-margin-inline-end` from any of the three.
    const f = fragment(
      '<nav class="ctl-rail"><button class="ctl-nav-link is-on">Work</button>' +
        '<div class="ctl-rail-tabs"><button role="tab" aria-selected="true">Agents</button></div>' +
        '<div class="ctl-nav-util"><button class="is-on">?</button></div></nav>',
    )
    for (const sel of ['.ctl-nav-link', '.ctl-rail-tabs button', '.ctl-nav-util button']) {
      expect(won(pick(f, sel), 'scroll-margin-inline-end', PHONE), sel).toBe('var(--rail-fade)')
    }
  })

  it('CH-15: the breadcrumb holds one line and the read age holds one width', () => {
    // DERIVED FROM `timeAgo`: the age's floor is the longest thing it prints.
    const T = Date.parse('2026-09-25T12:00:00Z')
    const ages = [0, 30_000, 59 * 60_000, 47 * 3_600_000, 999 * 86_400_000].map(
      (ms) => `newest read ${timeAgo(T - ms, T)}`,
    )
    const longest = Math.max(...ages.map((s) => s.length))
    const f = fragment(
      '<div class="ctl-head"><p class="ctl-crumb"><span class="ctl-crumb-at">Capacity</span><span class="ctl-crumb-sep">▸</span><span class="ctl-crumb-at">Profile headroom</span></p><span class="ctl-head-age">newest read 5s ago</span></div>',
    )
    // MUTATION: drop the age's `min-width`, or make it narrower than `longest`.
    expect(ch(won(pick(f, '.ctl-head-age'), 'min-width', WIDE)), ages.join(' | ')).toBeGreaterThanOrEqual(longest)
    // MUTATION: `flex-wrap: wrap` back, or the ellipsis off the last segment.
    expect(won(pick(f, '.ctl-crumb'), 'flex-wrap', PHONE)).toBe('nowrap')
    const last = pick(f, '.ctl-crumb > :last-child')
    expect(won(last, 'white-space', PHONE)).toBe('nowrap')
    expect(won(last, 'text-overflow', PHONE)).toBe('ellipsis')
    expect(won(last, 'min-width', PHONE)).toBe('0')
  })

  it('CH-16: in the open dock only the body gives way', () => {
    // MUTATION: drop `flex: none` from the line or the grip.
    const f = fragment(
      '<div class="ctl-dock"><div class="ctl-dock-grip"></div><button class="ctl-dock-line">x</button><div class="ctl-dock-body"></div></div>',
    )
    expect(shrinkOf(pick(f, '.ctl-dock-line'), PHONE)).toBe(0)
    expect(shrinkOf(pick(f, '.ctl-dock-grip'), PHONE)).toBe(0)
    expect(shrinkOf(pick(f, '.ctl-dock-body'), PHONE), 'the body is the part that scrolls').toBeGreaterThan(0)
  })

  it('OV-3: every rule that gives a `.ctl-util` a grid template also makes it a grid', () => {
    // The primitive is a wrapping FLEX line (§B6.2), so a template without
    // `display: grid` beside it is inert -- which is what Overview's ≥900
    // override was, and what the drawer's variant was before it. A property
    // of the sheet rather than of one rule, so the next override is covered
    // the moment it is written. MUTATION: delete `display: grid` from either.
    const templated = flatRules(STYLES).filter(
      (r) =>
        splitTop(r.selector).some((b) => /\.ctl-util\s*$/.test(b)) &&
        declarations(r.body).some((d) => d.property === 'grid-template-columns' || d.property === 'grid-template-areas'),
    )
    expect(templated.length, 'fewer templated `.ctl-util` rules than the drawer and Overview').toBeGreaterThanOrEqual(2)
    for (const r of templated) {
      const display = declarations(r.body).find((d) => d.property === 'display')?.value
      expect(display, `\`${r.selector}\` ${r.conditions.join(' ')} declares a template and no grid`).toBe('grid')
    }
    // "five-hour · 2m ago" and its kin are up to 19 characters. MUTATION: the
    // override's last track or the primitive's basis back under 19ch.
    const f = fragment(
      '<div class="ov-group"><div class="ctl-card-body"><div class="ctl-util"><span class="ctl-util-name">x</span><span class="ctl-util-by">five-hour · 2m ago</span></div></div></div>',
    )
    const last = splitTop(won(pick(f, '.ctl-util'), 'grid-template-columns', WIDE) ?? '', ' ').at(-1) ?? ''
    expect(ch(minmax(last)[0]), `Overview's last track is ${last}`).toBeGreaterThanOrEqual(19)
    const basis = splitTop(won(pick(f, '.ctl-util-by'), ['flex', 'flex-basis'], { width: 800 }) ?? '', ' ').at(-1)
    expect(ch(basis), `the primitive's provenance basis is ${basis}`).toBeGreaterThanOrEqual(19)
  })

  it('OV-13: every metric label reserves the mark\'s width, painted or not', () => {
    // MUTATION: generate the `::after` only on `.is-alert` / `.is-good` again.
    const f = fragment(
      '<div class="ctl-metric"><span class="ctl-metric-label">Running</span></div>' +
        '<div class="ctl-metric is-alert"><span class="ctl-metric-label">Over ceiling</span></div>',
    )
    const [plain, alert] = [...f.querySelectorAll('.ctl-metric-label')]
    expect(won(plain!, 'content', WIDE, 'after'), 'an unpainted label reserves nothing').toBe("''")
    for (const prop of ['width', 'margin-left', 'display']) {
      expect(won(plain!, prop, WIDE, 'after'), prop).toBe(won(alert!, prop, WIDE, 'after'))
    }
  })

  it('TS-7: an offer inside a workflow step is a step off the step card', () => {
    // MUTATION: drop the `--surface` fill from `.wfb-step .sbf-offer`.
    const f = fragment('<div class="app"><div class="wfb-step"><button class="sbf-offer">url</button></div></div>')
    const card = won(pick(f, '.wfb-step'), ['background', 'background-color'], WIDE)
    const offer = won(pick(f, '.sbf-offer'), ['background', 'background-color'], WIDE)
    expect(card).not.toBeNull()
    expect(offer, 'the offer is painted in its own card\'s fill, at zero contrast').not.toBe(card)
  })

  it('TS-13: the submit form selects without the accent and spends none on emphasis', () => {
    // MUTATION: the blue border/tint back on `.is-on`, the count back to
    // `--info`, or the consequence rule back to `--info`.
    const f = fragment(
      '<div class="dsp-options"><label class="dsp-option is-on"><span class="dsp-count">1 pull request</span></label></div>' +
        '<div class="dsp-consequence"></div>',
    )
    const on = pick(f, '.dsp-option')
    for (const props of [['background', 'background-color'], ['border-left-color', 'border-left', 'border-color', 'border']]) {
      expect(won(on, props, WIDE) ?? '', props.join('/')).not.toContain('--info')
    }
    expect(won(on, ['border-left-color', 'border-left', 'border-color', 'border'], WIDE)).toBe('var(--text)')
    expect(won(on, ['background', 'background-color'], WIDE)).toBe('var(--surface-2)')
    expect(won(pick(f, '.dsp-count'), 'color', WIDE)).toBe('var(--text)')
    expect(won(pick(f, '.dsp-consequence'), ['border-left', 'border-left-color'], WIDE)).toContain('var(--text-faint)')
  })

  it('TS-21: the spacing the QA pass found between steps is back on the scale', () => {
    // The four steps and the chrome padding, as `move 6` above pins them.
    const scale = new Set(['0', 'var(--ctl-s1)', 'var(--ctl-s2)', 'var(--ctl-s3)', 'var(--ctl-s5)', 'var(--ctl-pad-chrome)'])
    // MUTATION: any of 24/16/14/10/11/13/18/6px back in these rules.
    for (const sel of ['.sub', '.window-bar', '.tiles', '.tile', '.dsp', '.dsp-options', '.wfb-stage + .wfb-stage']) {
      const rules = flatRules(STYLES).filter((r) => r.conditions.length === 0 && r.selector === sel)
      expect(rules.length, `no top-level rule for ${sel}`).toBeGreaterThan(0)
      for (const r of rules) {
        for (const d of declarations(r.body)) {
          if (!/^(margin|padding|gap|row-gap|column-gap)(-|$)/.test(d.property)) continue
          for (const v of splitTop(d.value, ' ')) {
            expect(scale.has(v), `${sel} { ${d.property}: ${d.value} } -- ${v} is off the scale`).toBe(true)
          }
        }
      }
    }
  })

  it('TS-22: the "still open" legend key is the hatch its segment is', () => {
    // MUTATION: give the key its own flat fill again.
    const f = fragment('<p class="chart-legend"><i class="k open"></i></p><div class="stackcol"><i class="open"></i></div>')
    const bar = won(pick(f, '.stackcol > i'), ['background', 'background-image'], WIDE)
    expect(bar).toMatch(/gradient/)
    expect(won(pick(f, '.k'), ['background', 'background-image'], WIDE)).toBe(bar)
  })

  it('TS-24: the dependency disclosure has a marker, a hover and a focus ring', () => {
    // MUTATION: delete any of the four rules.
    const f = fragment(
      '<details class="wfb-more"><summary>waits for the stage above</summary></details>' +
        '<details class="wfb-more" open><summary>waits for</summary></details>',
    )
    const [shut, open] = [...f.querySelectorAll('summary')]
    expect(won(shut!, 'content', WIDE, 'before') ?? '', 'no marker on a closed disclosure').toContain('25B8')
    expect(won(open!, 'content', WIDE, 'before') ?? '', 'no marker on an open one').toContain('25BE')
    expect(won(shut!, 'color', { ...WIDE, states: ['hover'] })).toBe('var(--text)')
    expect(won(shut!, 'outline', { ...WIDE, states: ['focus-visible'] }) ?? '', 'no focus ring').toContain('var(--info)')
  })

  it('CP-18: the family tables and the profile tables each share one set of columns', () => {
    // `table-layout: fixed` takes the widths from the head row, which is the
    // same in every table of a screen, so the columns line up down the page.
    // MUTATION: drop `table-layout: fixed`, or the head widths, from either.
    const pools = fragment(
      '<div class="cap-families"><div class="ctl-card"><div class="ctl-card-body"><div class="ctl-table is-stacked"><table>' +
        '<thead><tr><th>Pool</th><th class="is-num">In use (units)</th></tr></thead></table></div></div></div></div>',
    )
    const profiles = fragment(
      '<section class="section panel"><dl class="kv"></dl><div class="table-wrap is-stacked"><table class="pools">' +
        '<thead><tr><th>Pool it must clear</th><th>Scope</th><th class="n">Units free</th></tr></thead></table></div></section>',
    )
    for (const [label, host, num] of [
      ['Pools', pools, 'th.is-num'],
      ['Profile headroom', profiles, 'th.n'],
    ] as const) {
      expect(won(pick(host, 'table'), 'table-layout', WIDE), label).toBe('fixed')
      expect(won(pick(host, 'th'), 'width', WIDE), `${label}: the name column`).toMatch(/%$/)
      expect(won(pick(host, num), 'width', WIDE), `${label}: a figure column`).toMatch(/%$/)
    }
  })

  it('CP-19: the fields in an open account take the panel\'s width', () => {
    // `.limit-edit input { width: 74px }` (0,1,1) beat `.acct-wide` (0,1,0).
    // MUTATION: lower the account rules' specificity, or drop them.
    const f = fragment(
      '<div class="acct-action"><span class="limit-edit"><input class="mono acct-wide"><button>save lending</button></span></div>' +
        '<div class="acct-action danger"><span class="limit-edit"><input class="mono"><button class="danger">remove</button></span></div>',
    )
    const [wide, confirm] = [...f.querySelectorAll('input')]
    expect(won(wide!, 'width', WIDE), 'the lending field').not.toBe('74px')
    const grow = cascade(STYLES, wide!, ['flex', 'flex-grow'], WIDE).winner
    expect(Number(grow?.property === 'flex' ? grow.value.split(/\s+/)[0] : grow?.value)).toBeGreaterThanOrEqual(1)
    expect(won(pick(f, '.limit-edit'), 'justify-self', WIDE)).toBe('stretch')
    expect(won(confirm!, 'width', WIDE), 'the confirmation field').not.toBe('74px')
    expect(ch(won(confirm!, 'min-width', WIDE))).toBeGreaterThanOrEqual(20)
    expect(won(confirm!, 'field-sizing', WIDE)).toBe('content')
  })

  it('CP-20: the fixed provider and its chip are two words, not one', () => {
    // MUTATION: drop the chip's margin.
    const f = fragment('<p class="acct-fixed mono">anthropic<span class="ctl-chip is-info"><i></i>fixed</span></p>')
    expect(won(pick(f, '.ctl-chip'), ['margin-left', 'margin'], WIDE)).toBe('var(--ctl-s2)')
  })

  it('CP-22: the em dash is one face wherever it lands', () => {
    // It inherited its family, so in an `.is-num` cell it was the mono dash and
    // elsewhere the sans one. MUTATION: drop `font-family` from `.ctl-em`.
    const f = fragment(
      '<div class="ctl-table"><table><tbody><tr><td class="is-num"><i class="ctl-em">—</i></td></tr></tbody></table></div>' +
        '<span><i class="ctl-em">—</i></span>',
    )
    const [inCell, inProse] = [...f.querySelectorAll('.ctl-em')]
    const a = cascade(STYLES, inCell!, ['font-family', 'font'], WIDE).winner
    const b = cascade(STYLES, inProse!, ['font-family', 'font'], WIDE).winner
    expect(a, 'the dash declares no face of its own and inherits its cell\'s').not.toBeNull()
    expect(a?.value).toBe(b?.value)
  })

  it('CP-23: an open account is marked as a selection, without the accent', () => {
    // MUTATION: the 3px `--info` rule back.
    const f = fragment('<div class="acct-detail"></div>')
    const rule = won(pick(f, 'div'), ['border-left', 'border-left-color'], WIDE) ?? ''
    expect(rule).toContain('var(--text)')
    expect(rule).not.toContain('--info')
    expect(won(pick(f, 'div'), ['background', 'background-color'], WIDE)).toBe('var(--surface-2)')
  })

  it('WF-8: the level rail\'s containing block spans the whole scrolled canvas', () => {
    // MUTATION: drop `width: max-content` from `.wf-graph`.
    const f = fragment('<div class="wf-graph"></div>')
    expect(won(pick(f, 'div'), 'width', WIDE)).toBe('max-content')
  })

  it('WF-16: the flags column is reserved on every row, and a mix chip is whole or absent', () => {
    // MUTATION: `[flags] minmax(0, auto)` back.
    const f = fragment('<button class="wf-bar"></button><span class="wf-mix"><span class="wf-chip">mock</span></span>')
    const flags = trackAfter(won(pick(f, '.wf-bar'), 'grid-template-columns', WIDE), 'flags')
    expect(flags, 'a content-sized flags track takes width from the name on the rows that have a flag').not.toMatch(
      /auto|content/,
    )
    // MUTATION: the one-line `overflow: hidden` strip back, which cut chips
    // mid-word; or drop the one-chip height that hides the second line.
    const mix = pick(f, '.wf-mix')
    expect(won(mix, 'flex-wrap', WIDE)).toBe('wrap')
    expect(won(mix, 'overflow', WIDE)).toBe('hidden')
    expect(won(mix, 'height', WIDE)).toContain('var(--lh-micro)')
    expect(won(pick(f, '.wf-chip'), 'text-overflow', WIDE)).toBe('ellipsis')
  })
})
