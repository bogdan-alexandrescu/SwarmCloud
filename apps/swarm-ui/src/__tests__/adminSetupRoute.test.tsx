// ADMIN › SETUP AND HEALTH, THE ROUTES AND THE NAV (DAYONE-SETUP-UI-3; part
// of #2688; plan §5.1, §5.2).
//
// WHAT EACH CASE HOLDS:
//   * /admin/setup is Admin › Setup for a platform admin, and a workspace
//     admin who types it is refused in place (the console's "is for platform
//     admins" page), never shown the strip;
//   * /admin/setup/health is the Health panel, with a nav row of its own;
//   * /admin/setup/github-app/callback is `GitHubAppCallback`, which posts
//     GitHub's `{code, state}` once, even though the router rewrites the bar;
//   * GitHub's return after the App is installed (the manifest's
//     `setup_url`, `/admin/setup?installation_id=..&setup_action=install`)
//     lands on Admin › Setup, which asks swarm-api about the installation at
//     once rather than after the first 10 s poll (DAYONE-FLOW-APP-RETURN);
//   * the paths and the Admin panel rows: Setup and Health are a platform
//     admin's, never in a workspace admin's `adminPagesFor`.
//
// Stubbed fetch only; no network, no credentials.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { SECTIONS } from '../App'
import { FIXED, addressToPath, pathToAddress } from '../paths'
import { PANEL_PAGES, adminPagesFor } from '../Spine'

type Json = Record<string, unknown>

const HOST = 'swarm.example.com'

function me(isAdmin: boolean): Json {
  return {
    tenant: { tenant_id: 'eng', kind: 'group', display_name: 'eng@example.com' },
    principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: isAdmin },
    environment: 'dev',
    environment_declared: true,
  }
}

const MINE_ADMIN = [{ tenant_id: 'eng', display_name: 'eng@example.com', kind: 'group', via: 'member', role: { name: 'admin', source: 'override' } }]

const SETUP: Json = {
  env: 'prod',
  engine: {
    release: 'v1.0.0',
    hostname: HOST,
    stages: Array.from({ length: 16 }, (_, i) => (i <= 8 || i === 10 || i === 13 ? { state: 'done', at: '2026-10-29T14:40:00Z', by: 'ana@example.com' } : null)),
    lease: { holder: 'laptop', heartbeat: new Date().toISOString(), expires: null },
    verify: null,
    hand_off_at: '2026-10-29T14:45:00Z',
    ready_at: null,
  },
  cli_running: true,
  github_app: null,
  teams: [],
  accounts: {},
  finished: null,
  addons: [],
  hostname: HOST,
  github_org: 'example-org',
}

const HEALTH: Json = {
  latest: {
    run_at: '2026-10-29T14:58:00Z',
    // A row as swarm-doctor writes it (doctor.Finding.to_json).
    checks: [{ id: 'edge.certificate', status: 'met', detail: 'certificate ACTIVE', step: 'edge.front_door', fix: '', cause: '', repair: 'swarm install --repair edge.certificate' }],
    findings: [],
  },
}

