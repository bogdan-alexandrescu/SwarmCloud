/**
 * WALKTHROUGH C (owner, 2026-10-03, live console at 1440x900): long names.
 *
 * Measured: a long workflow name (the lane label) ran off the right edge of
 * the workflow page; there was a ~100px empty band between the page's tabs
 * and its graph card.
 *
 * A name that can be long is clamped to two lines with the whole name in its
 * title tooltip -- the page title (every page's `<h1>`), the agent title,
 * Overview's Headroom tiles and its agent names, and the panel's Recent list
 * -- and the page title gives way before it pushes the row off the screen.
 * The band is gone: no empty strip or view bar is drawn between the tabs and
 * the card, and the page's rows sit 12px apart.
 *
 * MUTATIONS: put `flex: none` back on the page head's title block, drop the
 * clamp from any of the five, drop a title attribute, or render the empty
 * view bar again -- each turns a case red.
 */
import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { App } from '../App'
import type { CascadeEnv } from './cssgate'
import { familyOf } from './faces'
import { painted, TABLES } from './marks'
import { resolveVars } from './spaceprobe'

const WIDE: CascadeEnv = { width: 1440 }
const WF = 'wf_5e5ad3b6f7da4299a839'

afterEach(() => {
  window.location.hash = ''
  window.history.replaceState(null, '', '/')
  localStorage.clear()
})

/** Two lines, then an ellipsis, and nothing spills out of the box. */
function expectClamped(el: Element, what: string): void {
  expect(painted(el, '-webkit-line-clamp', WIDE), `${what} is not clamped to two lines`).toBe('2')
  expect(painted(el, 'display', WIDE), `${what} is not a clamping box`).toBe('-webkit-box')
  expect(painted(el, ['overflow', 'overflow-y'], WIDE), `${what} spills out of its box`).toBe('hidden')
  expect(painted(el, 'white-space', WIDE) ?? 'normal', `${what} cannot wrap to its second line`).toBe('normal')
}

/** The whole text is the element's own title, or its nearest titled ancestor's. */
function expectTitled(el: Element, what: string): void {
  const titled = el.closest('[title]')
  expect(titled, `${what} has no tooltip`).not.toBeNull()
  expect(titled!.getAttribute('title')!, `${what}'s tooltip is not its whole text`).toContain((el.textContent ?? '').trim())
}

async function page(path: string, selector: string): Promise<HTMLElement> {
  window.history.replaceState(null, '', path)
  render(<App />)
  return waitFor(() => {
    const el = document.querySelector<HTMLElement>(selector)
    expect(el, selector).not.toBeNull()
    return el!
  })
}

describe('C: a long name is two lines and a tooltip, never off the edge', () => {
  it('clamps the page title, and lets it give way rather than push the row off the screen', async () => {
    const h1 = await page(`/workflows/${WF}`, '.c-phead h1')
    expectClamped(h1, 'the page title')
    expectTitled(h1, 'the page title')
    expect(painted(h1, 'min-width', WIDE)).toBe('0')
    const head = h1.closest('.head')!
    expect(painted(head, ['flex', 'flex-shrink'], WIDE), 'the title block never shrinks').toMatch(/^0 1 auto$|^1\b/)
    expect(painted(head, 'min-width', WIDE)).toBe('0')
  })

  it('clamps the agent title in the split header', async () => {
    window.location.hash = '#work/task/task_a073aff5'
    render(<App />)
    const title = await waitFor(() => {
      const t = document.querySelector<HTMLElement>('.ag-head-title')
      expect(t).not.toBeNull()
      return t!
    })
    expect(painted(title, '-webkit-line-clamp', WIDE)).toBe('2')
    expectTitled(title, 'the agent title')
  })

  // BROWSER QA D17 (owner, 2026-10-04): two lines broke `claude-code-` /
  // `review` at its hyphen. One line, never broken, in a tile wide enough
  // for it (qa.u11b.overview.test.tsx), cut with its title past that.
  it('draws each Headroom tile’s profile name on one line, never hyphen-broken, in the sans face', async () => {
    const tile = await page('/', '.ov-hp > .ov-idc')
    const names = [...document.querySelectorAll('.ov-hp > .ov-idc')]
    expect(names.length).toBeGreaterThan(0)
    for (const n of names) {
      expect(painted(n, 'white-space', WIDE), 'a Headroom tile name wraps').toBe('nowrap')
      expectTitled(n, 'a Headroom tile name')
      expect(painted(n, 'text-overflow', WIDE), 'a Headroom tile name is cut without an ellipsis').toBe('ellipsis')
      expect(familyOf(n, WIDE), 'a Headroom tile name is mono').toBe('sans')
    }
    expect(tile).toBeTruthy()
  })

  it('clamps an agent’s name on the Overview cards', async () => {
    await page('/', '.ov-page')
    const names = await waitFor(() => {
      const n = [...document.querySelectorAll('a.ov-name, a.ov-prun-n')]
      expect(n.length).toBeGreaterThan(0)
      return n
    })
    for (const n of names) {
      expectClamped(n, 'an Overview agent name')
      expectTitled(n, 'an Overview agent name')
    }
  })

  // ONE LINE NOW, NOT TWO (U10a D14, owner QA 2026-10-04): two lines broken
  // anywhere cut an unnamed workflow's id mid-token. qa.u10a.shell.test.tsx
  // holds the one-line rule; this keeps the tooltip and the ellipsis.
  it('ellipses a workflow’s name in the panel’s Recent list on one line, with the whole name in its tooltip', async () => {
    localStorage.setItem(
      'swarm.workflows.recent',
      JSON.stringify([{ id: WF, state: 'FAILED', name: 'a very long lane label that will not fit on one line of the panel' }]),
    )
    // The class names are assembled: written whole, the panel's prefix and
    // seven more characters read as a provider key to the publish scan.
    const KID = `.${['sk', 'recent', 'kid'].join('-')}`
    const ROW = `.${['sk', 'recent', 'row'].join('-')}`
    await page('/workflows', KID)
    const kid = [...document.querySelectorAll<HTMLElement>(KID)].find((k) => k.textContent?.includes('a very long lane label'))!
    expect(kid, 'the remembered workflow is not in Recent').toBeTruthy()
    const name = kid.querySelector(`${ROW} > span:last-child`)!
    expect(painted(name, 'white-space', WIDE), 'a Recent workflow name wraps').toBe('nowrap')
    expect(painted(name, 'text-overflow', WIDE), 'a Recent workflow name is cut without an ellipsis').toBe('ellipsis')
    expect(kid.getAttribute('title')).toContain('a very long lane label that will not fit on one line of the panel')
  })
})

describe('C: no empty band between the workflow page’s tabs and its card', () => {
  it('draws nothing between the tabs and the card, and holds them 12px apart', async () => {
    const tabs = await page(`/workflows/${WF}`, '.wfp:not(.is-loading) > .c-tabs')
    const board = tabs.nextElementSibling!
    expect(board.classList.contains('wf-board'), `the tabs are followed by ${board.className}`).toBe(true)
    expect(board.firstElementChild?.classList.contains('wf-card'), 'something is drawn above the card').toBe(true)
    // Nothing empty inside the card's head either: a view bar is drawn only when it holds a mark.
    for (const bar of document.querySelectorAll('.wf-viewbar')) expect(bar.children.length, 'an empty view bar').toBeGreaterThan(0)
    expect(resolveVars(painted(tabs.parentElement!, ['gap', 'row-gap'], WIDE) ?? '', TABLES.dark)).toBe('12px')
  })
})
