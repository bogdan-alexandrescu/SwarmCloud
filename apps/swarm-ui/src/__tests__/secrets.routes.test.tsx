// CONFIG › SECRETS' ADDRESSES (lane SSC-UI, part of #2158;
// docs/design/secrets-scopes.md §6.1, §6.3, and §11's answers). Every old
// address of a secret's page lands on `/config/secrets`, and a one-time
// link's token survives the move to reach the form -- but never enters an
// address the router writes, because the router writes its address back into
// the bar and `canonical` is what a copied link is made of:
//
//   * Settings › API keys (`/settings/keys`) and its TypeSafe link
//     (`/settings/keys/typesafe?scope=user|tenant&t=..`), moved into Keys for
//     agents and MCP (BGD 2026-10-23 23:57Z);
//   * Settings › Workspace › Secrets' link (`/settings/workspace/secrets?..&t=..`);
//   * the runner-key link SSC-LINKS mints
//     (`/config/secrets?scope=workspace&runner_key=<provider>&t=..`, BGD 2026-10-24);
//   * a repository store's link and the GitHub Actions link, with their scope,
//     repository, destination, name and environment;
//   * Admin › Platform secrets, a platform admin's tab.
//
// Every token is built at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { KEY_LINK_PATH, addressToPath, pathToAddress, secretsLinkToken } from '../paths'
import { SECTIONS, canonical, fromAddress } from '../App'

const token = () => ['lnk', 'ssc'].join('') + 'Tz5'.repeat(10)

/** The address the router keeps for `path?search`, and the path it writes back. */
function land(path: string, search = '') {
  const p = pathToAddress(path, search)
  expect(p, `${path}${search}`).not.toBeNull()
  const r = fromAddress(p!.address)
  return { address: p!.address, route: r, written: addressToPath(canonical(r)) }
}

describe('the old addresses land on Config › Secrets', () => {
  it('Settings › API keys is Keys for agents and MCP now', () => {
    const { route, written } = land('/settings/keys')
    // MUTATION: keep `settings/keys` a Settings page -> the route is Settings and this fails.
    expect(route).toMatchObject({ sectionId: 'config', tab: 'secrets' })
    expect(written).toBe('/config/secrets')
    expect(addressToPath('settings/keys')).toBe('/config/secrets')
    // `/settings` alone is the head of Profile's path now.
    expect(land('/settings').written).toBe('/settings/profile')
  })

  it('the TypeSafe key link opens the key form in its scope, and its token is in no address', () => {
    const t = token()
    for (const [scope, view] of [['user', 'scope=personal&key=typesafe'], ['tenant', 'scope=workspace&key=typesafe']] as const) {
      const { address, route, written } = land(KEY_LINK_PATH, `?scope=${scope}&t=${t}`)
      // MUTATION: pass the link's query through -> the token is in the address and this fails.
      expect(address).not.toContain(t)
      expect(route).toMatchObject({ sectionId: 'config', tab: 'secrets', view })
      expect(written).toBe(`/config/secrets?${view}`)
      // MUTATION: stop reading `t` on the key link's old path -> App never captures it and the form gets none.
      expect(secretsLinkToken(KEY_LINK_PATH, `?scope=${scope}&t=${t}`)).toBe(t)
    }
  })

  it('every one-time link keeps what names its form, and never its token', () => {
    const t = token()
    const cases: readonly (readonly [string, string, string])[] = [
      ['/settings/workspace/secrets', `?action=rotate&name=typesafe-key&t=${t}`, 'action=rotate&name=typesafe-key'],
      ['/config/secrets', `?scope=workspace&runner_key=anthropic&t=${t}`, 'scope=workspace&runner_key=anthropic'],
      ['/config/secrets', `?scope=repository&repo=repo_0a1b2c3d4e5f6071&action=add&t=${t}`, 'scope=repository&repo=repo_0a1b2c3d4e5f6071&action=add'],
      ['/config/secrets', `?scope=repository&repo=repo_0a1b2c3d4e5f6071&dest=actions&name=MERGE_APP_PRIVATE_KEY&env=repo-secrets&t=${t}`,
        'scope=repository&repo=repo_0a1b2c3d4e5f6071&dest=actions&name=MERGE_APP_PRIVATE_KEY&env=repo-secrets'],
    ]
    for (const [path, search, view] of cases) {
      const { address, route, written } = land(path, search)
      expect(address).not.toContain(t)
      // MUTATION: drop `scope`, `repo`, `dest`, `runner_key` or `env` from the kept view -> one of these fails.
      expect(route.view, `${path}${search}`).toBe(view)
      expect(written).toBe(`/config/secrets?${view}`)
      expect(secretsLinkToken(path, search)).toBe(t)
    }
  })

  it('keeps nothing of the wrong shape: a repo outside the repository scope, a made-up scope, an unknown key', () => {
    expect(land('/config/secrets', '?scope=everything&repo=x').route.view ?? null).toBeNull()
    expect(land('/config/secrets', '?scope=workspace&repo=repo_x').route.view).toBe('scope=workspace')
    expect(land('/config/secrets', '?scope=repository&repo=../../x').route.view).toBe('scope=repository')
    expect(land('/config/secrets', '?scope=personal&tenant=u-other').route.view).toBe('scope=personal')
  })
})

