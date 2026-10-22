/**
 * THE PHONE OVERVIEW'S CHIP ROW SITS IN THE PAGE'S GUTTER (QAFIX4-PHONEBAR,
 * part of #1680; visual QA run 4, wf_ecd935541ced4a539540, the fixture build
 * of main at fabe4d6c, 1440 and 390 px, light and dark).
 *
 *   N2 (overview-automate-005, the gutter and spacing part) At 390 the first
 *      chip touched the screen's left edge, and 40-50px of empty band sat
 *      between the row and the refresh / tenant line under it.
 *
 * Kept as built (owner decisions 2026-10-22): the row fades the edge that has
 * more chips past it (c33) -- held here at both edges, with the gutter not
 * counted as chips past the edge.
 */
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { CascadeEnv } from './cssgate'
import { THEMES, build, painted } from './marks'

const PHONE: CascadeEnv = { width: 390 }

const hosts: HTMLElement[] = []
afterEach(() => {
  cleanup()
  for (const h of hosts.splice(0)) h.remove()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

/** Stub `matchMedia`, which jsdom does not have, as a phone below 760px. */
function phone(): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: query.includes('759px') || query.includes('560'),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }))
}

/** The px a length resolves to: `0` or `<n>px`, anything else is a loud failure. */
function px(v: string): number {
  if (v === '0') return 0
  const m = /^(-?\d+(?:\.\d+)?)px$/.exec(v.trim())
  if (m === null) throw new Error(`not a px length: ${JSON.stringify(v)}`)
  return Number(m[1])
}

/** One side of a 1-4 value box shorthand. */
function sideOf(value: string, at: 'top' | 'right' | 'bottom' | 'left'): string {
  const v = value.trim().split(/\s+/)
  const i = { top: 0, right: 1, bottom: v.length >= 3 ? 2 : 0, left: v.length === 4 ? 3 : 1 }[at]
  return v[i] ?? v[0]!
}

/** The value the cascade draws on one side of `padding` or `margin`, from its longhand or its shorthand. */
function side(el: Element, box: 'padding' | 'margin', at: 'top' | 'right' | 'bottom' | 'left'): string {
  const v = painted(el, [`${box}-${at}`, box], PHONE)
  if (v === null) return '0'
  return v === painted(el, box, PHONE) ? sideOf(v, at) : v
}

/** The scroller as the shell draws it on a phone Overview: the chip row, then the page. */
function frame(): { row: HTMLElement; app: HTMLElement; page: HTMLElement } {
  const row = build('div.sk-main > div.ctl-scroll > nav.sk-otp', hosts)
  const app = document.createElement('div')
  app.className = 'app'
  row.parentElement!.appendChild(app)
  const page = document.createElement('div')
  page.className = 'ov-page'
  app.appendChild(document.createElement('main')).appendChild(page)
  return { row, app, page }
}

describe('N2: the phone Overview chip row starts at the page gutter (run-4 overview-automate-005)', () => {
  it('the row starts at the gutter the cards below it use', () => {
    const { row, app } = frame()
    const gutter = side(app, 'padding', 'left')
    expect(gutter, 'the page draws its own gutter').toBe('var(--app-pad)')
    // MUTATION: put `.sk-otp`'s padding back to `0 2px 4px` and the first chip
    // touches the glass edge again, 16px left of the cards.
    expect(side(row, 'padding', 'left')).toBe(gutter)
    // MUTATION: drop the inline-end padding and the last chip, Pools, scrolls
    // to rest against the glass edge instead of the gutter.
    expect(side(row, 'padding', 'right')).toBe(gutter)
    // MUTATION: give `.sk-otp` a horizontal margin instead and the row stops at
    // the gutter rather than scrolling out to the glass edge.
    expect(side(row, 'margin', 'left')).toBe('0')
    expect(side(row, 'margin', 'right')).toBe('0')
  })

  it('leaves one spacing step under the row, the step between the cards, not two', () => {
    const { row, app, page } = frame()
    const step = px(painted(page, 'gap', PHONE)!)
    expect(step, 'the cards are one 16px step apart').toBe(16)
    const under = px(side(row, 'padding', 'bottom')) + px(side(row, 'margin', 'bottom')) + px(side(app, 'padding', 'top'))
    // MUTATION: put `.sk-otp`'s `margin: 0 0 12px` back, or drop the
    // `.sk-otp + .app` rule, and the row's own 16px stacks on the page's 16px
    // top padding: the run-4 band.
    expect(under).toBe(step)
  })

  it('still fades the edge with more chips past it, in both themes', () => {
    const end = build('nav.sk-otp.is-more-end', hosts)
    const start = build('nav.sk-otp.is-more-start', hosts)
    const both = build('nav.sk-otp.is-more-start.is-more-end', hosts)
    const rest = build('nav.sk-otp', hosts)
    for (const theme of THEMES) {
      const env = { ...PHONE, theme }
      // MUTATION: drop the `.sk-otp.is-more-end` mask and the cut chip reads as the last one.
      expect(painted(end, ['mask-image', '-webkit-mask-image'], env)).toMatch(/^linear-gradient\(to right, #000 .*transparent\)$/)
      // MUTATION: drop the `.sk-otp.is-more-start` mask and a row scrolled on hides that it has a start.
      expect(painted(start, ['mask-image', '-webkit-mask-image'], env)).toMatch(/^linear-gradient\(to right, transparent, #000/)
      // MUTATION: drop the two-edged mask and a row in mid-scroll fades only one side.
      expect(painted(both, ['mask-image', '-webkit-mask-image'], env)).toMatch(/^linear-gradient\(to right, transparent, .*transparent\)$/)
      // MUTATION: mask the row at rest and a row that fits fades its last chip.
      expect(painted(rest, ['mask-image', '-webkit-mask-image'], env)).toBeNull()
    }
  })

  it('does not count the gutter as chips past either edge', async () => {
    phone()
    window.history.replaceState(null, '', '/overview')
    vi.resetModules()
    const { OVERVIEW_JUMPS, SkyShell } = await import('../Spine')
    render(
      <SkyShell section="overview" tab="now" title="Overview" go={vi.fn()} foot={null}>
        {OVERVIEW_JUMPS.map((j) => (
          <section key={j.id} id={j.id} />
        ))}
      </SkyShell>,
    )
    const row = screen.getByRole('navigation', { name: 'On this page' })
    // jsdom lays out nothing and loads no sheet: the 16px gutter is given
    // inline, and the scroll geometry by hand.
    row.style.paddingLeft = '16px'
    row.style.paddingRight = '16px'
    const at = async (scrollLeft: number) => {
      for (const [k, v] of Object.entries({ scrollWidth: 992, clientWidth: 390, scrollLeft }))
        Object.defineProperty(row, k, { configurable: true, writable: true, value: v })
      await act(async () => {
        row.dispatchEvent(new Event('scroll'))
      })
    }

    await at(10)
    // MUTATION: read `scrollLeft > 1` alone and a row nudged inside its
    // gutter fades its first chip, which is whole and in view.
    expect(row.classList.contains('is-more-start')).toBe(false)
    expect(row.classList.contains('is-more-end')).toBe(true)

    await at(40)
    // MUTATION: never set `is-more-start` and a row scrolled past its first chip says nothing of it.
    expect(row.classList.contains('is-more-start')).toBe(true)

    await at(992 - 390 - 10)
    // MUTATION: read the end without the inline-end padding and the last
    // chip, whole inside the gutter, stays faded.
    expect(row.classList.contains('is-more-end')).toBe(false)
    expect(row.classList.contains('is-more-start')).toBe(true)
  })
})
