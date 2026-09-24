/**
 * THE RAIL'S TAB LABELS AGAINST THE WIDTH THE RAIL ACTUALLY GIVES THEM.
 *
 * WHY THIS FILE EXISTS. `styles.css` narrows the rail to 152px below 1280px,
 * and the note that shipped with that value said, correctly, that nothing in
 * this repository can measure rendered text at a width -- jsdom implements no
 * layout engine, so `getBoundingClientRect()` here returns zeroes and a test
 * asserting "the label fits" would pass against every string ever written.
 * The note therefore asked a human to LOOK at 1279px. Someone did, in a real
 * browser, on 2026-09-24, and found three labels clipped -- none of them the
 * one the note was worried about.
 *
 * So this file is not a measurement. It is the guard that makes the measurement
 * REPEAT when it stops being valid. It holds the numbers a browser produced,
 * pinned to the exact label set they were taken against, and it fails the build
 * the moment that set changes -- which is the only event that can invalidate
 * them. A renamed or added tab does not get to inherit someone else's
 * measurement, which is exactly what happened when "Runner profiles" became
 * "Profile headroom" and the 152px kept the old label's clearance.
 *
 * WHAT WAS MEASURED, AND HOW. Chromium on macOS, the font this sheet resolves
 * `500 var(--t-body)/var(--lh-body) var(--font)` to (`500 14px/21px
 * ui-sans-serif`), each label rendered in a replica of the rail's own box:
 * a 152px column, `.ctl-rail-tabs` margin-left 12px, button border-left 2px and
 * padding 4px 10px, `box-sizing: border-box` as the sheet's reset sets it. Two
 * independent methods agreed to the pixel -- the arithmetic below and a DOM
 * replica reporting `scrollWidth > clientWidth` -- which is why these are
 * recorded as measurements rather than as estimates.
 *
 * THE NUMBERS ARE PLATFORM-SPECIFIC AND THAT IS ACCEPTED. `ui-sans-serif`
 * resolves to a different face on Windows and Linux, so these widths are one
 * platform's. They are still the right guard: this product's readers are on a
 * console, the margins below are tens of pixels rather than tenths, and a
 * measurement on one real browser is strictly better than the zero
 * measurements this had before.
 */
import { describe, expect, it } from 'vitest'

// `?raw` rather than `node:fs`, for the reason brand.test.tsx gives: it is
// Vite's own loader, so a moved or renamed sheet fails to resolve here instead
// of quietly reading a stale copy from disk.
import STYLES from '../styles.css?raw'
import { SECTIONS } from '../App'

/**
 * Every label the rail renders as a TAB, in the sheet's source order.
 *
 * A single-tab section draws no second level (`Rail` renders the strip only
 * when `tabs.length > 1`), so Overview's one tab is not a rail tab and is not
 * measured. Taking this from `SECTIONS` rather than listing it again is what
 * makes the set assertion below meaningful: if it were restated here, a rename
 * would update both copies and the test would go on passing.
 */
function railTabLabels(): string[] {
  return SECTIONS.filter((s) => s.tabs.length > 1).flatMap((s) => s.tabs.map((t) => t.label))
}

/**
 * The rendered width of each tab's contents at `500 14px/21px ui-sans-serif`,
 * INCLUDING the inline `admin` badge where the tab carries one.
 *
 * THE BADGE IS THE PART THE ORIGINAL NOTE MISSED. It is a child of the button,
 * in the inline flow, `39.14px` of mono `admin` plus a `5px` margin -- 44.14px
 * that the longest-label-string reasoning never counted. It is why the two
 * widest entries here are not the two longest words.
 */
const MEASURED_PX: Readonly<Record<string, number>> = {
  Agents: 46.34,
  Workflows: 69.13,
  Timeline: 55.8,
  'Submit a task': 89.91,
  'Submit a workflow': 121.73,
  Pools: 36.2,
  Runtimes: 61.98,
  'Profile headroom': 113.02,
  Holders: 51.55,
  Accounts: 61.91,
  'Provider quota': 141.08, // 96.94 of text + 44.14 of badge
  'Pool limits': 111.97, // 67.83 + 44.14
  Tenants: 96.26, // 52.12 + 44.14
  'Platform counts': 149.19, // 105.05 + 44.14
}

