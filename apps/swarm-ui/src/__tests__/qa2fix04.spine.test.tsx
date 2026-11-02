/**
 * VISUAL QA PASS 2, THE SPINE (lane QA2-FIX-04; part of #2694 and #2391).
 *
 *   admin-shell-12  A 404 in no section drew an empty panel under the
 *                   switcher. It now lists the rail's sections, headed `Go to`.
 *   admin-shell-21  The member's personal workspace (an empty `/mine`) was
 *                   named by its document (`Bogdan`), the platform admin's
 *                   `Your workspace`: one workspace, two names.
 *   admin2-01       A workspace admin on /admin/limits was swapped to
 *                   Workspaces you admin with no word; refused in place now.
 *   admin2-06       The phone sheet's Escape gave focus to <body> when the
 *                   chip had been tapped (a tap focuses nothing).
 *   admin2-07       The rail's `1 waiting for an admin` sat on an item that
 *                   opened Pool limits; it opens People and roles now.
 *   admin2-11       Tab past the Act-as popover's last option went into the
 *                   page, skipping the panel's `copy id` and section links.
 *   admin2-15       A pool at 5 of 5 was `1 near ceiling` on the badge and
 *                   `another pool is full` under the meter.
 *   admin2-18       `Showing eng, the workspaces you administer.` for one.
 *
 * Each `it` names the mutation that turns it red.
 */
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

type Json = Record<string, unknown>

const WAIT = { timeout: 5000 } as const

const ME = (isAdmin: boolean, tenant: Json = { tenant_id: 'u-ana', kind: 'user', display_name: 'Ana' }): Json => ({
  tenant,
  principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: isAdmin },
  environment: 'dev',
  environment_declared: true,
})
const PERSONAL: Json = { tenant_id: 'u-ana', display_name: 'ana.b@example.com', kind: 'user', via: 'personal', role: { name: 'admin', source: 'personal' } }
const group = (id: string, role: string): Json => ({ tenant_id: id, display_name: `${id}@example.com`, kind: 'group', via: 'member', role: { name: role, source: 'override' } })
const FEED = (extra: Json = {}): Json => ({ items: [], unread_needs_you: 0, unread_updates: 0, admin_pending: 0, read_at: null, muted: { needs_you: 0, updates: 0 }, partial: false, ...extra })
const pool = (name: string, active: number, limit: number | null): Json => ({
  name,
  hard_limit: limit,
  adaptive_target: null,
  quota_derived_limit: null,
  effective_limit: limit,
  active,
  available: limit === null ? null : limit - active,
  enabled: true,
})

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

/** The frame's reads, answered as given; the feed and capacity only when given. Everything else is a 404. */
function serve(me: Json, mine: Json[], opts: { feed?: Json; capacity?: Json } = {}) {
  globalThis.fetch = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/v1/notifications') && opts.feed !== undefined) return Promise.resolve(json(opts.feed))
    if (url.includes('/v1/capacity') && opts.capacity !== undefined) return Promise.resolve(json(opts.capacity))
    if (url.includes('/v1/tenants/mine')) return Promise.resolve(json(mine))
    if (url.includes('/v1/tenants/me')) return Promise.resolve(json(me))
    return Promise.resolve(json({ detail: 'Not Found' }, 404))
  }) as unknown as typeof fetch
}

async function app(path: string) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const mod = await import('../App')
  window.history.replaceState(null, '', path)
  render(<mod.App />)
  return mod
}

async function shell(props: { section: 'overview' | 'admin'; tab: string; missing?: boolean }) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SkyShell } = await import('../Spine')
  render(
    <SkyShell section={props.section} tab={props.tab} title="x" go={vi.fn()} foot={null} missing={props.missing}>
      <p>page</p>
    </SkyShell>,
  )
}

const adminItem = () => document.querySelector<HTMLAnchorElement>('.sk-spine a[data-sec="admin"]')

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  localStorage.clear()
  sessionStorage.clear()
  window.history.replaceState(null, '', '/')
})

