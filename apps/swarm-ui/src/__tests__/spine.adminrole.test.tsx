/**
 * ADMIN IS SHOWN ONLY TO SOMEONE WHO ADMINISTERS SOMETHING (NAV-ADMIN-ROLE;
 * part of #1633, owner decision 2026-10-19 "All under Profile; Admin by
 * role").
 *
 *   * A platform admin (`/v1/tenants/me` `is_admin`) sees Admin with its six
 *     pages, Workspaces you admin last.
 *   * A person whose stored role is `admin` in a GROUP workspace
 *     (`/v1/tenants/mine`) sees Admin with only Workspaces you admin, which
 *     lists those workspaces; each link selects the workspace, then opens
 *     the page.
 *   * Anyone else -- a member, or the admin of only their own personal
 *     workspace -- sees no Admin item, flyout or drawer entry, and nothing is
 *     drawn while the reads are in flight. A direct link to an Admin page
 *     still lands on that page's own refusal.
 *
 * This replaces spine.adminlock.test.tsx, which held the reversed rule
 * ("Admin is SHOWN to a non-admin, with a lock").
 *
 * MUTATION: draw `SPINE` unfiltered and the member, personal-admin and
 * in-flight cases go red; drop `kind === 'group'` from `adminWorkspaces` and
 * the personal-admin case does; return all of PANEL_PAGES.admin from
 * `adminPagesFor` for a workspace admin and the eng case does; drop
 * `chooseTenant` from the page's links and its People and roles case does.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, waitFor } from '@testing-library/react'

type Json = Record<string, unknown>

const ME = (isAdmin: boolean): Json => ({
  tenant: { tenant_id: 'u-ana', display_name: 'Your workspace' },
  principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: isAdmin },
  environment: 'dev',
  environment_declared: true,
})

const PERSONAL: Json = { tenant_id: 'u-ana', display_name: 'ana.b@example.com', kind: 'user', via: 'personal', role: { name: 'admin', source: 'personal' } }
const group = (id: string, role: string, extra: Json = {}): Json => ({
  tenant_id: id,
  display_name: `${id}@example.com`,
  kind: 'group',
  via: 'member',
  role: { name: role, source: 'override' },
  ...extra,
})

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })

/**
 * The two frame reads, answered as given. `null` holds a read open until the
 * returned function answers it; every other read fails.
 */
function serve(me: Json | null, mine: Json[] | null): { answer: () => void } {
  const held: (() => void)[] = []
  globalThis.fetch = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    const reply = url.includes('/v1/tenants/mine') ? mine : url.includes('/v1/tenants/me') ? me : undefined
    if (reply === undefined) return Promise.resolve(new Response('{}', { status: 503, headers: { 'content-type': 'application/json' } }))
    if (reply !== null) return Promise.resolve(json(reply))
    return new Promise<Response>((resolve) => {
      held.push(() => resolve(json(url.includes('/v1/tenants/mine') ? [PERSONAL] : ME(false))))
    })
  }) as unknown as typeof fetch
  return { answer: () => held.splice(0).forEach((f) => f()) }
}

async function live() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../Spine')
}

async function shell(section: 'overview' | 'admin', tab: string) {
  const { SkyShell } = await live()
  render(
    <SkyShell section={section} tab={tab} title="x" go={vi.fn()} foot={null}>
      {null}
    </SkyShell>,
  )
}

const adminItem = () => document.querySelector<HTMLAnchorElement>('.sk-spine a[data-sec="admin"]')
/** The Admin pages in the panel, without the "In Automate" links under them (S1b, CONFIG-PANEL-E). */
const panelRows = () => [...document.querySelectorAll('.sk-panel .sk-pk:not(.sk-pk-out) .sk-pl')].map((e) => e.textContent)
/** The session is read once the panel foot names the person. */
const signedIn = () => expect(document.querySelector('.sk-pfoot small')?.textContent).toBe('ana.b@example.com')

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  localStorage.clear()
  sessionStorage.clear()
  window.history.replaceState(null, '', '/')
})

