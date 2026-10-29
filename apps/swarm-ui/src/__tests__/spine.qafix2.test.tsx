/**
 * THE SIDEBAR'S FINDINGS FROM THE SECOND CONSOLE VISUAL-QA RUN (QAFIX2-SIDEBAR,
 * part of #1680; run wf_b6e2441b419f423a84eb, 2026-10-20, the fixture build of
 * main at 75148351, 1440 and 390 px, light and dark). Each `describe` is one
 * finding, held by the property that was false:
 *
 *   admin-007 (also overview-automate-005, task-014, work-a-008, work-b-004)
 *           The Global pool note `a pool is full: anthropic · u-bogdan`
 *           (`another pool` until visual QA pass 2, admin2-15)
 *           broke inside the tenant id at its hyphen, `u-` ending one line
 *           and `bogdan` alone on the next.
 *   FF-P1   On a phone, Overview had no "On this page" list on the page
 *           itself (it was only behind the menu), so Recent failures was
 *           reached only by scrolling past every card.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { TENANT_HEADER } from '../fetch'
import type { Capacity } from '../types'
import type { CascadeEnv } from './cssgate'
import { THEMES, build, painted } from './marks'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const WAIT = { timeout: 5000 }

const JSON_HEADERS = { 'content-type': 'application/json' }
const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: JSON_HEADERS })

const hosts: HTMLElement[] = []
afterEach(() => {
  cleanup()
  for (const h of hosts.splice(0)) h.remove()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  window.localStorage.clear()
  window.history.replaceState(null, '', '/')
})

const pool = (name: string, active: number, limit: number) => ({
  name,
  hard_limit: limit,
  adaptive_target: null,
  quota_derived_limit: null,
  effective_limit: limit,
  active,
  available: limit - active,
  enabled: true,
  updated_at: '2026-10-20T00:00:00Z',
})

/** Stub `matchMedia`, which jsdom does not have, as a phone (below 760px) or a desktop. */
function media(phone: boolean): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: phone && query.includes('759px'),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }))
}

describe('admin-007: the Global pool note does not break inside a tenant id', () => {
  it('meterOf hands the pool label over on its own, so it can be drawn whole', async () => {
    const { meterOf } = await import('../Spine')
    const cap = { pools: [pool('global', 5, 20), pool('provider:anthropic:tenant:u-bogdan', 3, 3)], runner_profiles: {} } as unknown as Capacity
    const m = meterOf(cap)!
    expect(m.warn).toBe('a pool is full: anthropic · u-bogdan')
    // MUTATION: drop `warnLabel` from meterOf and the note has no unbreakable part to draw.
    expect(m.warnLabel).toBe('anthropic · u-bogdan')
    // MUTATION: give the global pool's own note a label and "this pool" gets a nowrap span it does not need.
    expect(meterOf({ pools: [pool('global', 20, 20)], runner_profiles: {} } as unknown as Capacity)?.warnLabel).toBeNull()
  })

  it('draws each part of the pool label in a span that is never wrapped, in both themes and widths', async () => {
    const segment = build('div.sk-meter div.sk-mw span.sk-mwn span.sk-mws', hosts)
    for (const theme of THEMES) {
      for (const env of [WIDE, PHONE]) {
        // MUTATION: drop `white-space: nowrap` from `.sk-mws` in styles.css and
        // `u-bogdan` breaks at its hyphen again, `u-` / `bogdan` on two lines.
        expect(painted(segment, 'white-space', { ...env, theme }), `${theme} at ${env.width}px`).toBe('nowrap')
      }
    }
  })

  it('renders the label under the meter as one unbreakable span per part, the raw key in the title', async () => {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const chosen = new Headers(init?.headers).get(TENANT_HEADER) ?? 'u-bogdan'
      if (url.includes('/v1/tenants/mine')) return ok([{ tenant_id: 'u-bogdan', display_name: 'Bogdan' }])
      if (url.includes('/v1/tenants/me'))
        return ok({
          tenant: { tenant_id: chosen, display_name: 'Bogdan' },
          principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: false },
          environment: 'dev',
          environment_declared: true,
        })
      if (url.includes('/v1/capacity')) return ok({ pools: [pool('global', 5, 20), pool('provider:anthropic:tenant:u-bogdan', 3, 3)] })
      return ok({})
    }) as unknown as typeof fetch
    const { SkyShell } = await import('../Spine')
    render(
      <SkyShell section="work" tab="running" title="Agents" go={vi.fn()} foot={null}>
        {null}
      </SkyShell>,
    )
    const note = await waitFor(() => {
      const n = document.querySelector<HTMLElement>('.sk-meter .sk-mw')
      expect(n, 'no note under the meter').not.toBeNull()
      return n!
    }, WAIT)
    // MUTATION: render `{meter.warn}` as one text node again and the note is whole text with no span.
    expect(note.textContent).toBe('a pool is full: anthropic · u-bogdan')
    const parts = [...note.querySelectorAll('.sk-mwn .sk-mws')].map((s) => s.textContent)
    // MUTATION: wrap the whole label in one nowrap span and a long one cannot break at its `·`.
    expect(parts).toEqual(['anthropic', 'u-bogdan'])
    expect(note.getAttribute('title')).toBe('provider:anthropic:tenant:u-bogdan')
  })
})

