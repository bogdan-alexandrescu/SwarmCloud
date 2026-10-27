/**
 * GITHUB ACCESS' MEMBERS AND BROWSER LOGINS MOVE TO THE PAGES WHOSE SUBJECT
 * THEY ARE (GA-MOVE-VIEWS; part of #2157, BGD 2026-10-24: "Members to Config ›
 * People and roles, Browser logins to Config › Secrets").
 *
 *   * Config › GitHub access is mock-up S2's two tabs and nothing else: no
 *     Mine / Members / Browser logins switch in its head.
 *   * Members is a tab of Config › People and roles at `?view=members`, drawn
 *     for whoever the API serves it to (a platform admin, `principal.is_admin`),
 *     with the same rows.
 *   * Browser logins is a tab of Config › Secrets at `?view=logins`, with the
 *     same list and its typed Revoke.
 *   * The old addresses -- `/config/github?view=members|logins`, `/access?view=...`
 *     and the hash router's `#work/access?view=...` -- land on the new places,
 *     written with replaceState, never pushed.
 *   * Access.tsx's own copy names Workspace setup as CONFIG-NAV's SECTIONS spell
 *     it and links its path, not a redirect.
 *
 * Fixtures only; no network. Nothing here is credential-shaped.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useEffect, useState } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>

const ME = (isAdmin: boolean): Json => ({
  tenant: { tenant_id: 'eng', kind: 'group', display_name: 'eng@example.com' },
  principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: isAdmin },
  environment: 'dev',
  environment_declared: true,
})

const ROLES: Json = {
  tenant_id: 'eng',
  kind: 'group',
  default_role: 'member',
  default_source: 'set',
  me: { email: 'ana.b@example.com', via: 'member', role: 'admin', source: 'override' },
  people: [],
  roles: [{ name: 'viewer', summary: 'Sees everything.' }],
  can_manage: true,
  audit: [],
}

const MEMBERS: Json = {
  tenant_id: 'eng',
  members: [
    {
      user: 'bo.c@example.com',
      connection: { state: 'active', forge_login: 'bo-c', failure: null, method: 'app_user', access_expires_at: null, refreshed_at: null },
      orgs: [{ owner: 'example-org' }],
      grants: [{ repo: 'example-org/api', mode: 'write' }],
    },
  ],
}

const SECRETS: Json = { tenant_id: 'u-ana', kind: 'user', can_manage: true, secrets: [] }
// Config › Secrets' one read since SSC-UI (Secrets.tsx): its tenant is the one the logins tab is in.
const OVERVIEW: Json = { tenant_id: 'u-ana', scope: 'workspace', repo_id: null, repository: null, personal_tenant_id: 'u-ana', can_manage: true, rows: [] }
const LOGIN = { name: 'github-qa', site: 'https://github.com', saved_at: '2026-10-20T10:00:00Z', expires_at: '2099-01-01T00:00:00Z', saved_by: 'ana.b@example.com' }

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  localStorage.clear()
  sessionStorage.clear()
  window.history.replaceState(null, '', '/')
})

async function routing() {
  vi.resetModules()
  const [App, paths] = await Promise.all([import('../App'), import('../paths')])
  const land = (path: string): string => {
    const [pathname, search = ''] = path.split('?')
    const p = paths.pathToAddress(pathname!, search === '' ? '' : `?${search}`)
    expect(p, `${path} is no address at all`).not.toBeNull()
    return paths.addressToPath(App.canonical(App.fromAddress(p!.address)))
  }
  return { App, paths, land }
}

describe('the old addresses land on the new places', () => {
  const MOVED: readonly [string, string][] = [
    ['/config/github?view=members', '/config/people?view=members'],
    ['/config/github?view=logins', '/config/secrets?view=logins'],
    ['/access?view=members', '/config/people?view=members'],
    ['/access?view=logins', '/config/secrets?view=logins'],
    // The two GitHub access tabs stay where they are.
    ['/config/github?view=choose', '/config/github?view=choose'],
    ['/config/github', '/config/github'],
    // The new addresses are their own spelling.
    ['/config/people?view=members', '/config/people?view=members'],
    ['/config/secrets?view=logins', '/config/secrets?view=logins'],
  ]
  it.each(MOVED)('%s -> %s', async (old, now) => {
    const { land } = await routing()
    // MUTATION: drop the members or logins row of MOVED_VIEWS in paths.ts -> its row fails.
    expect(land(old)).toBe(now)
  })

  it('the hash router\'s old addresses land there too', async () => {
    const { App, paths } = await routing()
    const at = (hash: string) => paths.addressToPath(App.canonical(App.fromAddress(hash)))
    expect(at('work/access?view=members')).toBe('/config/people?view=members')
    expect(at('work/access?view=logins')).toBe('/config/secrets?view=logins')
    expect(at('config/github?view=logins')).toBe('/config/secrets?view=logins')
  })

  it('a secrets link keeps its action over a view, and never a token', async () => {
    const { paths } = await routing()
    const t = Math.random().toString(36).slice(2)
    expect(paths.secretsLinkView(`view=logins&t=${t}`)).toBe('view=logins')
    expect(paths.secretsLinkView(`action=add&view=logins&t=${t}`)).toBe('action=add')
    expect(paths.secretsLinkView('view=other')).toBe('')
  })

  it('opening an old address rewrites the bar in place: replaced, never pushed', async () => {
    serve((_m, url) => {
      if (url.includes('/v1/tenants/me')) return { status: 200, body: ME(true) }
      if (url.includes('/v1/workspace/roles')) return { status: 200, body: ROLES }
      if (url.includes('/v1/access/members')) return { status: 200, body: MEMBERS }
      return null
    })
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const mod = await import('../App')
    window.history.replaceState(null, '', '/config/github?view=members')
    const push = vi.spyOn(window.history, 'pushState')
    render(<mod.App />)
    await waitFor(() => expect(window.location.pathname + window.location.search).toBe('/config/people?view=members'))
    // MUTATION: write the redirect with pushState -> fails.
    expect(push).not.toHaveBeenCalled()
    push.mockRestore()
  })
})

describe('Config › GitHub access is S2\'s two tabs and nothing else', () => {
  it('draws no Mine / Members / Browser logins switch', async () => {
    serve(() => null)
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { AccessScreen } = await import('../Access')
    render(<AccessScreen />)
    // MUTATION: put the UrRadio "Whose access" back in the head -> fails.
    expect(screen.queryByRole('radiogroup', { name: 'Whose access' })).toBeNull()
    expect(screen.queryByRole('radio', { name: 'Members' })).toBeNull()
    expect(screen.queryByRole('radio', { name: /browser logins/i })).toBeNull()
    expect(screen.getAllByRole('tab').map((t) => t.textContent)).toEqual(['Connection', 'Choose repositories'])
  })

  it('a stale ?view=members on the page itself opens Connection, not Members', async () => {
    serve(() => null)
    window.history.replaceState(null, '', '/config/github?view=members')
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { AccessScreen } = await import('../Access')
    render(<AccessScreen />)
    expect(document.querySelector('.ac-members')).toBeNull()
    expect(screen.getByRole('tab', { name: 'Connection' }).getAttribute('aria-selected')).toBe('true')
  })

  it('the recovery line names Workspace setup as SECTIONS spells it, linked at its own path', async () => {
    serve((_m, url) =>
      url.includes('/v1/access') && !url.includes('owners')
        ? { status: 200, body: { tenant_id: 'eng', connection: { state: 'revoked_by_user', forge_login: 'ana', failure: 'token refused', method: 'app_user', access_expires_at: null, refreshed_at: null }, orgs: [], grants: [] } }
        : null,
    )
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const [{ AccessScreen }, App] = await Promise.all([import('../Access'), import('../App')])
    render(<AccessScreen />)
    const config = App.SECTIONS.find((s) => s.id === 'config')!
    const ws = config.tabs.find((t) => t.id === 'workspace')!.label
    const link = await screen.findByRole('link', { name: `Config › ${ws} › Workspace setup` })
    // MUTATION: link `/setup` (the redirect) or leave the words unlinked -> fails.
    expect(link.getAttribute('href')).toBe('/config/workspace/setup')
    expect(visible(document.body)).not.toMatch(/Work › Setup/)
  })
})

describe('Members is a tab of Config › People and roles', () => {
  function api(isAdmin: boolean) {
    return serve((_m, url) => {
      if (url.includes('/v1/tenants/me')) return { status: 200, body: ME(isAdmin) }
      if (url.includes('/v1/workspace/roles')) return { status: 200, body: { ...ROLES, can_manage: isAdmin } }
      if (url.includes('/v1/access/members')) return isAdmin ? { status: 200, body: MEMBERS } : { status: 403, body: { detail: { code: 'forbidden', message: "the workspace's members view is for admins" } } }
      return null
    })
  }
  async function mount() {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { WorkspacePeopleScreen } = await import('../WorkspacePeople')
    return render(<WorkspacePeopleScreen />)
  }

  it('a platform admin finds the tab, and it draws every member\'s GitHub connection', async () => {
    api(true)
    await mount()
    const tab = await screen.findByRole('tab', { name: "Members' GitHub" })
    fireEvent.click(tab)
    // MUTATION: leave the tab out of WorkspacePeople.tsx -> the find above fails.
    expect(new URLSearchParams(window.location.search).get('view')).toBe('members')
    const list = await screen.findByRole('list', { name: 'Members of eng' })
    const row = within(list).getByText('bo.c@example.com').closest('li')!
    expect(visible(row)).toContain('@bo-c')
    expect(visible(row)).toContain('active')
    expect(visible(row)).toContain('example-org')
    expect(visible(row)).toContain('1 · 1 write')
    fireEvent.click(screen.getByRole('tab', { name: 'Roles' }))
    expect(window.location.search).toBe('')
    expect(screen.queryByRole('list', { name: 'Members of eng' })).toBeNull()
  })

  it('?view=members opens the tab on arrival', async () => {
    api(true)
    window.history.replaceState(null, '', '/config/people?view=members')
    await mount()
    expect(await screen.findByRole('list', { name: 'Members of eng' })).toBeTruthy()
    expect(screen.getByRole('tab', { name: "Members' GitHub" }).getAttribute('aria-selected')).toBe('true')
  })

  it('a member who is no platform admin is offered no tab and never asks for members', async () => {
    const calls = api(false)
    await mount()
    await screen.findByText(/Everyone else in eng holds/)
    await waitFor(() => expect(calls.some((c) => c.url.includes('/v1/tenants/me'))).toBe(true))
    // MUTATION: draw the tab whatever `is_admin` says -> fails.
    expect(screen.queryByRole('tab', { name: "Members' GitHub" })).toBeNull()
    expect(calls.some((c) => c.url.includes('/v1/access/members'))).toBe(false)
  })

  it('a non-admin who follows an old Members link sees the admin gate, as on GitHub access before', async () => {
    api(false)
    window.history.replaceState(null, '', '/config/people?view=members')
    await mount()
    await waitFor(() => expect(document.querySelector('.ac-members')).not.toBeNull())
    await waitFor(() => expect(visible(document.querySelector('.ac-members'))).toMatch(/admin/i))
    expect(screen.queryByRole('button', { name: /try again/i })).toBeNull()
  })
})

describe('Browser logins is a tab of Config › Secrets', () => {
  function api() {
    return serve((m, url) => {
      if (m === 'GET' && url.startsWith('/v1/secrets/overview')) return { status: 200, body: OVERVIEW }
      if (m === 'GET' && url.startsWith('/v1/repositories')) return { status: 200, body: { repositories: [] } }
      if (m === 'GET' && url.includes('/v1/workspace/secrets/consumers')) return { status: 200, body: { kinds: [], providers: [], consumers: [] } }
      if (m === 'GET' && url === '/v1/browser-logins') return { status: 200, body: { tenant_id: 'u-ana', logins: [LOGIN] } }
      if (m === 'DELETE' && url === '/v1/browser-logins/github-qa') return { status: 200, body: { name: 'github-qa', revoked: true } }
      return null
    })
  }
  /**
   * The page as App routes it: its view is the bar's query, and a tab moves
   * the bar (Secrets.tsx `goView`), which App reads back on popstate.
   */
  async function mount() {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { SecretsScreen } = await import('../Secrets')
    function Routed() {
      const [view, setView] = useState(window.location.search.slice(1))
      useEffect(() => {
        const on = () => setView(window.location.search.slice(1))
        window.addEventListener('popstate', on)
        return () => window.removeEventListener('popstate', on)
      }, [])
      return <SecretsScreen view={view} />
    }
    return render(<Routed />)
  }

  it('the tab draws the person\'s saved logins, and its address is ?view=logins', async () => {
    api()
    await mount()
    // MUTATION: leave the tab out of Secrets.tsx -> fails.
    fireEvent.click(screen.getByRole('tab', { name: 'Browser logins' }))
    expect(new URLSearchParams(window.location.search).get('view')).toBe('logins')
    const list = await screen.findByRole('list', { name: /browser logins/i })
    expect(visible(list)).toContain('github-qa')
    fireEvent.click(screen.getByRole('tab', { name: 'Secrets' }))
    expect(window.location.search).toBe('')
  })

  it('?view=logins opens the tab on arrival, and Revoke still works from it', async () => {
    const calls = api()
    window.history.replaceState(null, '', '/config/secrets?view=logins')
    await mount()
    const list = await screen.findByRole('list', { name: /browser logins/i })
    fireEvent.click(within(list).getByRole('button', { name: /^revoke github-qa$/i }))
    const ask = screen.getByRole('group', { name: /revoke github-qa/i })
    fireEvent.change(within(ask).getByLabelText(/type github-qa to revoke/i), { target: { value: 'github-qa' } })
    await act(async () => {
      fireEvent.click(within(ask).getByRole('button', { name: /^revoke$/i }))
    })
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE' && c.url === '/v1/browser-logins/github-qa')).toBe(true))
  })

  it('in a group workspace the tab says only where logins live, and the head offers no Refresh of the secrets (seen on the render)', async () => {
    serve((m, url) => {
      if (m === 'GET' && url.startsWith('/v1/secrets/overview')) return { status: 200, body: { ...OVERVIEW, tenant_id: 'eng' } }
      if (m === 'GET' && url.startsWith('/v1/repositories')) return { status: 200, body: { repositories: [] } }
      if (m === 'GET' && url.includes('/v1/workspace/secrets/consumers')) return { status: 200, body: { kinds: [], providers: [], consumers: [] } }
      return null
    })
    window.history.replaceState(null, '', '/config/secrets?view=logins')
    await mount()
    await screen.findByText('Not kept in a group tenant')
    // MUTATION: draw the "browser sessions you saved" lede whatever the tenant -> fails.
    expect(visible(document.body)).not.toMatch(/The browser sessions you saved/)
    // MUTATION: keep the head's UrRefresh or Add a secret on the Browser logins tab -> fails.
    expect(document.querySelector('.c-phead .c-refresh')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Add a secret' })).toBeNull()
    fireEvent.click(screen.getByRole('tab', { name: 'Secrets' }))
    await waitFor(() => expect(document.querySelector('.c-phead .c-refresh')).not.toBeNull())
  })

  it('a one-time add link opens the secrets, whatever the view', async () => {
    api()
    window.history.replaceState(null, '', '/config/secrets?action=add&view=logins')
    await mount()
    // MUTATION: keep `view=logins` beside a link's action in paths.ts `secretsLinkView` -> fails.
    expect(screen.getByRole('tab', { name: 'Secrets' }).getAttribute('aria-selected')).toBe('true')
    expect((await screen.findAllByText(/one-time form/i)).length).toBeGreaterThan(0)
  })
})

