// CONFIG › GITHUB ACCESS (GITHUB-ACCESS-PAGE; part of #2157, BGD's pick of
// 2026-10-24, proposal E, docs/web-ui/mockups/config-e.html S2a-S2f).
//
// WHAT EACH CASE HOLDS:
//   * the page is the GitHub App integration and Choose repositories, and
//     nothing else: a Connection tab (connect or disconnect, the person's
//     GitHub identity) and a Choose repositories tab, as S2 draws them;
//   * "Repositories SwarmCloud may reach as you" is no longer drawn here, on
//     either tab: it is ReachableRepositories.tsx, which Config › Repositories
//     mounts. Until then the page links there;
//   * the deep link `/config/github?view=choose` (Repositories' "Add new
//     repository") opens Choose repositories directly;
//   * every page leads with one next-action sentence and button (proposal E).
//
// Fixtures only; no network. Fake values are built at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { serve } from './repofixture'

const ID = (n: number) => `repo_${String(n).padStart(16, '0')}`

const CONNECTION = {
  connection_id: 'github:eng:abc',
  forge: 'github',
  method: 'app_user',
  forge_login: 'octo-dev',
  forge_user_id: 42,
  token_id: 'tok_u',
  secret_name: 'swarm-tenant-eng-git-u-89abcdef01234567',
  state: 'active',
  failure: null,
  access_expires_at: new Date(Date.now() + 7 * 3600_000).toISOString(),
  refresh_expires_at: null,
  refreshed_at: new Date(Date.now() - 600_000).toISOString(),
  refreshing: false,
  created_at: new Date().toISOString(),
  connected_at: new Date().toISOString(),
  revoked_at: null,
}

const ORG = (owner: string) => ({
  owner,
  owner_type: owner === 'octo-dev' ? 'User' : 'Organization',
  installation_id: 7,
  repository_selection: 'selected',
  install_state: 'installed',
  sso: 'ok',
  enabled: true,
  enabled_at: null,
  checked_at: null,
})

const GRANT = (n: number, repository: string, mode: 'read' | 'write') => ({
  repo_id: ID(n),
  repository,
  owner: repository.split('/')[0],
  mode,
  can_push: true,
  archived: false,
  granted_at: null,
  granted_by: 'dev@swarm.example.com',
  checks: { clone: { state: 'ok', code: null, checked_at: null } },
  verified_at: null,
})

const GRANTS = [GRANT(1, 'example-org/example-docs', 'read'), GRANT(2, 'example-org/example-service', 'write')]

function overview(over: Record<string, unknown> = {}) {
  return { tenant_id: 'eng', connection: CONNECTION, orgs: [ORG('example-org')], grants: GRANTS, ...over }
}

const OWNERS = { tenant_id: 'eng', orgs_listed: true, install_url: null, owners: [{ ...ORG('example-org'), install_url: null }] }
const PAGE1 = { tenant_id: 'eng', owner: 'example-org', repositories: [], page: 1, per_page: 100, max_pages: 10, next_page: null, capped: false, q: null, total_count: 0 }

