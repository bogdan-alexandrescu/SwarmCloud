// ADMIN › PLATFORM SECRETS (lane SSC-UI, part of #2158;
// docs/design/secrets-scopes.md §5, proposal E S7). Status only, from
// `GET /v1/admin/platform-secrets`: set or not, enabled versions, the newest
// one's age, who manages it. No form, and nothing of a value even when the
// API wrongly serves one. The deployed state until the owner's bootstrap
// apply (`unreadable`, `permission_denied`) is said as that.
//
// Fixtures only; no network. The leaked value is built at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>

const leaked = () => ['plat', 'demo'].join('-') + 'Yt6'.repeat(12)

function row(secret_id: string, over: Json = {}): Json {
  return { secret_id, managed_by: 'terraform', written_by: `scripts/create-secrets.sh --${secret_id}`, status: 'set', reason: null, enabled_versions: 1, newest_enabled_at: '2026-10-01T00:00:00+00:00', rotation_in_grace: false, family: 'platform', ...over }
}

async function screenWith(body: Json) {
  serve((m, url) => (m === 'GET' && url === '/v1/admin/platform-secrets' ? { status: 200, body } : null))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { AdminPlatformSecretsScreen } = await import('../AdminPlatformSecrets')
  render(<AdminPlatformSecretsScreen />)
  return screen.findByRole('table', { name: /platform secrets/i })
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
})

describe('Admin › Platform secrets', () => {
  it('lists each secret’s status, versions and manager, leads with the one that needs a look, and offers no form', async () => {
    const value = leaked()
    const table = await screenWith({
      secrets: [
        row('swarm-search-api-key', { enabled_versions: 2, rotation_in_grace: true, value, payload: value, length: value.length }),
        row('swarm-live-key', { status: 'not_declared', enabled_versions: null, newest_enabled_at: null }),
      ],
      accounts_unread: false, accounts_refused: 0, accounts_truncated: false, read_at: '2026-10-24T00:00:00+00:00',
    })
    expect(visible(within(table).getByRole('row', { name: /swarm-search-api-key/ }))).toMatch(/rotation in its grace/)
    expect(visible(within(table).getByRole('row', { name: /swarm-live-key/ }))).toMatch(/not declared/)
    // MUTATION: lead with the first row whatever its state -> fails.
    expect(visible(screen.getByRole('region', { name: /needs a look/i }))).toMatch(/swarm-live-key is not declared/)
    // MUTATION: draw a served value or its length -> fails.
    expect(document.documentElement.outerHTML).not.toContain(value)
    expect(document.querySelector('input, textarea, form')).toBeNull()
  })

  it('says the status role is not applied yet when every row is permission_denied', async () => {
    await screenWith({
      secrets: [row('swarm-live-key', { status: 'unreadable', reason: 'permission_denied', enabled_versions: null, newest_enabled_at: null })],
      accounts_unread: false, accounts_refused: 0, accounts_truncated: false, read_at: null,
    })
    // MUTATION: call it "needs a look" like a missing secret -> fails.
    expect(visible(screen.getByRole('region', { name: /not readable yet/i }))).toMatch(/enable_platform_secret_status = true/)
  })
})
