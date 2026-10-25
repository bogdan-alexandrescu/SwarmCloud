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
    serve((m, url) => {
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
    serve((m, url) =>
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
    return serve((m, url) => {
      if (url.includes('/v1/tenants/me')) return { status: 200, body: ME(isAdmin) }
      if (url.includes('/v1/workspace/roles')) return { status: 200, body: { ...ROLES, can_manage: isAdmin } }
      if (url.includes('/v1/access/members')) return isAdmin ? { status: 200, body: MEMBERS } : { status: 403, body: { detail: { code: 'forbidden', message: "the tenant's members view is for admins" } } }
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
      if (m === 'GET' && url.includes('/v1/workspace/secrets/consumers')) return { status: 200, body: { kinds: [], providers: [], consumers: [] } }
      if (m === 'GET' && url.includes('/v1/workspace/secrets')) return { status: 200, body: SECRETS }
      if (m === 'GET' && url === '/v1/browser-logins') return { status: 200, body: { tenant_id: 'u-ana', logins: [LOGIN] } }
      if (m === 'DELETE' && url === '/v1/browser-logins/github-qa') return { status: 200, body: { name: 'github-qa', revoked: true } }
      return null
    })
  }
  async function mount() {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { WorkspaceSecretsScreen } = await import('../WorkspaceSecrets')
    return render(<WorkspaceSecretsScreen link={null} />)
  }

  it('the tab draws the person\'s saved logins, and its address is ?view=logins', async () => {
    api()
    await mount()
    // MUTATION: leave the tab out of WorkspaceSecrets.tsx -> fails.
    fireEvent.click(screen.getByRole('tab', { name: 'Browser logins' }))
    expect(new URLSearchParams(window.location.search).get('view')).toBe('logins')
    const list = await screen.findByRole('list', { name: /browser logins/i })
    expect(visible(list)).toContain('github-qa')
    fireEvent.click(screen.getByRole('tab', { name: 'Workspace secrets' }))
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

  it('a one-time add link opens the secrets, whatever the view', async () => {
    api()
    window.history.replaceState(null, '', '/config/secrets?view=logins')
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { WorkspaceSecretsScreen } = await import('../WorkspaceSecrets')
    render(<WorkspaceSecretsScreen link={{ action: 'add', name: null, token: null }} />)
    expect(screen.getByRole('tab', { name: 'Workspace secrets' }).getAttribute('aria-selected')).toBe('true')
    expect(screen.getByText('Opened from a one-time link')).toBeTruthy()
  })
})