describe("People and roles' role list and overrides read as the house list and table (seen on the render, GA-MOVE-VIEWS review)", () => {
  // The rendered review at 1440 found both drawn by the browser's defaults:
  // the definition list indented 40px at 16px, the table's cells centred and
  // unpadded. They take Profile's label-value grid (`.pf-facts`) and the
  // Secrets / MCP servers table (`.mcp-servers`, which stacks on a phone).
  const WIDE = { width: 1440 }
  const PHONE = { width: 390 }
  it('draws the role list as a label-value grid and the overrides as a table that stacks at 390', async () => {
    const { painted } = await import('./marks')
    serve((_m, url) => (url.includes('/v1/workspace/roles') ? { status: 200, body: { ...ROLES, people: [{ email: 'bo.c@example.com', role: 'admin', source: 'override', granted_by: 'ops@example.com', granted_at: '2026-10-18T09:14:03Z', granted_via: 'console', reason: 'lead' }] } } : null))
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { WorkspacePeopleScreen } = await import('../WorkspacePeople')
    render(<WorkspacePeopleScreen />)
    const table = await screen.findByRole('table', { name: /^Who holds which role in / })
    const facts = screen.getByText('Sees everything.').closest('dl')!
    // MUTATION: drop `className="pf-facts"` from RoleTable's <dl> -> fails.
    expect(painted(facts, 'display', WIDE)).toBe('grid')
    // MUTATION: drop `className="mcp-servers"` from Overrides' <table> -> fails.
    const cell = table.querySelector('tbody td')!
    expect(painted(cell, 'text-align', WIDE)).toBe('left')
    expect(painted(table.querySelector('tbody tr')!, 'display', PHONE)).toBe('block')
    // Every cell names its column, which a phone draws above the value.
    for (const c of table.querySelectorAll('tbody th, tbody td')) expect(c.getAttribute('data-label'), c.textContent ?? '').not.toBeNull()
  })
})

