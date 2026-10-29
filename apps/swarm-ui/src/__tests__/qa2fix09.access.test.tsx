// VISUAL QA PASS 2, LANE QA2-FIX-09 (part of #2694): CONFIG › GITHUB ACCESS,
// as the QA2-CONFIG sweep found it on the fixture console on 2026-10-29.
//
//   * config2-02: an expired token on an active connection has one state,
//     `active · token renews on next use`, with no ok check beside its
//     expired warning; `githubTokenState` is the word Secrets › GitHub token
//     is to take too;
//   * config2-22: the installs card's head offers another organisation, and
//     the person's own row says it installs on their account.
//
// Fixtures only; no network. Fake values are built at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { serve } from './repofixture'

const ID = (n: number) => `repo_${String(n).padStart(16, '0')}`
const DAY = 86_400_000

function connection(over: Record<string, unknown> = {}) {
  return {
    connection_id: 'github:eng:abc',
    forge: 'github',
    method: 'app_user',
    forge_login: 'octo-dev',
    forge_user_id: 42,
    token_id: 'tok_u',
    secret_name: 'swarm-tenant-eng-git-u-' + 'a'.repeat(16),
    state: 'active',
    failure: null,
    access_expires_at: new Date(Date.now() + 7 * 3600_000).toISOString(),
    refresh_expires_at: null,
    refreshed_at: new Date(Date.now() - 600_000).toISOString(),
    refreshing: false,
    created_at: new Date().toISOString(),
    connected_at: new Date().toISOString(),
    revoked_at: null,
    ...over,
  }
}

const ORG = {
  owner: 'example-org',
  owner_type: 'Organization',
  installation_id: 7,
  repository_selection: 'selected',
  install_state: 'installed',
  sso: 'ok',
  enabled: true,
  enabled_at: null,
  checked_at: null,
}

const GRANT = (n: number, repository: string, mode: 'read' | 'write') => ({
  repo_id: ID(n),
  repository,
  owner: repository.split('/')[0],
  mode,
  can_push: true,
  archived: false,
  granted_at: null,
  granted_by: 'dev@example.com',
  checks: {},
  verified_at: null,
})

const GRANTS = [
  GRANT(1, 'example-org/example-api', 'write'),
  GRANT(2, 'example-org/example-docs', 'read'),
  GRANT(3, 'example-org/example-web', 'write'),
]

const INSTALL = 'https://github.com/apps/swarmcloud/installations/new'
const OWNERS = {
  tenant_id: 'eng',
  orgs_listed: true,
  install_url: INSTALL,
  owners: [
    { ...ORG, owner: 'octo-dev', owner_type: 'User', install_state: 'not_installed', enabled: false, installation_id: null, install_url: INSTALL },
    { ...ORG, install_url: null },
    { ...ORG, owner: 'example-labs', install_state: 'not_installed', sso: 'required', enabled: false, installation_id: null, install_url: INSTALL },
  ],
}

function api(conn: Record<string, unknown> = {}) {
  // Every write and the repository list answer 404 with no code: the fixture
  // build's "not served", which is what the sweep met.
  return serve((m, url) => {
    if (m === 'GET' && url === '/v1/access') return { status: 200, body: { tenant_id: 'eng', connection: connection(conn), orgs: [ORG], grants: GRANTS } }
    if (m === 'GET' && url === '/v1/access/orgs') return { status: 200, body: OWNERS }
    return null
  })
}

async function mount(path = '/config/github') {
  window.history.replaceState(null, '', path)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { AccessScreen } = await import('../Access')
  return render(<AccessScreen />)
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

describe('config2-02: one state for an expired token on an active connection', () => {
  it('draws the state as active · token renews on next use, in warn tone, with no ok check', async () => {
    api({ access_expires_at: new Date(Date.now() - 10 * DAY).toISOString(), refreshed_at: new Date(Date.now() - 10 * DAY).toISOString() })
    await mount()
    const state = await screen.findByText('active · token renews on next use')
    const card = state.closest('.ac-conn') as HTMLElement
    // MUTATION: put back `tone={active ? 'ok' : 'bad'}` on the state ToneMark -- the expired token keeps its check.
    expect(card.querySelector('[data-tone="ok"]')).toBeNull()
    expect(state.closest('[data-tone="warn"]')).not.toBeNull()
    // The renewal is said once, in the state.
    expect(card.textContent).not.toContain('the next use renews it')
  })

  it('names the same word for both pages from one helper', async () => {
    const { githubTokenState } = await import('../githubTokenState')
    const now = Date.parse('2026-10-29T10:00:00Z')
    // MUTATION: return `expired` (Secrets' old word) or `active` for an expired active token.
    expect(githubTokenState('active', '2026-10-19T10:00:00Z', now)).toEqual({ word: 'active · token renews on next use', tone: 'warn' })
    expect(githubTokenState('active', '2026-10-29T18:00:00Z', now)).toEqual({ word: 'active', tone: 'ok' })
    expect(githubTokenState('active', null, now)).toEqual({ word: 'active', tone: 'ok' })
    expect(githubTokenState('revoked', '2026-10-19T10:00:00Z', now)).toEqual({ word: 'revoked', tone: 'bad' })
  })
})

describe('config2-22: one install control per target', () => {
  it('heads the card with another organisation and labels the own-account row', async () => {
    api()
    await mount()
    const table = await screen.findByRole('table', { name: 'Where the App is installed' })
    const card = table.closest('.ac-installs') as HTMLElement
    // MUTATION: put back "Install on an organisation…" in the Installs card head.
    expect(within(card).getByRole('link', { name: 'Install on another organisation…' }).getAttribute('href')).toBe(INSTALL)
    expect(within(card).queryByRole('link', { name: 'Install on an organisation…' })).toBeNull()
    const mine = table.querySelector('tr[data-owner="octo-dev"]') as HTMLElement
    // MUTATION: put back the bare `Install` label on a User owner's row.
    expect(within(mine).getByRole('link', { name: 'Install on your account' })).toBeTruthy()
  })
})