describe('admin-shell-12: a 404 in no section', () => {
  it('lists the rail\'s sections in the panel, headed Go to', async () => {
    serve(ME(false), [PERSONAL])
    await shell({ section: 'overview', tab: 'now', missing: true })
    await waitFor(() => expect(document.querySelector('.sk-panel .sk-tenant')).not.toBeNull(), WAIT)
    // MUTATION: return null for an Overview 404 again -> no heading, no links.
    expect(document.querySelector('.sk-panel .sk-pscroll .sk-pt')?.textContent).toBe('Go to')
    expect(document.querySelector('.sk-panel .sk-pscroll .sk-pnote')?.textContent).toBe('This page is not in any section.')
    const links = [...document.querySelectorAll<HTMLAnchorElement>('.sk-panel .sk-pscroll a.sk-pk')]
    expect(links.map((a) => a.textContent)).toEqual(['Overview', 'Work', 'Automate', 'Capacity', 'Config'])
    // The rail's own links, the same addresses.
    const rail = [...document.querySelectorAll<HTMLAnchorElement>('.sk-spine a[data-sec]')].filter((a) => a.dataset.sec !== 'profile')
    expect(links.map((a) => a.getAttribute('href'))).toEqual(rail.map((a) => a.getAttribute('href')))
    // A 404 is on none of them.
    expect(links.some((a) => a.getAttribute('aria-current') !== null)).toBe(false)
  })
})

describe('admin-shell-21: one personal workspace, one name', () => {
  it('names the caller\'s own personal workspace Your workspace when /mine lists nothing', async () => {
    const { workspaceName } = await import('../Spine')
    // MUTATION: name a `kind: user` tenant by its display_name again -> 'Ana'.
    expect(workspaceName({ tenant_id: 'u-ana', kind: 'user', display_name: 'Ana' }, [])).toBe('Your workspace')
    expect(workspaceName({ tenant_id: 'eng', kind: 'group', display_name: 'Engineering' }, [])).toBe('Engineering')
  })

  it('draws the member\'s switcher as Your workspace, personal', async () => {
    serve(ME(false), [])
    await shell({ section: 'overview', tab: 'now' })
    await waitFor(() => expect(document.querySelector('.sk-panel .sk-tenant b')?.textContent).toBe('Your workspace'), WAIT)
    // MUTATION: say `workspace` for every kind in the one-workspace block -> fails.
    expect(document.querySelector('.sk-panel .sk-tenant small')?.textContent).toMatch(/^personal /)
  })
})

describe('admin2-01: a workspace admin on a platform page is told so in place', () => {
  it('refuses /admin/limits in place, keeps the address, and links Workspaces you admin', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'admin')])
    await app('/admin/limits')
    // MUTATION: redirect a workspace admin to Workspaces you admin again -> no refusal, and the address changes.
    await waitFor(() => expect(document.querySelector('[data-refused="admin"]')?.textContent).toContain('Pool limits is for platform admins'), WAIT)
    expect(window.location.pathname).toBe('/admin/limits')
    expect(document.querySelector('[data-refused="admin"]')?.textContent).toContain('You administer eng.')
    const open = [...document.querySelectorAll<HTMLAnchorElement>('[data-refused="admin"] a')].find((a) => a.textContent === 'Open Workspaces you admin')
    expect(open?.getAttribute('href')).toBe('/admin/workspaces')
    fireEvent.click(open!)
    await waitFor(() => expect(window.location.pathname).toBe('/admin/workspaces'), WAIT)
    expect(document.querySelector('[data-refused="admin"]')).toBeNull()
  })

  it('still lands a bare /admin on Workspaces you admin', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'admin')])
    await app('/admin')
    // MUTATION: refuse the bare address too -> it stays on the refusal.
    await waitFor(() => expect(window.location.pathname).toBe('/admin/workspaces'), WAIT)
    expect(document.querySelector('[data-refused="admin"]')).toBeNull()
  })
})

describe('admin2-07: the Admin item opens where its badge\'s requests are', () => {
  it('links People and roles while a request waits, Pool limits otherwise', async () => {
    serve(ME(true), [PERSONAL, group('eng', 'admin')], { feed: FEED({ admin_pending: 1 }) })
    await app('/overview')
    await waitFor(() => expect(adminItem()?.querySelector('.sk-apill')?.textContent).toBe('1'), WAIT)
    // MUTATION: keep the Admin item on `landing.to` -> /admin/limits.
    expect(adminItem()!.getAttribute('href')).toBe('/admin/people')
    cleanup()
    serve(ME(true), [PERSONAL, group('eng', 'admin')], { feed: FEED({ admin_pending: 0 }) })
    await app('/overview')
    await waitFor(() => expect(adminItem()).not.toBeNull(), WAIT)
    expect(adminItem()!.getAttribute('href')).toBe('/admin/limits')
  })
})