describe('an old bookmark lands on the new page with its tab drawn (GA-MOVE-VIEWS review)', () => {
  // The review found the bar right and the tab wrong: each page read its tab
  // off window.location once, at mount, before the router had written the new
  // address, so a hash bookmark drew Workspace secrets under ?view=logins and
  // Roles under ?view=members. The tab is the routed address App carries.
  function api() {
    return serve((m, url) => {
      if (url.includes('/v1/tenants/me')) return { status: 200, body: ME(true) }
      if (url.includes('/v1/workspace/roles')) return { status: 200, body: ROLES }
      if (url.includes('/v1/access/members')) return { status: 200, body: MEMBERS }
      if (m === 'GET' && url.startsWith('/v1/secrets/overview')) return { status: 200, body: OVERVIEW }
      if (m === 'GET' && url.includes('/v1/workspace/secrets/consumers')) return { status: 200, body: { kinds: [], providers: [], consumers: [] } }
      if (m === 'GET' && url.includes('/v1/workspace/secrets')) return { status: 200, body: SECRETS }
      if (m === 'GET' && url === '/v1/browser-logins') return { status: 200, body: { tenant_id: 'u-ana', logins: [LOGIN] } }
      return null
    })
  }
  const LANDS: readonly [string, string, string][] = [
    ['/#work/access?view=logins', '/config/secrets?view=logins', 'Browser logins'],
    ['/#config/github?view=logins', '/config/secrets?view=logins', 'Browser logins'],
    ['/#config/github?view=members', '/config/people?view=members', "Members' GitHub"],
    ['/#work/access?view=members', '/config/people?view=members', "Members' GitHub"],
    ['/#config/people?view=members', '/config/people?view=members', "Members' GitHub"],
    ['/config/github?view=logins', '/config/secrets?view=logins', 'Browser logins'],
    ['/config/github?view=members', '/config/people?view=members', "Members' GitHub"],
  ]
  it.each(LANDS)('%s opens %s on %s', async (old, now, tab) => {
    api()
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const mod = await import('../App')
    window.history.replaceState(null, '', old)
    render(<mod.App />)
    await waitFor(() => expect(window.location.pathname + window.location.search).toBe(now))
    // MUTATION: read the tab off window.location at mount again (peopleViewFromUrl / secretsViewFromUrl) -> the hash rows fail.
    await waitFor(() => expect(screen.getByRole('tab', { name: tab }).getAttribute('aria-selected')).toBe('true'))
  })
})