/**
 * The widest single WORD in the set, which is the figure wrapping depends on.
 *
 * A wrapped line can break between words but not inside one, so the thing that
 * would still clip after the fix is a word wider than the content box. The
 * widest is "Workflows" at 69.13px against 118px of content -- 48.87px of
 * clearance, which is why `white-space: normal` is a complete fix here and not
 * a narrower one that happens to work.
 */
const WIDEST_WORD = { word: 'Workflows', px: 69.13 }

/** The sheet's own numbers, asserted below so the arithmetic cannot go stale. */
const RAIL_W_NARROW = 152
const TABS_MARGIN_LEFT = 12 // --ctl-s3
const BUTTON_BORDER_LEFT = 2
const BUTTON_PADDING_X = 10

/** 152 - 12 - 2 - 20 = 118px. `box-sizing: border-box` for every box in it. */
const CONTENT_PX =
  RAIL_W_NARROW - TABS_MARGIN_LEFT - BUTTON_BORDER_LEFT - BUTTON_PADDING_X * 2

/**
 * One `@media` block's body, brace-matched.
 *
 * NOT A REGEX OVER THE WHOLE SHEET, for the reason `keyboard.ts` gives about
 * `rules()`: nested blocks and commas inside parentheses break the obvious
 * pattern, and they break it by matching LESS rather than by erroring -- which
 * here would report the wrap rule as absent and fail honestly, or report the
 * enclosing block as absent and fail confusingly. Brace-matching is exact.
 */
function mediaBody(css: string, query: string): string | null {
  const at = css.indexOf('@media ' + query)
  if (at === -1) return null
  const open = css.indexOf('{', at)
  if (open === -1) return null
  let depth = 1
  for (let i = open + 1; i < css.length; i += 1) {
    if (css[i] === '{') depth += 1
    else if (css[i] === '}') {
      depth -= 1
      if (depth === 0) return css.slice(open + 1, i)
    }
  }
  return null
}