describe('admin2-06 and admin2-11: the switcher hands focus back', () => {
  it('returns focus to the phone chip on Escape, though the tap that opened the sheet focused nothing', async () => {
    serve(ME(false, { tenant_id: 'eng', kind: 'group', display_name: 'Engineering' }), [group('eng', 'member'), group('ops', 'member')])
    await shell({ section: 'overview', tab: 'now' })
    const chip = await waitFor(() => {
      const c = document.querySelector<HTMLButtonElement>('.sk-pbar .sk-tchip')
      expect(c).not.toBeNull()
      return c!
    }, WAIT)
    ;(document.activeElement as HTMLElement | null)?.blur()
    fireEvent.click(chip)
    await waitFor(() => expect(document.querySelector('.sk-tpop.is-sheet')).not.toBeNull())
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(document.querySelector('.sk-tpop')).toBeNull())
    // MUTATION: give focus back only to what held it at open (<body>) -> fails.
    expect(document.activeElement).toBe(chip)
  })

  it('Tab from the popover\'s last option goes on to copy id, Shift+Tab from the first back to the switcher', async () => {
    serve(ME(false, { tenant_id: 'eng', kind: 'group', display_name: 'Engineering' }), [group('eng', 'member'), group('ops', 'member')])
    await shell({ section: 'overview', tab: 'now' })
    const sw = await waitFor(() => {
      const b = document.querySelector<HTMLButtonElement>('.sk-panel .sk-tsw')
      expect(b).not.toBeNull()
      return b!
    }, WAIT)
    sw.focus()
    fireEvent.click(sw)
    const options = await waitFor(() => {
      const o = [...document.querySelectorAll<HTMLButtonElement>('.sk-tpop.is-panel .sk-to')]
      expect(o.length).toBe(2)
      return o
    })
    options[1]!.focus()
    fireEvent.keyDown(options[1]!, { key: 'Tab' })
    await waitFor(() => expect(document.querySelector('.sk-tpop')).toBeNull())
    // MUTATION: drop the Tab branch from TenantPicker's onKeyDown -> focus stays where jsdom left it, not copy id.
    expect(document.activeElement?.className).toBe('sk-cp')
    sw.focus()
    fireEvent.click(sw)
    const again = await waitFor(() => {
      const o = [...document.querySelectorAll<HTMLButtonElement>('.sk-tpop.is-panel .sk-to')]
      expect(o.length).toBe(2)
      return o
    })
    again[0]!.focus()
    fireEvent.keyDown(again[0]!, { key: 'Tab', shiftKey: true })
    await waitFor(() => expect(document.querySelector('.sk-tpop')).toBeNull())
    expect(document.activeElement).toBe(sw)
  })
})

describe('admin2-15: a full pool is called full', () => {
  it('badges a pool at its ceiling `1 full`, and one under it `1 near ceiling`', async () => {
    const { adminPanelCounts } = await import('../Spine')
    const cap = (pools: Json[]) => ({ status: 'ok' as const, fetchedAt: 0, data: { pools, read_at: '2026-10-29T10:00:00Z' } as never })
    const mine = { status: 'ok' as const, fetchedAt: 0, data: [] }
    // MUTATION: say `near ceiling` for a full pool again -> '1 near ceiling'.
    expect(adminPanelCounts(true, null, cap([pool('global', 2, 20), pool('provider:anthropic:tenant:u-bogdan', 5, 5)]), mine).limits?.text).toBe('1 full')
    expect(adminPanelCounts(true, null, cap([pool('global', 2, 20), pool('tenant:eng', 4, 5)]), mine).limits?.text).toBe('1 near ceiling')
    const both = adminPanelCounts(true, null, cap([pool('a', 5, 5), pool('tenant:eng', 4, 5)]), mine).limits
    expect(both?.text).toBe('1 full')
    expect(both?.why).toBe('1 pool is full; 1 pool is at 80% of the ceiling or more')
  })

  it('says `a pool is full`, not `another pool`, under the global meter', async () => {
    const { meterOf } = await import('../Spine')
    const m = meterOf({ pools: [pool('global', 2, 20), pool('provider:anthropic:tenant:u-bogdan', 5, 5)] } as never)
    // MUTATION: put `another pool is` back -> fails.
    expect(m?.warn).toBe('a pool is full: anthropic · u-bogdan')
  })
})

describe('admin2-18: one workspace is one', () => {
  it('says the workspace you administer for one, the workspaces for two', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'admin')])
    await shell({ section: 'admin', tab: 'workspaces' })
    // MUTATION: keep the plural for one -> 'the workspaces you administer'.
    await waitFor(() => expect(document.querySelector('.sk-panel .sk-pnote')?.textContent).toBe('Showing eng, the workspace you administer. The platform’s own pages are shown to platform admins only.'), WAIT)
    cleanup()
    serve(ME(false), [PERSONAL, group('eng', 'admin'), group('ops', 'admin')])
    await shell({ section: 'admin', tab: 'workspaces' })
    await waitFor(() => expect(document.querySelector('.sk-panel .sk-pnote')?.textContent).toBe('Showing eng and ops, the workspaces you administer. The platform’s own pages are shown to platform admins only.'), WAIT)
  })
})
