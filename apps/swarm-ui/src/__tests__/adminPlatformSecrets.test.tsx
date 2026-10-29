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
    expect(row(/swarm-live-key/)).toMatch(/no secret/)
    // The lead names the one that needs a look.
    expect(visible(screen.getByRole('region', { name: /needs a look/i }))).toMatch(/swarm-live-key has no secret yet/)
    // MUTATION: draw each account half as its own row -> fails.
    expect(within(table).getAllByRole('row')).toHaveLength(1 + 3)
    expect(row(/swarm-account-/)).toMatch(/1 account.*all 2 set/)
    // MUTATION: copy the row whole -> the served value is drawn and this fails.
    expect(document.documentElement.outerHTML).not.toContain(value)
    // No form: nothing on this page can set one.
    expect(document.querySelector('input, form')).toBeNull()
  })

  // VQ-049 (part of #2391): with the status role not granted every row is
  // `unreadable`, and the lead said "31 platform secrets are not set" over a
  // running platform, with 'no secret' and 'no version' drawn as if measured.
  it('an unread status is said as unread, with its reason and step, never counted as missing', async () => {
    const denied = (id: string, family = 'platform', extra: Record<string, unknown> = {}) => ({
      secret_id: id, status: 'unreadable', reason: 'permission_denied', enabled_versions: null, newest_enabled_at: null,
      rotation_in_grace: false, managed_by: 'terraform', written_by: 'scripts/create-secrets.sh --stdin', family, ...extra,
    })
    serve((_m, url) => (url === '/v1/admin/platform-secrets'
      ? { status: 200, body: {
        secrets: [
          denied('swarm-search-api-key'),
          denied('swarm-live-key'),
          denied('swarm-egress-key'),
          denied('swarm-account-eng--max-1', 'account', { owner_tenant: 'eng', label: 'max-1', half: 'access', managed_by: 'quota-broker' }),
          denied('swarm-account-eng--max-1-refresh', 'account', { owner_tenant: 'eng', label: 'max-1', half: 'refresh', managed_by: 'quota-broker' }),
        ],
        accounts_unread: false, accounts_refused: 0, accounts_truncated: false, read_at: AT,
      } }
      : null))
    const { AdminPlatformSecretsScreen } = await load()
    render(<AdminPlatformSecretsScreen />)
    const table = await screen.findByRole('table', { name: /platform secrets/i })
    // MUTATION: count `unreadable` with `not_set` again -> "5 platform secrets are not set" and this fails.
    expect(screen.queryByRole('region', { name: /needs a look/i })).toBeNull()
    expect(visible(document.body)).not.toMatch(/not set/)
    const lead = visible(screen.getByRole('region', { name: /status not read/i }))
    expect(lead).toMatch(/status of 5 platform secrets cannot be read/i)
    expect(lead).toMatch(/status role is not granted/)
    expect(lead).toMatch(/enable_platform_secret_status/)
    expect(lead).toMatch(/make bootstrap/)
    // An unknown figure is a dash with its reason, never a measured absence.
    for (const name of [/swarm-search-api-key/, /swarm-live-key/, /swarm-account-/]) {
      const r = within(table).getByRole('row', { name })
      for (const label of ['Enabled versions', 'Age of newest']) {
        const cell = r.querySelector(`td[data-label="${label}"]`)!
        // MUTATION: draw 'no secret' / 'no version' / a summed 0 for an unread row -> fails.
        expect(visible(cell as HTMLElement), `${label} of ${name}`).toMatch(/^— ?not read$/)
      }
    }
    // MUTATION: count the unread account halves as unset -> "0 of 2 set" and this fails.
    expect(visible(within(table).getByRole('row', { name: /swarm-account-/ }))).not.toMatch(/0 of 2/)
    expect(visible(within(table).getByRole('row', { name: /swarm-account-/ }))).toMatch(/unreadable/)
  })

  it('a missing secret still leads when others are unread, and the unread ones are named apart', async () => {
    serve((_m, url) => (url === '/v1/admin/platform-secrets'
      ? { status: 200, body: {
        secrets: [
          { secret_id: 'swarm-live-key', status: 'not_declared', reason: null, enabled_versions: null, newest_enabled_at: null, rotation_in_grace: false, managed_by: 'terraform', written_by: 'scripts/create-secrets.sh --live-key', family: 'platform' },
          { secret_id: 'swarm-egress-key', status: 'unreadable', reason: 'read_failed', enabled_versions: null, newest_enabled_at: null, rotation_in_grace: false, managed_by: 'terraform', written_by: 'terraform', family: 'platform' },
        ],
        accounts_unread: false, accounts_refused: 0, accounts_truncated: false, read_at: AT,
      } }
      : null))
    const { AdminPlatformSecretsScreen } = await load()
    render(<AdminPlatformSecretsScreen />)
    const lead = visible(await screen.findByRole('region', { name: /needs a look/i }))
    expect(lead).toMatch(/swarm-live-key has no secret yet/)
    // MUTATION: fold the unread one into the missing count -> "2 platform secrets are not set" and this fails.
    expect(lead).toMatch(/1 other could not be read: Secret Manager did not answer/)
  })

  // VISUAL QA PASS 1, admin-shell-22 (part of #2596): the lead said
  // "swarm-live-key is not declared" and then "Its name is in the catalogue",
  // and the row's pill said "not declared" over columns that said "no secret".
  it('a catalogue name with no secret is said as "no secret" in the lead and the pill, never "not declared"', async () => {
    serve((_m, url) => (url === '/v1/admin/platform-secrets'
      ? { status: 200, body: {
        secrets: [
          { secret_id: 'swarm-live-key', status: 'not_declared', reason: null, enabled_versions: null, newest_enabled_at: null, rotation_in_grace: false, managed_by: 'terraform', written_by: 'scripts/create-secrets.sh --live-key', family: 'platform' },
          { secret_id: 'swarm-search-api-key', status: 'set', reason: null, enabled_versions: 1, newest_enabled_at: AT, rotation_in_grace: false, managed_by: 'terraform', written_by: 'scripts/create-secrets.sh --search-api-key', family: 'platform' },
        ],
        accounts_unread: false, accounts_refused: 0, accounts_truncated: false, read_at: AT,
      } }
      : null))
    const { AdminPlatformSecretsScreen } = await load()
    render(<AdminPlatformSecretsScreen />)
    const lead = visible(await screen.findByRole('region', { name: /needs a look/i }))
    // MUTATION: put back `${id} is ${STATUS_WORDS[status]}` -> "is not declared" and this fails.
    expect(lead).toMatch(/swarm-live-key has no secret yet/)
    expect(lead).toMatch(/It is in the catalogue but nothing was stored\./)
    expect(lead).not.toMatch(/not declared|no secret exists/)
    const table = screen.getByRole('table', { name: /platform secrets/i })
    const pill = within(table).getByRole('row', { name: /swarm-live-key/ }).querySelector('td[data-label="Set"] .sec-chip')!
    // MUTATION: STATUS_WORDS.not_declared back to 'not declared' -> fails.
    expect(visible(pill as HTMLElement)).toBe('no secret')
    expect(visible(document.body)).not.toMatch(/not declared/)
  })

  // VISUAL QA PASS 2, admin2-16 (QA2-FIX-10, part of #2694): the
  // swarm-live-key row said "no secret" three times -- the pill, Enabled
  // versions and Age of newest. The pill says it; the figures are a dash.
  it('says "no secret" once, in the pill, and dashes the figures beside it', async () => {
    serve((_m, url) => (url === '/v1/admin/platform-secrets'
      ? { status: 200, body: {
        secrets: [
          { secret_id: 'swarm-live-key', status: 'not_declared', reason: null, enabled_versions: null, newest_enabled_at: null, rotation_in_grace: false, managed_by: 'terraform', written_by: 'scripts/create-secrets.sh --live-key', family: 'platform' },
          { secret_id: 'swarm-search-api-key', status: 'not_set', reason: null, enabled_versions: 0, newest_enabled_at: null, rotation_in_grace: false, managed_by: 'terraform', written_by: 'scripts/create-secrets.sh --search-api-key', family: 'platform' },
        ],
        accounts_unread: false, accounts_refused: 0, accounts_truncated: false, read_at: AT,
      } }
      : null))
    const { AdminPlatformSecretsScreen } = await load()
    render(<AdminPlatformSecretsScreen />)
    const table = await screen.findByRole('table', { name: /platform secrets/i })
    const row = within(table).getByRole('row', { name: /swarm-live-key/ })
    const cell = (label: string) => visible(row.querySelector(`td[data-label="${label}"]`) as HTMLElement)
    // MUTATION: put back `'no secret'` in Enabled versions or Age of newest -> fails.
    expect(cell('Enabled versions')).toBe('—')
    expect(cell('Age of newest')).toBe('—')
    expect(visible(row).match(/no secret/g)).toHaveLength(1)
    // A declared name with no version still says so: its pill does not.
    expect(visible(within(table).getByRole('row', { name: /swarm-search-api-key/ }).querySelector('td[data-label="Age of newest"]') as HTMLElement)).toBe('no version')
  })

  it('a non-admin who types the address sees the route\'s refusal, not rows', async () => {
    serve((_m, url) => (url === '/v1/admin/platform-secrets' ? { status: 403, body: { detail: 'admin only', code: 'admin_required' } } : null))
    const { AdminPlatformSecretsScreen } = await load()
    render(<AdminPlatformSecretsScreen />)
    await waitFor(() => expect(visible(document.body)).toMatch(/admin/i))
    expect(screen.queryByRole('table', { name: /platform secrets/i })).toBeNull()
  })
})
