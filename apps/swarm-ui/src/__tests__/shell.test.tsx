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

  it('renders all six sections AND every section tab, at all times', () => {
    render(<App />)
    const rail = document.querySelector('.ctl-rail')
    expect(rail, 'no rail').not.toBeNull()

    // Six sections, in one fixed order, so a position means one thing.
    const sections = [...rail!.querySelectorAll('.ctl-rail-group > .ctl-nav-link')].map(
      (b) => b.textContent?.trim(),
    )
    // `Work` and `Capacity`, not `Agents` and `Pools`. Both of those named the
    // section after its own first tab, and because both sections have more
    // than one tab the rail drew the name twice -- `Agents > Agents`,
    // `Pools > Pools` -- and so did the breadcrumb. The assertion below on
    // `Holders` is the other half: the tab could drop the word `Capacity` only
    // once the section carried it.
    expect(sections).toEqual(['Overview', 'Work', 'Runtimes', 'Capacity', 'History', 'Admin'])

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
    // Pools' five tabs are in the DOM while Overview is the open section, so
    // the rail's geometry is a constant rather than something you re-read.
    // The first text node, not `textContent`: an admin-gated tab appends the
    // word "admin" as a marker span, and folding that into the label would
    // make this assert on the marker rather than on the name.
    const tabs = [...rail!.querySelectorAll('[role="tab"]')].map((b) =>
      (b.firstChild?.textContent ?? '').trim(),
    )
    expect(tabs).toContain('Runner profiles')
    expect(tabs).toContain('Holders')
    expect(tabs).toContain('Tenants')
    // Work(4) + Capacity(5) + History(2) + Admin(2). Overview and Runtimes have
    // one pane each and draw no second level -- one tab under one section is a
    // duplicate of the section.
    expect(tabs.length).toBe(13)
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
})