function api(over: Record<string, unknown> = {}) {
  return serve((m, url) => {
    if (m === 'GET' && url === '/v1/access') return { status: 200, body: overview(over) }
    if (m === 'GET' && url === '/v1/access/orgs') return { status: 200, body: OWNERS }
    if (m === 'GET' && url === '/v1/access/orgs/example-org/repositories?page=1') return { status: 200, body: PAGE1 }
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

const tab = (name: string) => screen.getByRole('tab', { name }) as HTMLButtonElement

afterEach(() => {
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

describe('GitHub access is the GitHub App integration and Choose repositories', () => {
  it('draws a Connection tab and a Choose repositories tab, Connection open on the bare path', async () => {
    api()
    await mount()
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('GitHub access')
    const strip = screen.getByRole('tablist', { name: 'GitHub access' })
    // MUTATION: drop the Choose repositories tab from Access.tsx's GITHUB_TABS.
    expect(within(strip).getAllByRole('tab').map((t) => t.textContent)).toEqual(['Connection', 'Choose repositories'])
    // MUTATION: open the page on the Choose tab when the address names no view.
    expect(tab('Connection').getAttribute('aria-selected')).toBe('true')
    // MUTATION: stop rendering <Connection> on the Connection tab.
    expect(await screen.findByRole('button', { name: 'Disconnect GitHub' })).toBeTruthy()
    expect(screen.getByText('@octo-dev')).toBeTruthy()
  })

  it('offers Connect GitHub when nothing is connected, once, as the lead the page opens with', async () => {
    api({ connection: null, orgs: [], grants: [] })
    await mount()
    // MUTATION: put the ConnectButton back inside the GitHub card as well as the lead (it is drawn twice).
    expect(await screen.findAllByRole('button', { name: 'Connect GitHub' })).toHaveLength(1)
    const lead = document.querySelector('section.ac-lead') as HTMLElement
    // MUTATION: render no lead on the Connection tab.
    expect(lead).not.toBeNull()
    expect(within(lead).getByText('Connect GitHub to start')).toBeTruthy()
  })

  it('opens Choose repositories from its tab, and the lead offers it once connected', async () => {
    api({ grants: [] })
    await mount()
    const lead = await screen.findByRole('region', { name: 'Next step' })
    expect(within(lead).getByText('Choose the repositories SwarmCloud may reach')).toBeTruthy()
    // MUTATION: make the lead's button a no-op instead of opening the Choose tab.
    fireEvent.click(within(lead).getByRole('button', { name: 'Choose repositories' }))
    expect(tab('Choose repositories').getAttribute('aria-selected')).toBe('true')
    expect(document.getElementById('ac-choose')).not.toBeNull()
    // MUTATION: drop writeViewToUrl from the tab switch (a reload would land on Connection).
    expect(window.location.search).toBe('?view=choose')
    // The Connection card is on its own tab, not drawn under the chooser too.
    expect(screen.queryByRole('button', { name: 'Disconnect GitHub' })).toBeNull()
  })
})

describe('the reach-as-you list is not on GitHub access', () => {
  it('is drawn on neither tab, and the page links to Config › Repositories for it', async () => {
    api()
    await mount()
    await screen.findByRole('button', { name: 'Disconnect GitHub' })
    // MUTATION: render <ReachableRepositories> inside Mine again.
    expect(screen.queryByText('Repositories SwarmCloud may reach as you')).toBeNull()
    expect(document.querySelector('.ac-grants')).toBeNull()
    fireEvent.click(tab('Choose repositories'))
    expect(document.getElementById('ac-choose')).not.toBeNull()
    expect(screen.queryByText('Repositories SwarmCloud may reach as you')).toBeNull()
    expect(document.querySelector('.ac-grants')).toBeNull()
    // MUTATION: drop the Next: Repositories link from the Choose lead.
    const link = screen.getByRole('link', { name: 'Next: Repositories' })
    expect(link.getAttribute('href')).toBe('/config/repositories')
  })
})

describe('the deep link opens Choose repositories', () => {
  it('mounts on /config/github?view=choose with the chooser open', async () => {
    api()
    await mount('/config/github?view=choose')
    // MUTATION: drop 'choose' from viewFromUrl's accepted values.
    expect(tab('Choose repositories').getAttribute('aria-selected')).toBe('true')
    expect(await screen.findByRole('heading', { name: 'Choose repositories', level: 2 })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Disconnect GitHub' })).toBeNull()
  })

  it('is the address Repositories links to, as a path the router keeps', async () => {
    const { ADD_REPOSITORY_ADDRESS } = await import('../Access')
    const { addressToPath } = await import('../paths')
    // MUTATION: change ADD_REPOSITORY_ADDRESS to `config/github?add=1` (App's keptKeys drops `add`).
    expect(ADD_REPOSITORY_ADDRESS).toBe('config/github?view=choose')
    expect(addressToPath(ADD_REPOSITORY_ADDRESS)).toBe('/config/github?view=choose')
  })

  it('on the deep link with nothing connected, says to connect first and offers the Connection tab', async () => {
    api({ connection: null, orgs: [], grants: [] })
    await mount('/config/github?view=choose')
    // MUTATION: mount <Choose> for a person with no connection (it would read GitHub to be refused).
    expect(await screen.findByText('Connect GitHub first')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Open Connection' }))
    expect(tab('Connection').getAttribute('aria-selected')).toBe('true')
  })
})