describe('FF-P1: a phone has an "On this page" list on Overview itself', () => {
  async function overview(phone: boolean) {
    media(phone)
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
    return OVERVIEW_JUMPS
  }

  it('draws the list at the top of the page, and Recent failures jumps there and takes focus', async () => {
    const jumps = await overview(true)
    const page = document.getElementById('swarm-page')!
    // MUTATION: drop the phone `On this page` nav from the content column in
    // SkyShell and the phone's only way to Recent failures is scrolling.
    const list = within(page).getByRole('navigation', { name: 'On this page' })
    // MUTATION: put it after the page's children and it is under every card it lists.
    expect(list.compareDocumentPosition(page.querySelector('section')!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    const links = within(list).getAllByRole('link')
    // MUTATION: list fewer jumps than the panel and the two lists disagree.
    expect(links.map((a) => a.textContent)).toEqual(jumps.map((j) => j.label))
    const target = document.getElementById('ov-failures')!
    target.scrollIntoView = vi.fn()
    fireEvent.click(within(list).getByRole('link', { name: 'Recent failures' }))
    // MUTATION: call `jumpTo` instead of `onJump` and focus stays on the link.
    expect(window.location.hash).toBe('#ov-failures')
    expect(document.activeElement).toBe(target)
  })

  it('is not drawn on the desktop, where the panel already lists the sections', async () => {
    await overview(false)
    const page = document.getElementById('swarm-page')!
    // MUTATION: draw the in-page list at every width and the desktop lists every section twice.
    expect(within(page).queryByRole('navigation', { name: 'On this page' })).toBeNull()
    expect(screen.getAllByText('Recent failures')).toHaveLength(1)
  })

  it('is one scrollable row of tap-sized links on the phone', () => {
    const row = build('nav.sk-otp', hosts)
    const link = build('nav.sk-otp a.sk-oj', hosts)
    for (const theme of THEMES) {
      const env = { ...PHONE, theme }
      // MUTATION: drop `overflow-x: auto` / `flex-wrap: nowrap` from `.sk-otp` and
      // eight links stack into a block taller than the card it skips.
      expect(painted(row, 'display', env)).toBe('flex')
      expect(painted(row, 'flex-wrap', env)).toBe('nowrap')
      expect(painted(row, 'overflow-x', env)).toBe('auto')
      // MUTATION: drop `min-height: 44px` from `.sk-oj` and a link is smaller than a fingertip.
      expect(painted(link, 'min-height', env)).toBe('44px')
      expect(painted(link, 'white-space', env)).toBe('nowrap')
    }
  })
})
