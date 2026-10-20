/**
 * VISUAL QA RE-RUN 2026-10-16 (#1427), lane R15: the shell's chrome -- the
 * bell bar, the panel's sub-nav and the phone header.
 *
 * V178  With a live API the bell bar (`header.ctl-bar`) was drawn on every
 *       page holding only the bell: on a phone a second 44px sticky band
 *       under the app bar, at 1440 an empty 48px band above the page title.
 *       The bar now holds the trail alone and is drawn only where there is
 *       one (an open object); the bell lives in the shell's own chrome -- the
 *       spine's foot from 760px up, the phone header below it. The bar's
 *       height is a token, `--ctl-bar-h`, 0 where there is no bar, for the
 *       sticky table heads that must sit under it (V091, V187).
 * V150  Recent has no sub-nav count while the list's tab says "Recent 17".
 *       That is deliberate, but the reason was only in a hover tooltip: the
 *       row now says so in words beside it.
 * V237  The phone header and its hamburger said nothing about approvals
 *       waiting; the count appeared only once the drawer was open.
 *
 * Each `it` names the mutation that turns it red.
 */
import { fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import NOTIF_CSS from '../styles/notifications.css?raw'
import { cascade, type CascadeEnv } from './cssgate'
import { serve } from './repofixture'

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null
type Handler = (method: string, url: string, body: unknown) => Reply

const WAIT = { timeout: 5000 }
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

const FEED: Json = { items: [], unread_needs_you: 2, unread_updates: 0, admin_pending: 0, read_at: new Date().toISOString() }

/** The notification feed answers, and whatever `extra` answers; anything else is the fixture's 404. */
function live(extra: Handler = () => null): Handler {
  return (m, url, b) => {
    const hit = extra(m, url, b)
    if (hit !== null) return hit
    if (m === 'GET' && url.split('?')[0] === '/v1/notifications') return { status: 200, body: FEED }
    return null
  }
}

/** A phone: the one media query the shell and the bell ask that holds below 760px. */
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

async function app(path: string, handler: Handler = live()) {
  serve(handler)
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

const value = (el: Element, prop: string, env: CascadeEnv) => cascade(NOTIF_CSS, el, prop, env).winner?.value.trim() ?? null

describe('V178: no bell bar holding only the bell', () => {
  it('draws no header bar on a section page, and the bell sits at the spine’s foot', async () => {
    const { container } = await app('/capacity/pools')
    // MUTATION: draw `<header className="ctl-bar">` with the bell on every page again.
    const bell = await waitFor(() => {
      const b = container.querySelector('.sk-spine .sk-bell button.nt-bell')
      expect(b, 'the spine drew no bell').not.toBeNull()
      return b!
    }, WAIT)
    expect(bell.getAttribute('aria-label')).toBe('Notifications, 2 need you')
    expect(container.querySelector('.ctl-bar'), 'a header bar on a section page').toBeNull()
    expect(container.querySelectorAll('button.nt-bell'), 'one bell, not two').toHaveLength(1)
    const frame = container.querySelector('.ctl-frame')!
    expect(frame.classList.contains('has-ctl-bar')).toBe(false)
    expect(value(frame, '--ctl-bar-h', WIDE), 'no bar is 0px tall').toBe('0px')
  })

  it('opens the rail bell’s drop-down beside the spine, not clipped inside its 84px', async () => {
    const { container } = await app('/capacity/pools')
    const bell = await waitFor(() => {
      const b = container.querySelector<HTMLButtonElement>('.sk-spine button.nt-bell')
      expect(b).not.toBeNull()
      return b!
    }, WAIT)
    // The bell reads on the spine's dark ground in the spine's own ink.
    expect(value(bell, 'color', WIDE)).toBe('inherit')
    fireEvent.click(bell)
    const panel = await waitFor(() => {
      const p = container.querySelector('.sk-spine .nt-panel')
      expect(p, 'the bell opened nothing').not.toBeNull()
      return p!
    }, WAIT)
    // MUTATION: drop the `.sk-bell .nt-panel` rule; the panel is absolute at
    // the bell's right edge, inside a scrolling 84px column.
    expect(value(panel, 'position', WIDE)).toBe('fixed')
    expect(value(panel, 'left', WIDE)).toBe('92px')
    expect(value(panel, 'right', WIDE)).toBe('auto')
    expect(value(panel, 'bottom', WIDE)).toBe('12px')
    // Open, it is lit as a hovered spine item is.
    expect(value(bell, 'color', WIDE)).toBe('#fff')
  })

  it('at phone width puts the bell in the phone header, once, with no second band under it', async () => {
    phoneWidth()
    const { container } = await app('/admin/limits')
    // MUTATION: leave the bell in the spine at every width.
    const bell = await waitFor(() => {
      const b = container.querySelector('header.sk-pbar .sk-pbell button.nt-bell')
      expect(b, 'the phone header drew no bell').not.toBeNull()
      return b!
    }, WAIT)
    expect(container.querySelector('.sk-spine button.nt-bell'), 'a second bell in the drawer').toBeNull()
    expect(container.querySelector('.ctl-bar'), 'a second band under the phone header').toBeNull()
    // Light on the header's dark ground, like its menu button.
    expect(value(bell, 'color', PHONE)).toBe('#eef4fa')
  })

  it('keeps the bar for an open object’s trail, with no bell in it, and exports its height', async () => {
    const { container } = await app('/workflows/wf_broker')
    await waitFor(() => expect(container.querySelector('.ctl-bar .ctl-crumb'), 'no trail').not.toBeNull(), WAIT)
    await waitFor(() => expect(container.querySelector('.sk-spine button.nt-bell')).not.toBeNull(), WAIT)
    expect(container.querySelector('.ctl-bar .nt-bell'), 'the bell is back in the trail bar').toBeNull()
    const frame = container.querySelector('.ctl-frame')!
    // MUTATION: drop `has-ctl-bar` from the frame, or the token from the sheet.
    expect(frame.classList.contains('has-ctl-bar')).toBe(true)
    expect(value(frame, '--ctl-bar-h', WIDE)).toBe('48px')
    expect(value(frame, '--ctl-bar-h', PHONE)).toBe('44px')
    // The bar is as tall as the token says, so the two cannot drift.
    expect(value(container.querySelector('.ctl-bar')!, 'height', WIDE)).toBe('var(--ctl-bar-h, 48px)')
  })
})

describe('V237: the phone header says approvals are waiting', () => {
  const approvals = (n: number): Handler =>
    live((m, url) =>
      m === 'GET' && url.split('?')[0] === '/v1/approvals'
        ? { status: 200, body: { approvals: Array.from({ length: n }, (_, i) => ({ approval_id: `a${i}` })), tenant_id: 'eng' } }
        : null,
    )

  async function shell(n: number) {
    serve(approvals(n))
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { SkyShell } = await import('../Spine')
    return render(
      <SkyShell section="overview" tab="now" title="Overview" go={vi.fn()} foot={null}>
        {null}
      </SkyShell>,
    )
  }

  it('badges the menu button with the count, and describes it to a screen reader', async () => {
    const { container } = await shell(3)
    const menu = container.querySelector<HTMLButtonElement>('.sk-pbar button[aria-label="Open the menu"]')!
    // MUTATION: drop the badge from the phone header's menu button.
    await waitFor(() => expect(menu.querySelector('.sk-mbadge')?.textContent).toBe('3'), WAIT)
    expect(menu.querySelector('.sk-mbadge')?.getAttribute('aria-hidden')).toBe('true')
    const said = document.getElementById(menu.getAttribute('aria-describedby') ?? '')
    expect(said?.textContent).toBe('3 waiting for approval')
    // At the button's corner, ringed against the header's ground.
    const badge = menu.querySelector('.sk-mbadge')!
    expect(value(badge, 'top', PHONE)).toBe('-2px')
    expect(value(badge, 'right', PHONE)).toBe('-4px')
  })

  it('draws no badge when nothing waits', async () => {
    const { container } = await shell(0)
    await waitFor(() => expect(container.querySelector('.sk-spine a[data-sec="automate"]')).not.toBeNull(), WAIT)
    await new Promise((r) => setTimeout(r, 30))
    const menu = container.querySelector('.sk-pbar button[aria-label="Open the menu"]')!
    expect(menu.querySelector('.sk-mbadge')).toBeNull()
    expect(menu.hasAttribute('aria-describedby')).toBe(false)
  })
})

describe('V150: Recent says in words why it has no count', () => {
  // The words became "newest first" (QA-078, part of #1680): "not totalled"
  // beside a tab saying "Recent 17" read as one of the two being wrong.
  it('draws "newest first" beside Recent, muted, and still no count mark', async () => {
    const { container } = await app('/agents/live')
    const recent = await waitFor(() => {
      const k = [...container.querySelectorAll('.sk-panel .sk-kid')].find((x) => x.querySelector('span')?.textContent === 'Recent')
      expect(k, 'no Recent row').toBeDefined()
      return k!
    }, WAIT)
    // MUTATION: leave the reason in the tooltip alone.
    const note = recent.querySelector('.sk-knote')
    expect(note?.textContent).toBe('newest first')
    expect(recent.querySelector('.sk-cnt'), 'a count mark beside Recent').toBeNull()
    expect(recent.getAttribute('title')).toMatch(/nothing totals every finished agent/)
    expect(value(note!, 'color', WIDE)).toBe('var(--text-faint)')
  })
})
