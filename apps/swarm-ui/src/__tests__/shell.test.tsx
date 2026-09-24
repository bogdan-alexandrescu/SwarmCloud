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
import { describe, expect, it, vi } from 'vitest'
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
    vi.doMock('../api', () => ({ loadStats }))
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
    const z = /z-index:\s*(\d+)/.exec(close)?.[1]
    expect(z, 'the ✕ must rank above the band it sits on, not default to auto').toBe('2')
    expect(
      /float:\s*right/.test(close),
      'floated, the ✕ shares its line with the pane tabs and the band covers both',
    ).toBe(false)
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