describe('the rail is wide enough for the labels it renders', () => {
  /**
   * MUTATION THIS CATCHES: rename a tab, add a tab, or remove one, and the
   * measured table no longer describes what ships. The message says to
   * re-measure rather than to edit the table, because editing the table is how
   * a guess gets laundered into a recorded measurement.
   */
  it('has a browser measurement for exactly the labels it renders', () => {
    const rendered = railTabLabels().slice().sort()
    const measured = Object.keys(MEASURED_PX).sort()
    expect(
      rendered,
      'A rail tab label changed. These widths were measured in a real browser ' +
        'at 1279px and cannot be carried over to a different string: re-measure ' +
        'the set and update MEASURED_PX. See the header of this file for the method.',
    ).toEqual(measured)
  })

  /**
   * MUTATION THIS CATCHES: narrowing `--rail-w` further, shrinking the button's
   * padding, or changing `--ctl-s3`, any of which moves the 118px this file
   * reasons about while leaving the number here unchanged.
   */
  it('derives its content width from the values the sheet still declares', () => {
    expect(STYLES).toMatch(
      /@media \(max-width: 1279px\) \{ :root \{ --rail-w: 152px; \} \}/,
    )
    expect(STYLES).toMatch(/--ctl-s3:\s*12px/)

    const tabs = STYLES.match(/\.ctl-rail-tabs \{[^}]*\}/)?.[0] ?? ''
    expect(tabs).toMatch(/margin:\s*2px 0 2px var\(--ctl-s3\)/)

    // ALL the blocks for this selector, not the first one. The sheet now carries
    // three: the base rule, the wrap rule in the 900-1279px band, and the
    // `width: auto` override below 900px. Taking `match()[0]` read whichever
    // happened to come first in the file, which is how this assertion would
    // silently start checking the wrap block for a padding it does not set.
    const buttons = STYLES.match(/\.ctl-rail-tabs button \{[^}]*\}/g) ?? []
    const base = buttons.filter((b) => /min-height:\s*26px/.test(b))
    expect(
      base,
      'The base `.ctl-rail-tabs button` rule is the one carrying `min-height: ' +
        '26px`; it is the box the 118px below is derived from.',
    ).toHaveLength(1)
    expect(base[0]).toMatch(/padding:\s*4px 10px/)
    expect(base[0]).toMatch(/border-left:\s*2px solid transparent/)
    expect(CONTENT_PX).toBe(118)
  })

  /**
   * The defect, recorded as an assertion so it cannot be quietly reintroduced
   * by someone who reads the fix as cosmetic.
   *
   * MUTATION THIS CATCHES: restoring `white-space: nowrap` on the rail tabs in
   * the 900-1279px band, or deleting that media block. Either brings back the
   * ellipsis, and with it a "Platform counts" whose `admin` badge -- the only
   * warning a non-admin gets that the tab opens a gate -- is cut off entirely.
   */
  it('lets the three labels that do not fit 152px wrap instead of clipping', () => {
    const tooWide = Object.entries(MEASURED_PX)
      .filter(([, px]) => px > CONTENT_PX)
      .map(([label]) => label)
      .sort()
    // Measured, not assumed: these three exceed 118px and the other eleven do
    // not. If this list ever empties, the wrap rule below has become
    // unnecessary and should be removed rather than left as decoration.
    expect(tooWide).toEqual(['Platform counts', 'Provider quota', 'Submit a workflow'])

    const band = mediaBody(STYLES, '(min-width: 900px) and (max-width: 1279px)')
    expect(
      band,
      'The 900-1279px block is gone. That is the only band where the rail is a ' +
        '152px vertical column with full-width buttons, and it is where the ' +
        'three labels above clip.',
    ).not.toBeNull()
    expect(
      band,
      'The rail tabs must wrap in this band. With `white-space: nowrap` and ' +
        '`text-overflow: ellipsis` the three labels above are CLIPPED, and what ' +
        'is clipped off two of them is the `admin` badge.',
    ).toMatch(/\.ctl-rail-tabs button \{[^}]*white-space:\s*normal/)
  })

  /**
   * MUTATION THIS CATCHES: moving the wrap block ABOVE the base
   * `.ctl-rail-tabs button` rule -- for instance up beside the `--rail-w`
   * declaration it logically belongs with, which is where it was written first
   * and where it did nothing.
   *
   * A media query adds no specificity. `@media (...) { .ctl-rail-tabs button }`
   * and `.ctl-rail-tabs button` are equally specific, so the one later in the
   * sheet wins, and the base rule sets `white-space: nowrap`. Ordered wrongly
   * this fix is invisible: the CSS parses, the media query matches, the
   * declaration is simply overridden. Nothing anywhere else in this repository
   * would notice, because noticing requires rendering at 1279px.
   */
  it('puts the wrap rule after the base rule it has to override', () => {
    const baseAt = STYLES.search(/^\.ctl-rail-tabs button \{/m)
    const bandAt = STYLES.indexOf('@media (min-width: 900px) and (max-width: 1279px)')
    expect(baseAt).toBeGreaterThan(-1)
    expect(bandAt).toBeGreaterThan(-1)
    expect(
      bandAt,
      'The 900-1279px wrap block must come AFTER `.ctl-rail-tabs button`. A ' +
        'media query carries no extra specificity, so placed before it the ' +
        '`white-space: normal` loses to the base rule\'s `white-space: nowrap` ' +
        'and the three labels clip exactly as they did before the fix.',
    ).toBeGreaterThan(baseAt)
  })

  /**
   * MUTATION THIS CATCHES: a future tab label containing a single word wider
   * than the content box -- which wrapping cannot rescue, because a line breaks
   * between words and not inside one. Such a label needs a shorter word, not a
   * wider rail.
   */
  it('contains no single word too wide to wrap', () => {
    expect(WIDEST_WORD.px).toBeLessThan(CONTENT_PX)
    const longestRendered = railTabLabels()
      .flatMap((l) => l.split(' '))
      .reduce((a, b) => (b.length > a.length ? b : a), '')
    // A proxy for width, and deliberately a weak one: character count cannot
    // order two different words by rendered width. It is here only to notice a
    // label whose longest word is longer than the one that was MEASURED, which
    // is the case where WIDEST_WORD has stopped being the widest and the
    // measurement has to be retaken.
    expect(
      longestRendered.length,
      `"${longestRendered}" is longer than the measured widest word ` +
        `"${WIDEST_WORD.word}" (${WIDEST_WORD.px}px). Re-measure the widest word.`,
    ).toBeLessThanOrEqual(WIDEST_WORD.word.length)
  })
})