async function mountApp(path: string, isAdmin: boolean, extra: (m: string, url: string, body: unknown) => { status: number; body: unknown } | null = () => null) {
  const calls = serve((m, url, body) => {
    if (url.includes('/v1/tenants/mine')) return { status: 200, body: isAdmin ? [] : MINE_ADMIN }
    if (url.includes('/v1/tenants/me')) return { status: 200, body: me(isAdmin) }
    if (m === 'GET' && url === '/v1/admin/setup') return isAdmin ? { status: 200, body: SETUP } : { status: 403, body: { detail: 'platform admins only' } }
    if (m === 'GET' && url === '/v1/admin/setup/health') return { status: 200, body: HEALTH }
    return extra(m, url, body)
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const mod = await import('../App')
  window.history.replaceState(null, '', path)
  render(<mod.App />)
  return calls
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

describe('Admin › Setup and Health, routed', () => {
  it('/admin/setup renders Admin › Setup for a platform admin and not for a workspace admin', async () => {
    await mountApp('/admin/setup', true)
    // MUTATION: drop the `admin/setup` case from SectionBody -> the default pane, and this fails.
    await waitFor(() => expect(document.querySelector('[aria-label="Install setup"]')).not.toBeNull())
    expect(window.location.pathname).toBe('/admin/setup')
    // The Admin panel draws Setup and Health for them.
    await waitFor(() => expect(document.querySelector('a[href="/admin/setup"]')).not.toBeNull())
    expect(document.querySelector('a[href="/admin/setup/health"]')).not.toBeNull()
    cleanup()

    await mountApp('/admin/setup', false)
    // MUTATION: name `setup` in WORKSPACE_ADMIN_PAGE_KEYS -> a workspace admin is drawn the page, and this fails.
    await waitFor(() => expect(visible(document.body)).toMatch(/Setup is for platform admins/))
    expect(document.querySelector('[aria-label="Install setup"]')).toBeNull()
    expect(document.querySelector('a[href="/admin/setup"]')).toBeNull()
  })

  it('/admin/setup/health renders the Health panel', async () => {
    await mountApp('/admin/setup/health', true)
    // MUTATION: map /admin/setup/health to Setup, or drop its case -> fails.
    await waitFor(() => expect(screen.getByText('edge.certificate')).toBeTruthy())
    expect(window.location.pathname).toBe('/admin/setup/health')
    expect(document.querySelector('[aria-label="Install setup"]')).toBeNull()
  })

  it('/admin/setup/github-app/callback renders GitHubAppCallback', async () => {
    const code = `c${Math.random().toString(36).slice(2)}`
    const state = `s${Math.random().toString(36).slice(2)}`
    const calls = await mountApp(`/admin/setup/github-app/callback?code=${code}&state=${state}`, true, (m, url) =>
      m === 'POST' && url === '/v1/admin/setup/github-app/conversion' ? { status: 200, body: { id: 4242, slug: 'swarmcloud-example' } } : null,
    )
    // MUTATION: let the router drop the query before the page reads it -> nothing is posted, and this fails.
    await waitFor(() => expect(screen.getByText('GitHub App created')).toBeTruthy())
    const posts = calls.filter((c) => c.url === '/v1/admin/setup/github-app/conversion')
    expect(posts).toHaveLength(1)
    expect(posts[0]!.body).toEqual({ code, state })
    // The code is single-use and leaves the bar.
    expect(window.location.search).toBe('')
    expect(window.location.pathname).toBe('/admin/setup/github-app/callback')
  })

  it('GitHub\'s return after the install lands on Admin › Setup and checks the installation at once', async () => {
    const created = { ...SETUP, github_app: { started: true, id: 4242, slug: 'swarmcloud-example', client_id: 'Iv1.example', installed_on: null, secrets_written_at: '2026-10-29T14:50:00Z', self_check: null } }
    const calls = serve((m, url) => {
      if (url.includes('/v1/tenants/mine')) return { status: 200, body: [] }
      if (url.includes('/v1/tenants/me')) return { status: 200, body: me(true) }
      if (m === 'GET' && url === '/v1/admin/setup') return { status: 200, body: created }
      if (m === 'GET' && url === '/v1/admin/setup/github-app/installation') return { status: 200, body: { installed: true, org: 'example-org', slug: 'swarmcloud-example' } }
      return null
    })
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const mod = await import('../App')
    window.history.replaceState(null, '', '/admin/setup?installation_id=7&setup_action=install')
    render(<mod.App />)
    await waitFor(() => expect(document.querySelector('[aria-label="Install setup"]')).not.toBeNull())
    expect(window.location.pathname).toBe('/admin/setup')
    // MUTATION: check only on the 10 s interval -> no read within waitFor's
    // second, and this fails.
    await waitFor(() => expect(calls.some((c) => c.url === '/v1/admin/setup/github-app/installation')).toBe(true))
    // The installation's id from the query is never trusted or forwarded.
    expect(calls.every((c) => !c.url.includes('installation_id'))).toBe(true)
  })

  it('has its addresses, and its nav rows are a platform admin\'s alone', () => {
    // MUTATION: drop a FIXED entry or the callback's path -> fails.
    expect(FIXED['admin/setup']).toBe('/admin/setup')
    expect(addressToPath('admin/health')).toBe('/admin/setup/health')
    expect(pathToAddress('/admin/setup/health')?.address).toBe('admin/health')
    expect(pathToAddress('/admin/setup/github-app/callback', '?code=a&state=b')?.address).toBe('admin/setup/github-app/callback?code=a&state=b')
    expect(addressToPath('admin/setup/github-app/callback')).toBe('/admin/setup/github-app/callback')
    // Setup, Health beside it, both marked admin; Pool limits stays Admin's landing.
    const admin = SECTIONS.find((s) => s.id === 'admin')!
    const at = admin.tabs.findIndex((t) => t.id === 'setup')
    expect(admin.tabs.slice(at, at + 2)).toEqual([
      { id: 'setup', label: 'Setup', admin: true },
      { id: 'health', label: 'Health', admin: true },
    ])
    expect(admin.tabs[0]?.id).toBe('limits')
    const row = PANEL_PAGES.admin.findIndex((p) => p.key === 'setup')
    expect(PANEL_PAGES.admin.slice(row, row + 2).map((p) => p.to)).toEqual(['admin/setup', 'admin/health'])
    expect(adminPagesFor(true, false).map((p) => p.key)).toEqual(expect.arrayContaining(['setup', 'health']))
    // MUTATION: hand Setup to a workspace admin -> fails.
    expect(adminPagesFor(false, true).map((p) => p.key)).not.toContain('setup')
    expect(adminPagesFor(false, true).map((p) => p.key)).not.toContain('health')
  })
})