describe('who sees Admin (NAV-ADMIN-ROLE)', () => {
  it('draws no Admin item, flyout or drawer entry for a member with no admin role', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'member'), group('ops', 'viewer')])
    localStorage.setItem('swarm.shell.collapsed', '1')
    await shell('overview', 'now')
    await waitFor(() => expect(document.querySelector('.sk-tenant, .sk-ttile, .sk-spine')).not.toBeNull())
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30))
    })
    expect(adminItem(), 'the collapsed spine drew Admin').toBeNull()
    // Every other section still opens its flyout; there is none for Admin.
    fireEvent.focus(document.querySelector<HTMLElement>('.sk-spine a[data-sec="capacity"]')!)
    expect(document.querySelector('.sk-flyout.is-capacity')).not.toBeNull()
    expect(document.querySelector('.sk-flyout.is-admin')).toBeNull()
    cleanup()

    // The phone drawer draws the same spine.
    vi.stubGlobal('matchMedia', (query: string) => ({
      matches: query.includes('max-width: 759px'),
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
    }))
    localStorage.clear()
    await shell('overview', 'now')
    fireEvent.click(document.querySelector<HTMLElement>('.sk-pbar button[aria-label="Open the menu"]')!)
    await waitFor(() => expect(document.querySelector('.sk-app.has-drawer')).not.toBeNull())
    await waitFor(signedIn)
    expect(document.querySelector('.sk-app.has-drawer .sk-spine a[data-sec="capacity"]'), 'the drawer drew no sections').not.toBeNull()
    expect(document.querySelector('.sk-app.has-drawer a[data-sec="admin"]'), 'the drawer drew Admin').toBeNull()
  })

  it('draws no Admin for someone who administers only their personal workspace', async () => {
    serve(ME(false), [PERSONAL])
    await shell('overview', 'now')
    await waitFor(signedIn)
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30))
    })
    expect(adminItem()).toBeNull()
  })

  it('draws no Admin while the reads are in flight, rather than a locked one', async () => {
    const { answer } = serve(null, null)
    await shell('admin', 'limits')
    expect(adminItem(), 'Admin was drawn before the reads landed').toBeNull()
    expect(panelRows()).toEqual([])
    expect(document.querySelector('.sk-panel [aria-disabled="true"]')).toBeNull()
    await act(async () => answer())
    await waitFor(signedIn)
    expect(adminItem()).toBeNull()
  })

  it('shows a workspace admin of eng only Workspaces you admin, and Admin opens it', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'admin'), group('ops', 'member')])
    await shell('admin', 'workspaces')
    await waitFor(() => expect(adminItem()).not.toBeNull())
    expect(adminItem()!.getAttribute('href')).toBe('/admin/workspaces')
    expect(adminItem()!.querySelector('.sk-lkd, .sk-ic.sk-lk')).toBeNull()
    expect(panelRows()).toEqual(['Workspaces you admin'])
  })

  it('shows a platform admin the seven Admin pages, their workspaces\' group first and Runner sizes beside Pool limits (SIZES-UI)', async () => {
    serve(ME(true), [PERSONAL, group('eng', 'admin', { role: { name: 'admin', source: 'platform_admin' } })])
    await shell('admin', 'limits')
    await waitFor(() => expect(adminItem()).not.toBeNull())
    expect(adminItem()!.getAttribute('href')).toBe('/admin/limits')
    // Their workspaces' group first, then the platform's (S1b, CONFIG-PANEL-E).
    // Platform secrets (SSC-UI, part of #2158) last of the platform's, as proposal E S1b draws it.
    expect(panelRows()).toEqual(['People', 'Workspaces you admin', 'Pool limits', 'Runner sizes', 'Tenants', 'Platform counts', 'Platform secrets'])
  })
})

describe('Admin › Workspaces you admin (NAV-ADMIN-ROLE)', () => {
  async function app(path: string) {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { App } = await import('../App')
    window.history.replaceState(null, '', path)
    render(<App />)
  }
  const rows = () => [...document.querySelectorAll<HTMLElement>('.wya-row')]

  it('lists eng, and its People and roles link selects eng and lands on Config › People and roles', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'admin'), group('ops', 'member')])
    await app('/admin/workspaces')
    await waitFor(() => expect(rows()).toHaveLength(1))
    const eng = rows()[0]!
    expect(eng.getAttribute('data-tenant')).toBe('eng')
    expect(eng.querySelector('.wya-name b')?.textContent).toBe('eng@example.com')
    const links = [...eng.querySelectorAll<HTMLAnchorElement>('.wya-links a')]
    expect(links.map((a) => [a.textContent, a.getAttribute('href')])).toEqual([
      // The pages' own addresses are still `settings/*`; they land on Config since CONFIG-NAV.
      ['Workspace', '/config/workspace'],
      ['MCP servers', '/config/mcp-servers'],
      ['People and roles', '/config/people'],
      // Settings › API keys is Config › Secrets since SSC-UI (part of #2158).
      ['Workspace key', '/config/secrets'],
    ])
    const { chosenTenant } = await import('../fetch')
    expect(chosenTenant()).toBeNull()
    fireEvent.click(links.find((a) => a.textContent === 'People and roles')!)
    expect(chosenTenant(), 'the link did not select eng').toBe('eng')
    await waitFor(() => expect(window.location.pathname).toBe('/config/people'))
  })

  it('lists for a platform admin the workspaces where their stored role is admin, not every one they reach', async () => {
    serve(ME(true), [
      PERSONAL,
      group('eng', 'admin', { role: { name: 'admin', source: 'platform_admin' } }),
      group('far', 'admin', { via: 'platform_admin', role: { name: 'admin', source: 'platform_admin' } }),
    ])
    await app('/admin/workspaces')
    await waitFor(() => expect(rows()).toHaveLength(1))
    expect(rows()[0]!.getAttribute('data-tenant')).toBe('eng')
  })

  it('says plainly that you administer no group workspace', async () => {
    serve(ME(true), [PERSONAL, group('eng', 'member')])
    await app('/admin/workspaces')
    await waitFor(() => expect(document.querySelector('.wya-page .c-emp h3')?.textContent).toContain('You administer no group workspace'))
    expect(rows()).toHaveLength(0)
  })

  it('sends a member on a direct /admin/limits to Overview (CONFIG-NAV)', async () => {
    // The fixture build: its caller is a member who administers nothing. The
    // page's own refusal and the API's 403 admin_required are unchanged; since
    // CONFIG-NAV (part of #2157, BGD 2026-10-23 "Admin is shown only to
    // admins") the console sends them to Overview before either is met.
    vi.resetModules()
    const { App } = await import('../App')
    window.history.replaceState(null, '', '/admin/limits')
    render(<App />)
    await waitFor(() => expect(window.location.pathname).toBe('/overview'))
    expect(adminItem(), 'the member was drawn an Admin item').toBeNull()
  })
})
