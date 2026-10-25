// ADMIN › PLATFORM SECRETS (lane SSC-UI over SSC-PLATFORM-STATUS, part of
// #2158; docs/design/secrets-scopes.md §5, proposal E S7). Held to:
//
//   * the tab is absent for a non-admin: a workspace admin's Admin panel and
//     a member's spine do not list it, and only a platform admin's does;
//   * STATUS ONLY: set, versions, the newest one's age and who writes it,
//     and never a value, even one the route wrongly served;
//   * a non-admin who types the address is shown the route's 403, not rows.
//
// Fixtures only; no network. Every value is built at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { adminGroups, adminPagesFor } from '../Spine'

const AT = '2026-10-20T09:30:00+00:00'
const fakeValue = () => ['pf', 'sec'].join('-') + 'Nb3'.repeat(12)

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../AdminPlatformSecrets')
}

describe('the Platform secrets tab is absent for a non-admin', () => {
  it('only a platform admin\'s Admin pages list it, under Platform', () => {
    const tab = (pages: readonly { to: string }[]) => pages.some((p) => p.to === 'admin/platform-secrets')
    // MUTATION: drop it from PANEL_PAGES.admin -> a platform admin cannot reach it and this fails.
    expect(tab(adminPagesFor(true, false))).toBe(true)
    expect(adminGroups(adminPagesFor(true, false)).find((g) => g.key === 'platform')?.rows.some((p) => p.to === 'admin/platform-secrets')).toBe(true)
    // MUTATION: hand it to a workspace admin (name it in ADMIN_WORKSPACE_KEYS) -> fails.
    expect(tab(adminPagesFor(false, true))).toBe(false)
    expect(adminPagesFor(false, false)).toEqual([])
  })

  it('a workspace admin\'s spine draws Admin without it', async () => {
    const me = { tenant: { tenant_id: 'eng', kind: 'group', display_name: 'eng@example.com' }, principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: false }, environment: 'dev', environment_declared: true }
    const mine = [{ tenant_id: 'eng', display_name: 'eng@example.com', kind: 'group', via: 'member', role: { name: 'admin', source: 'override' } }]
    serve((_m, url) => (url.includes('/v1/tenants/mine') ? { status: 200, body: mine } : url.includes('/v1/tenants/me') ? { status: 200, body: me } : null))
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const mod = await import('../App')
    window.history.replaceState(null, '', '/admin/workspaces')
    render(<mod.App />)
    await waitFor(() => expect(document.querySelector('a[href="/admin/workspaces"]')).not.toBeNull())
    // MUTATION: list the tab for every Admin reader -> fails.
    expect(document.querySelector('a[href="/admin/platform-secrets"]')).toBeNull()
    expect(visible(document.body)).not.toMatch(/Platform secrets/)
  })
})

describe('status only', () => {
  it('draws set, versions, age and who writes it, the accounts as one line, and never a value', async () => {
    const value = fakeValue()
    serve((_m, url) => (url === '/v1/admin/platform-secrets'
      ? { status: 200, body: {
        secrets: [
          { secret_id: 'swarm-search-api-key', status: 'set', reason: null, enabled_versions: 2, newest_enabled_at: AT, rotation_in_grace: true, managed_by: 'terraform', written_by: 'scripts/create-secrets.sh --search-api-key', family: 'platform', value, payload: value },
          { secret_id: 'swarm-live-key', status: 'not_declared', reason: null, enabled_versions: null, newest_enabled_at: null, rotation_in_grace: false, managed_by: 'terraform', written_by: 'scripts/create-secrets.sh --live-key', family: 'platform' },
          { secret_id: 'swarm-account-eng--max-1', status: 'set', enabled_versions: 1, newest_enabled_at: AT, managed_by: 'quota-broker', written_by: 'the quota broker', family: 'account', owner_tenant: 'eng', label: 'max-1', half: 'access' },
          { secret_id: 'swarm-account-eng--max-1-refresh', status: 'set', enabled_versions: 1, newest_enabled_at: AT, managed_by: 'quota-broker', written_by: 'the quota broker', family: 'account', owner_tenant: 'eng', label: 'max-1', half: 'refresh' },
        ],
        accounts_unread: false, accounts_refused: 0, accounts_truncated: false, read_at: AT,
      } }
      : null))
    const { AdminPlatformSecretsScreen } = await load()
    render(<AdminPlatformSecretsScreen />)
    const table = await screen.findByRole('table', { name: /platform secrets/i })
    const row = (name: RegExp) => visible(within(table).getByRole('row', { name }))
    expect(row(/swarm-search-api-key/)).toMatch(/set/)
    // MUTATION: drop the grace note -> fails.
    expect(row(/swarm-search-api-key/)).toMatch(/2.*rotation in its grace/)
    expect(row(/swarm-search-api-key/)).toMatch(/create-secrets\.sh --search-api-key/)
    expect(row(/swarm-live-key/)).toMatch(/not declared/)
    // The lead names the one that needs a look.
    expect(visible(screen.getByRole('region', { name: /needs a look/i }))).toMatch(/swarm-live-key is not declared/)
    // MUTATION: draw each account half as its own row -> fails.
    expect(within(table).getAllByRole('row')).toHaveLength(1 + 3)
    expect(row(/swarm-account-/)).toMatch(/1 account.*all 2 set/)
    // MUTATION: copy the row whole -> the served value is drawn and this fails.
    expect(document.documentElement.outerHTML).not.toContain(value)
    // No form: nothing on this page can set one.
    expect(document.querySelector('input, form')).toBeNull()
  })

  it('a non-admin who types the address sees the route\'s refusal, not rows', async () => {
    serve((_m, url) => (url === '/v1/admin/platform-secrets' ? { status: 403, body: { detail: 'admin only', code: 'admin_required' } } : null))
    const { AdminPlatformSecretsScreen } = await load()
    render(<AdminPlatformSecretsScreen />)
    await waitFor(() => expect(visible(document.body)).toMatch(/admin/i))
    expect(screen.queryByRole('table', { name: /platform secrets/i })).toBeNull()
  })
})
