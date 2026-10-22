// VISUAL QA RUN 4 LANE QAFIX4-PAGEHEADS-A (part of #1680): THE ACTIONS IN
// ACCESS'S AND SETUP'S HEADS.
//
//   N4 (run-4 work-b-001)  At 390 Access's Setup link dropped onto a row of its
//        own: Refresh and the Mine / Members / Browser logins switch filled one
//        row and Setup sat alone, flush right, on the next. The switch and
//        Setup are now one wrapping group, so they move to a row together.
//   N5 (run-4 work-b-002)  Setup's GitHub "Re-check" ended 10 px inside the
//        edge the page head's Access button ends on at 1440, and sat on the
//        left beside the GitHub heading at 390 while the head's actions stayed
//        right. That Re-check is `GitHubPermissions`'s section head
//        (PermissionCards.tsx, `.c-card-h.fp-head > .ur-acts`), not a
//        GitHubConnect `GuideRow`: Setup draws no GuideRow. A ghost button's
//        words end its padding and transparent border inside its box, so the
//        head pulls that inset back; and the head's actions keep to the right
//        at 390, where the `.ur-acts` phone rule had dropped their auto margin.
//
// jsdom lays nothing out, so each case reads what the real sheets resolve to
// at the width the finding was measured (`painted`, every sheet in the
// bundle's order, with the phone rules applied at 390).
//
// Fixtures only; no network. Nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { serve } from './repofixture'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', window.location.pathname)
})

/** Every read answers 404: the heads draw before any read lands. */
function quiet() {
  serve(() => null)
}

/** The px a length the sheets give resolves to; `auto`, a var() or a calc() is a loud failure. */
function px(v: string): number {
  if (v === '0') return 0
  const m = /^(-?\d+(?:\.\d+)?)px$/.exec(v)
  if (m === null) throw new Error(`not a px length: ${JSON.stringify(v)}`)
  return Number(m[1])
}

/** The inline-end side of a 1-4 value box shorthand (`padding`, `margin`, `border-width`). */
function endOf(value: string): string {
  const parts = value.trim().split(/\s+/)
  return parts.length === 1 ? parts[0]! : parts[1]!
}

/** The box the cascade draws on an element's inline end, from its longhand or its shorthand. */
function side(el: Element, box: 'margin' | 'padding', env: CascadeEnv): number {
  // Both can be declared; the one the cascade picks among the two wins.
  const v = painted(el, [`${box}-right`, box], env)
  if (v === null) return 0
  return px(v === painted(el, box, env) ? endOf(v) : v)
}

/** The border width on an element's inline end. */
function borderEnd(el: Element, env: CascadeEnv): number {
  const v = painted(el, ['border', 'border-right', 'border-width', 'border-right-width'], env)
  if (v === null || /^(none|0)\b/.test(v)) return 0
  const lengths = v.split(/\s+/).filter((t) => /^-?\d+(?:\.\d+)?px$|^0$/.test(t))
  if (lengths.length === 0) return 0
  // `border: 1px solid x` names one width; `border-width: a b c d` names the sides.
  return px(lengths.length === 1 ? lengths[0]! : endOf(lengths.join(' ')))
}

describe('N4: Access head at 390 (run-4 work-b-001)', () => {
  async function mountAccess() {
    quiet()
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { AccessScreen } = await import('../Access')
    return render(<AccessScreen />)
  }

  it('the Mine / Members / Browser logins switch and Setup are one wrapping group, so Setup never takes a row alone', async () => {
    await mountAccess()
    const head = document.querySelector('.ac-page > .c-phead > .c-acts')
    expect(head, 'the page head draws its actions').not.toBeNull()
    const setup = screen.getByRole('link', { name: 'Setup' })
    const sw = screen.getByRole('radiogroup', { name: 'Whose access' })
    const group = setup.closest('.ur-acts')
    // MUTATION: move `<UrRadio label="Whose access">` back out of the `.ur-acts` span in Access.tsx's PageHead.
    expect(group, 'Setup sits in a `.ur-acts` group').not.toBeNull()
    expect(group!.contains(sw), 'the switch is in the same group as Setup').toBe(true)
    // The group is one item of the head's actions, so the head wraps it whole.
    // MUTATION: wrap only Setup in its own inner span again (the group stops being a direct item of the head).
    expect(group!.parentElement).toBe(head)
    // MUTATION: set `.ur-acts { display: contents }` (the group dissolves into the head's row and Setup wraps alone).
    expect(painted(group!, 'display', PHONE)).toMatch(/^(inline-)?flex$/)
    // Refresh stays outside the group, first in the head as it is on Setup.
    expect(group!.contains(document.querySelector('.c-refresh'))).toBe(false)
  })

  it('at 1440 the head still draws Refresh, the switch and Setup on one row, Setup last', async () => {
    await mountAccess()
    const group = screen.getByRole('link', { name: 'Setup' }).closest('.ur-acts')!
    // MUTATION: put Setup before the switch in the group.
    expect(group.lastElementChild?.textContent).toBe('Setup')
    // MUTATION: set `.c-phead > .c-acts { flex-wrap: wrap }` outside the phone block.
    expect(painted(group.parentElement!, 'flex-wrap', WIDE)).toBe('nowrap')
  })
})

describe("N5: Setup's GitHub Re-check (run-4 work-b-002)", () => {
  async function mountSection() {
    quiet()
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { GitHubPermissions } = await import('../PermissionCards')
    return render(<GitHubPermissions />)
  }

  it('at 390 the head keeps Re-check on the right, as the page head keeps its actions', async () => {
    await mountSection()
    const recheck = screen.getByRole('button', { name: /Re-check/ })
    const acts = recheck.closest('.ur-acts')!
    expect(acts.parentElement?.classList.contains('fp-head')).toBe(true)
    // MUTATION: drop `.fp-head > .ur-acts { margin-left: auto }` from repositories.css (the phone `.ur-acts { margin-left: 0 }` wins).
    expect(painted(acts, 'margin-left', PHONE)).toBe('auto')
    // MUTATION: give the rule a phone-only `margin-left: 0`.
    expect(painted(acts, 'margin-left', WIDE)).toBe('auto')
  })

  it('at 1440 Re-check\'s words end on the edge the cards and the page head end on: no inline-end inset', async () => {
    await mountSection()
    const recheck = screen.getByRole('button', { name: /Re-check/ })
    expect(recheck.classList.contains('is-ghost'), 'Re-check is a ghost: no ground or edge shows its box').toBe(true)
    // A ghost draws no edge, so what a reader sees end is its words: they end
    // at the box's end less its padding and its transparent border, plus any
    // margin. Those three must sum to nothing.
    const inset = side(recheck, 'margin', WIDE) + borderEnd(recheck, WIDE) + side(recheck, 'padding', WIDE)
    // MUTATION: drop `.fp-head > .ur-acts > .c-btn.is-ghost { margin-right: ... }` (the inset is the 10 px padding and the 1 px border).
    expect(inset).toBe(0)
    // And at 390, where the head's actions are on the right too.
    // MUTATION: scope the margin rule to `@media (min-width: 720px)`.
    expect(side(recheck, 'margin', PHONE) + borderEnd(recheck, PHONE) + side(recheck, 'padding', PHONE)).toBe(0)
  })
})
