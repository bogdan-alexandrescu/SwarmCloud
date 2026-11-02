/**
 * ITEM 3 OF LANE U9 (owner, 2026-10-03, live console at 1440x900): on the
 * workflow graph page the WINDOW scrolled past the app frame into blank grey.
 *
 * The frame is `html, body, #root { height: 100% }` with the page's scroller
 * (`.ctl-scroll`, `overflow: auto`) inside it, so nothing in a page should be
 * able to make the document taller than the viewport. Something did: the
 * visually hidden words (`.sk-vh`, and `.sk-st-w` on a compact agent row) are
 * `position: absolute`, and the scroller was not positioned. An absolute box
 * is laid out against its CONTAINING BLOCK -- the nearest positioned
 * ancestor, which was `.sk-app`, OUTSIDE the scroller -- at its static
 * position, which for a hidden state word in the Steps table is a couple of
 * thousand pixels down the page. A box whose containing block is outside a
 * scroll container is not that container's overflow: it was the document's,
 * and the window grew to reach it.
 *
 * WHAT IS ASKED, IN CASCADE TERMS (jsdom lays nothing out, spaceprobe.ts
 * measured it): `document.scrollHeight == innerHeight` holds exactly when no
 * box escapes the frame. The frame is a fixed 100% chain whose scroller
 * clips; so the claim is that EVERY absolutely positioned element rendered
 * inside the scroller has its containing block at or inside the scroller,
 * where the scroller's own overflow takes it. Asked on the workflow page, the
 * agent page and the timeline page.
 *
 * MUTATION: delete `position: relative` from `.ctl-scroll` in styles.css --
 * every case goes red, naming the `.sk-vh` / `.sk-st-w` spans.
 */
import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { App } from '../App'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WIDE: CascadeEnv = { width: 1440 }

afterEach(() => {
  window.location.hash = ''
  window.history.replaceState(null, '', '/')
  localStorage.clear()
})

/** The nearest ancestor the cascade positions (anything but static), or null. */
function containingBlock(el: Element): Element | null {
  for (let n = el.parentElement; n !== null; n = n.parentElement) {
    const p = painted(n, 'position', WIDE)
    if (p !== null && p !== 'static') return n
  }
  return null
}

/** Every absolutely positioned element in the scroller whose containing block is outside it. */
function escapes(): { checked: number; out: string[] } {
  const scroll = document.querySelector('.ctl-scroll')
  expect(scroll, 'no page scroller').not.toBeNull()
  let checked = 0
  const out: string[] = []
  for (const el of scroll!.querySelectorAll('*')) {
    if (painted(el, 'position', WIDE) !== 'absolute') continue
    checked++
    const cb = containingBlock(el)
    if (cb === null || !scroll!.contains(cb)) {
      out.push(`${el.tagName.toLowerCase()}.${el.getAttribute('class')} ("${(el.textContent ?? '').trim()}") is laid out against ${cb === null ? 'the viewport' : `${cb.tagName.toLowerCase()}.${cb.getAttribute('class')}`}`)
    }
  }
  return { checked, out }
}

function frameHolds(): void {
  for (const sel of ['html', 'body', '#root']) {
    const el = document.querySelector(sel)
    if (el !== null) expect(painted(el, 'height', WIDE), `${sel} is not the viewport's height`).toBe('100%')
  }
  const scroll = document.querySelector('.ctl-scroll')!
  expect(painted(scroll, ['overflow-y', 'overflow'], WIDE), 'the page scroller does not clip').toBe('auto')
}

describe('item 3: no box escapes the app frame, so the window never scrolls', () => {
  // The Steps table is the Table tab's alone since visual QA pass 1
  // (overview-agents-73, #2596): the Graph tab no longer draws it under the graph.
  it('the workflow table page', async () => {
    window.history.replaceState(null, '', '/workflows/wf_5e5ad3b6f7da4299a839/table')
    render(<App />)
    await waitFor(() => expect(document.querySelectorAll('.wf-card .wf-table tbody tr[data-step]').length).toBeGreaterThan(1))
    // The Table tab's Steps table writes its state words out, so the hidden
    // words on this page are the step inspector's (its head and its scrub
    // strip), and the inspector can draw after the rows: on a loaded CI runner
    // the rows were there and no absolute box yet (PR 2633's merge run). Wait
    // for the boxes this case measures, not only for the table.
    await waitFor(() => expect(document.querySelectorAll('.ctl-scroll .wf-inspect .sk-vh').length).toBeGreaterThan(0))
    frameHolds()
    const { checked, out } = escapes()
    // The hidden state words in the step inspector are the boxes that escaped.
    expect(checked, 'no absolutely positioned element was visited').toBeGreaterThan(0)
    expect(out).toEqual([])
  }, 60_000)

  it('the agent page', async () => {
    window.location.hash = '#work/task/task_a073aff5'
    render(<App />)
    await waitFor(() => expect(document.querySelector('.ag-split .ag-head-facts')).not.toBeNull())
    frameHolds()
    const { checked, out } = escapes()
    expect(checked, 'no absolutely positioned element was visited').toBeGreaterThan(0)
    expect(out).toEqual([])
  }, 60_000)

  it('the timeline page', async () => {
    window.history.replaceState(null, '', '/timeline')
    render(<App />)
    await waitFor(() => expect(document.querySelector('.ctl-scroll h1')).not.toBeNull())
    frameHolds()
    expect(escapes().out).toEqual([])
  }, 60_000)
})
