/**
 * VISUAL QA 2026-10-20 (#1680), lane QAFIX3-BANNER: one banner per page.
 *
 * On a phone with an object open, two top-level `<header>` elements were
 * drawn: the spine's phone header (`header.sk-pbar`) and the trail bar
 * (`header.ctl-bar`). Each is a `banner` landmark, so axe's
 * `landmark-no-duplicate-banner` fired at 390px. The phone header is the
 * page's banner; the trail bar is a plain bar whose breadcrumb is its own
 * `nav` landmark.
 *
 * Each assertion names the mutation that turns it red.
 */
import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { serve } from './repofixture'

const WAIT = { timeout: 5000 }

/** A phone: the one media query the shell asks that holds below 760px. */
function phoneWidth() {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: query.includes('max-width: 759px'),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }))
}

const FEED = { items: [], unread_needs_you: 0, unread_updates: 0, admin_pending: 0, read_at: new Date().toISOString() }

async function app(path: string) {
  serve((m, url) => (m === 'GET' && url.split('?')[0] === '/v1/notifications' ? { status: 200, body: FEED } : null))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  window.history.replaceState(null, '', path)
  const { App } = await import('../App')
  return render(<App />)
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

/** Sectioning content and `main`: a `<header>` inside one is not the page's banner (HTML-AAM). */
const SCOPED = 'article, aside, main, nav, section'

/** Every element the accessibility tree reads as a `banner` landmark. */
function banners(root: ParentNode): Element[] {
  const found = new Set<Element>(Array.from(root.querySelectorAll('[role="banner"]')))
  for (const h of Array.from(root.querySelectorAll('header:not([role])'))) {
    if (h.parentElement?.closest(SCOPED) == null) found.add(h)
  }
  return Array.from(found)
}

describe('QAFIX3-BANNER: one banner on a phone with an object open', () => {
  it('reads the phone header as the one banner, and the trail bar as none', async () => {
    phoneWidth()
    const { container } = await app('/workflows/wf_broker')
    const bar = await waitFor(() => {
      const b = container.querySelector('.ctl-bar')
      expect(b, 'no trail bar over the open object').not.toBeNull()
      return b!
    }, WAIT)
    const found = banners(document.body)
    // MUTATION: draw the trail bar as `<header className="ctl-bar">` again: a
    // second banner, and axe's landmark-no-duplicate-banner fires at 390px.
    expect(found.length, `banners: ${found.map((e) => e.className).join(', ')}`).toBe(1)
    // MUTATION: give the trail bar `role="banner"`, or make it a `<header>`.
    expect(found, 'the trail bar is a banner').not.toContain(bar)
    expect(bar.tagName, 'the trail bar is a header element').not.toBe('HEADER')
    // MUTATION: make the phone header a `<div>` too: the page has no banner.
    expect(found[0]?.classList.contains('sk-pbar'), 'the phone header is not the banner').toBe(true)
    // MUTATION: drop the breadcrumb's `nav`: the trail is no landmark at all.
    expect(bar.querySelector('nav'), 'the trail is not its own nav landmark').not.toBeNull()
  })
})
