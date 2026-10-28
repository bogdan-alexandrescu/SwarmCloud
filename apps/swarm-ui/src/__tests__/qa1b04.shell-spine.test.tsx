/**
 * VISUAL QA PASS 1b, LANE QA1B-04 (part of #2596): the pass-1 findings whose
 * fix sat in Shell.tsx or Spine.tsx, outside the lane that found them,
 * applied here from that lane's patch.
 *
 *   * overview-agents-79 (QA1-FIX-30): a screen that does not poll says so,
 *     `read once · 1 min ago`, not a bare age that reads as a stalled cadence.
 *   * admin-shell-19 (QA1-FIX-33): a capacity screen's slower phone cadence
 *     carries its reason into the head, `every 1 min on a phone`.
 *   * admin-shell-12 (QA1-FIX-25): a 404 near nothing is never lit as Overview.
 *   * admin-shell-23 (QA1-FIX-31, BGD 2026-10-28, reversing CONFIG-NAV for this
 *     case): someone who administers nothing, at an Admin address, is refused
 *     in place, not sent to Overview.
 *
 * automate-25 (QA1-FIX-38) is pinned by its own lane's file,
 * qafix38.shell-answered.test.tsx. Fixtures only; no network.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { RefreshControl, Screen } from '../Shell'

type Json = Record<string, unknown>

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  localStorage.clear()
  sessionStorage.clear()
  window.history.replaceState(null, '', '/')
})

describe('overview-agents-79: a screen read once says so', () => {
  it('reads `read once · 0 s ago`, and a polling screen keeps its cadence', () => {
    const now = Date.now()
    const { container, rerender } = render(<RefreshControl readAt={now} now={now} onRefresh={() => {}} />)
    // MUTATION: drop `once · ` and the bare age reads as a cadence that stalled.
    expect(container.querySelector('.c-refresh')!.textContent).toBe('⟳ read once · 0 s ago')
    rerender(<RefreshControl readAt={now} now={now} cadence={{ base: 30_000, wait: 30_000 }} onRefresh={() => {}} />)
    expect(container.querySelector('.c-refresh')!.textContent).toBe('⟳ every 30 s · read 0 s ago')
  })
})

describe('admin-shell-19: the head says why a cadence is what it is', () => {
  it('carries a poll’s `note` into `every 1 min on a phone`', async () => {
    const poll = Object.assign(() => 60_000, { note: () => 'on a phone' })
    render(
      <Screen<{ n: number }> title="Pool limits" load={async () => ({ status: 'ok', data: { n: 1 }, fetchedAt: Date.now() })} pollMs={poll}>
        {(d) => <p>{d.n}</p>}
      </Screen>,
    )
    // MUTATION: leave `note` out of `setCadence` and the head says `every 1 min` with no reason.
    await waitFor(() => expect(document.querySelector('.c-refresh')!.textContent).toMatch(/^⟳ every 1 min on a phone · read \d+ s ago$/))
  })

  it('says nothing more for a poll with no note', async () => {
    render(
      <Screen<{ n: number }> title="Pools" load={async () => ({ status: 'ok', data: { n: 1 }, fetchedAt: Date.now() })} pollMs={30_000}>
        {(d) => <p>{d.n}</p>}
      </Screen>,
    )
    await waitFor(() => expect(document.querySelector('.c-refresh')!.textContent).toMatch(/^⟳ every 30 s · read \d+ s ago$/))
  })
})

describe('admin-shell-12: a 404 near nothing lights no Overview', () => {
  it('draws no section lit for /zzzzzzzzzz', async () => {
    const { App } = await import('../App')
    window.history.replaceState(null, '', '/zzzzzzzzzz')
    render(<App />)
    await screen.findByRole('heading', { name: /No page at \/zzzzzzzzzz/ })
    await waitFor(() => expect(document.querySelector('.sk-ri[data-sec="overview"]')).not.toBeNull())
    // MUTATION: light `section === s.key` alone and Overview is lit over an empty panel.
    const lit = [...document.querySelectorAll('.sk-ri[data-sec].is-on')].map((a) => a.getAttribute('data-sec'))
    expect(lit).toEqual([])
  })
})

describe('admin-shell-23: someone who administers nothing is refused in place', () => {
  const ME: Json = {
    tenant: { tenant_id: 'eng', kind: 'group', display_name: 'eng@example.com' },
    principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: false },
    environment: 'dev',
    environment_declared: true,
  }
  const MINE: Json[] = [{ tenant_id: 'eng', display_name: 'eng@example.com', kind: 'group', via: 'member', role: { name: 'viewer', source: 'override' } }]
  const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

  it('keeps /admin/limits and says Pool limits is for platform admins', async () => {
    globalThis.fetch = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      const reply = url.includes('/v1/tenants/mine') ? MINE : url.includes('/v1/tenants/me') ? ME : undefined
      return Promise.resolve(reply === undefined ? json({ detail: 'Not Found' }, 404) : json(reply))
    }) as unknown as typeof fetch
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { App } = await import('../App')
    window.history.replaceState(null, '', '/admin/limits')
    render(<App />)
    // MUTATION: redirect `landing === null` to '' again and the address becomes /overview.
    await screen.findByText('Pool limits is for platform admins', undefined, { timeout: 4000 })
    expect(window.location.pathname).toBe('/admin/limits')
    expect(document.querySelector('[data-refused="admin"]')!.textContent).toContain('You are a viewer of')
    expect(screen.getByRole('link', { name: 'Open Overview' }).getAttribute('href')).toBe('/overview')
  })
})