describe('Admin › Platform secrets', () => {
  it('is a platform admin\'s tab at /admin/platform-secrets', () => {
    const tab = SECTIONS.find((s) => s.id === 'admin')?.tabs.find((t) => t.id === 'platform-secrets')
    // MUTATION: drop the tab, or its admin mark -> fails.
    expect(tab).toMatchObject({ label: 'Platform secrets', admin: true })
    const { route, written } = land('/admin/platform-secrets')
    expect(route).toMatchObject({ sectionId: 'admin', tab: 'platform-secrets' })
    expect(written).toBe('/admin/platform-secrets')
  })
})

// ---------------------------------------------------------------------------
// The whole app: an old link opens the new form, and its token reaches the write
// ---------------------------------------------------------------------------

type Json = Record<string, unknown>
const ME: Json = {
  tenant: { tenant_id: 'eng', kind: 'group', display_name: 'eng@example.com' },
  principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: false },
  environment: 'dev',
  environment_declared: true,
}
const MINE: Json[] = [{ tenant_id: 'eng', display_name: 'eng@example.com', kind: 'group', via: 'member', role: { name: 'admin', source: 'override' } }]

function serveApp() {
  const calls: { method: string; url: string; body: unknown }[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    const body = init?.body ? JSON.parse(String(init.body)) : null
    calls.push({ method, url, body })
    const reply = url.includes('/v1/tenants/mine') ? MINE
      : url.includes('/v1/tenants/me') ? ME
        : url.startsWith('/v1/secrets/overview') ? { tenant_id: 'eng', scope: 'personal', personal_tenant_id: 'u-ana-b', can_manage: true, rows: [] }
          : method === 'PUT' && url === '/v1/provider-keys/typesafe/user' ? { set: true }
            : undefined
    return new Response(JSON.stringify(reply ?? { detail: 'Not Found' }), { status: reply === undefined ? 404 : 200, headers: { 'content-type': 'application/json' } })
  }) as unknown as typeof fetch
  return calls
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  localStorage.clear()
  sessionStorage.clear()
  window.history.replaceState(null, '', '/')
})

describe('an old one-time link, opened in the app', () => {
  it('/settings/keys/typesafe?scope=user&t=.. opens Config › Secrets\' key form, and the token goes back with the write alone', async () => {
    const t = token()
    const value = ['ts', 'key'].join('-') + 'Lp7'.repeat(10)
    const calls = serveApp()
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const mod = await import('../App')
    window.history.replaceState(null, '', `${KEY_LINK_PATH}?scope=user&t=${t}`)
    render(<mod.App />)
    // MUTATION: route the old path to a Settings page -> the bar stays there and this fails.
    await waitFor(() => expect(window.location.pathname).toBe('/config/secrets'))
    expect(window.location.search).toBe('?scope=personal&key=typesafe')
    expect(window.location.href).not.toContain(t)
    const form = await screen.findByRole('form', { name: /set your typesafe key/i })
    // The link's token is the form's: the page mints none of its own.
    expect(calls.filter((c) => c.url.includes('/links'))).toHaveLength(0)
    fireEvent.change(within(form).getByLabelText(/typesafe key/i), { target: { value } })
    await act(async () => {
      fireEvent.click(within(form).getByRole('button', { name: /set secret/i }))
    })
    await screen.findByText(/your typesafe key was written/i)
    const put = calls.filter((c) => c.method === 'PUT')
    // MUTATION: drop App's capture of the key link's token -> no link_token and this fails.
    expect(put).toHaveLength(1)
    expect(put[0]).toMatchObject({ url: '/v1/provider-keys/typesafe/user', body: { api_key: value, link_token: t } })
    expect(document.documentElement.outerHTML).not.toContain(value)
    expect(document.documentElement.outerHTML).not.toContain(t)
  })
})
