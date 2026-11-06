// CONFIG › GITHUB ACCESS, THE REST OF PROPOSAL E (GH-ACCESS-E-REST; part of
// #2157, BGD's pick of 2026-10-24, docs/web-ui/mockups/config-e.html S2a-S2f).
//
// WHAT EACH CASE HOLDS:
//   * S2a: not connected, the Connection tab draws "What comes after", a
//     dashed card naming setup's steps 2 to 4, each with what it waits for;
//   * S2b: connected, the Connection tab draws "Where the App is installed"
//     -- Owner / Install / Chosen, from the owners read Choose repositories
//     makes -- with Request install for an organisation without the App and
//     "Install on another organisation…" in the card's head (config2-22);
//   * S2c: Choose repositories ends in a foot bar, "N changes saved · <age> ·
//     Refresh", counted from the grants this visit wrote; Refresh left the
//     card's head for it.
//
// Fixtures only; no network. Fake values are built at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

const ID = (n: number) => `repo_${String(n).padStart(16, '0')}`
const INSTALL_URL = 'https://github.com/apps/swarmcloud/installations/new'

const CONNECTION = {
  connection_id: 'github:eng:abc',
  forge: 'github',
  method: 'app_user',
  forge_login: 'octo-dev',
  forge_user_id: 42,
  token_id: 'tok_u',
  secret_name: 'swarm-tenant-eng-git-u-' + '0'.repeat(16),
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

const ORG = (owner: string, over: Record<string, unknown> = {}) => ({
  owner,
  owner_type: owner === 'octo-dev' ? 'User' : 'Organization',
  installation_id: 7,
  repository_selection: owner === 'octo-dev' ? 'all' : 'selected',
  install_state: 'installed',
  sso: 'ok',
  enabled: true,
  enabled_at: null,
  checked_at: null,
  install_url: null,
  ...over,
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

const OWNERS = {
  tenant_id: 'eng',
  orgs_listed: true,
  install_url: INSTALL_URL,
  owners: [
    ORG('octo-dev'),
    ORG('example-org'),
    ORG('example-labs', { installation_id: null, repository_selection: null, install_state: 'not_installed', enabled: false, install_url: INSTALL_URL }),
  ],
}

const REPO = (n: number, name: string) => ({
  repository: `example-org/${name}`,
  owner: 'example-org',
  repo: name,
  repo_id: ID(n),
  private: true,
  archived: false,
  default_branch: 'main',
  can_push: true,
  permission: 'write',
  chosen: GRANTS.find((g) => g.repo_id === ID(n))?.mode ?? null,
})

const PAGE1 = {
  tenant_id: 'eng',
  owner: 'example-org',
  repositories: [REPO(1, 'example-docs'), REPO(4, 'example-service-ui')],
  page: 1,
  per_page: 100,
  max_pages: 10,
  next_page: null,
  capped: false,
  q: null,
  total_count: 2,
}

type Handler = (m: string, url: string, body: unknown) => { status: number; body: unknown } | null

function api(over: Record<string, unknown> = {}, extra: Handler = () => null) {
  return serve((m, url, body) => {
    const hit = extra(m, url, body)
    if (hit !== null) return hit
    if (m === 'GET' && url === '/v1/access') return { status: 200, body: { tenant_id: 'eng', connection: CONNECTION, orgs: [ORG('octo-dev'), ORG('example-org')], grants: GRANTS, ...over } }
    if (m === 'GET' && url === '/v1/access/orgs') return { status: 200, body: OWNERS }
    if (m === 'GET' && url === '/v1/access/orgs/example-org/repositories?page=1') return { status: 200, body: PAGE1 }
    if (m === 'GET' && url === '/v1/access/orgs/octo-dev/repositories?page=1') return { status: 200, body: { ...PAGE1, owner: 'octo-dev', repositories: [], total_count: 0 } }
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

/** A card by its title: Card draws a section headed by an h2. */
async function cardTitled(title: string): Promise<HTMLElement> {
  return (await screen.findByRole('heading', { name: title, level: 2 })).closest('section') as HTMLElement
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

describe('S2a: not connected, What comes after names steps 2 to 4', () => {
  it('draws the dashed card with each step and what it waits for', async () => {
    api({ connection: null, orgs: [], grants: [] })
    await mount()
    // MUTATION: drop <WhatComesAfter> from Mine.
    const card = await cardTitled('What comes after')
    expect(card.className).toContain('is-dash')
    const steps = within(card).getAllByRole('listitem').map((li) => visible(li))
    expect(steps).toEqual([
      '2. Choose repositories Waits for GitHub: there is nothing to list until you connect and install the App.',
      '3. Set secrets Waits for repositories: what is needed depends on what you register.',
      '4. Invite people Any time after GitHub.',
    ])
  })

  it('is not drawn once GitHub is connected', async () => {
    api()
    await mount()
    await screen.findByRole('button', { name: 'Disconnect GitHub' })
    // MUTATION: draw What comes after whatever the connection.
    expect(screen.queryByRole('heading', { name: 'What comes after' })).toBeNull()
  })
})

describe('S2b: connected, Where the App is installed is on the Connection tab', () => {
  it('draws Owner / Install / Chosen for every owner the connection reaches', async () => {
    api()
    await mount()
    // MUTATION: drop <Installs> from Mine (the owners were only on Choose repositories).
    const card = await cardTitled('Where the App is installed')
    const table = await within(card).findByRole('table', { name: 'Where the App is installed' })
    expect(within(table).getAllByRole('columnheader').map((h) => visible(h))).toEqual(['Owner', 'Installed', 'Chosen', 'Action'])
    const row = (owner: string) => table.querySelector(`tr[data-owner="${owner}"]`) as HTMLElement
    expect(visible(row('octo-dev'))).toContain('your account')
    expect(visible(row('octo-dev'))).toContain('all repositories')
    expect(visible(row('example-org'))).toContain('selected repositories')
    // MUTATION: count every grant under every owner.
    expect(visible(row('example-org').querySelector('[data-label="Chosen"]'))).toBe('2')
    expect(visible(row('octo-dev').querySelector('[data-label="Chosen"]'))).toBe('0')
    expect(visible(row('example-labs'))).toContain('not installed')
    expect(visible(row('example-labs'))).toContain('nothing to list until it is')
    // The head offers installing on an organisation, at the App's install page.
    const install = within(card).getByRole('link', { name: 'Install on another organisation…' })
    expect(install.getAttribute('href')).toBe(INSTALL_URL)
  })

  it('offers Request install for an owner without the App, which opens GitHub and records the request', async () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)
    const calls = api({}, (m, url) =>
      m === 'POST' && url === '/v1/access/orgs/example-labs/install-request'
        ? { status: 200, body: { tenant_id: 'eng', owner: 'example-labs', recorded: true, install_state: 'requested' } }
        : null,
    )
    await mount()
    const card = await cardTitled('Where the App is installed')
    const labs = (await within(card).findByText('example-labs')).closest('tr') as HTMLElement
    // MUTATION: offer no Request install for an organisation without the App.
    fireEvent.click(within(labs).getByRole('button', { name: 'Request install' }))
    expect(open).toHaveBeenCalledWith(INSTALL_URL, '_blank', 'noopener,noreferrer')
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST').map((c) => c.url)).toEqual(['/v1/access/orgs/example-labs/install-request']))
    // An owner with the App installed offers no Request install.
    const org = within(card).getByText('example-org').closest('tr') as HTMLElement
    expect(within(org).queryByRole('button', { name: 'Request install' })).toBeNull()
  })

  it("an installed owner's Choose opens Choose repositories on that owner", async () => {
    api()
    await mount()
    const card = await cardTitled('Where the App is installed')
    const org = (await within(card).findByText('example-org')).closest('tr') as HTMLElement
    fireEvent.click(within(org).getByRole('button', { name: 'Choose' }))
    expect(screen.getByRole('tab', { name: 'Choose repositories' }).getAttribute('aria-selected')).toBe('true')
    // MUTATION: ignore the owner Choose names (the chooser opens on octo-dev, the first enabled).
    expect(await screen.findByRole('list', { name: "example-org's repositories, page 1" })).toBeTruthy()
  })
})

describe('S2c: the foot bar of Choose repositories', () => {
  it('counts the grants changed and when, and carries the Refresh that left the card head', async () => {
    // example-org is the one enabled owner, so the chooser opens on it.
    api({ orgs: [ORG('example-org')] }, (m, url, body) => {
      if (m === 'PUT' && url === `/v1/access/grants/${ID(4)}`) {
        const b = body as { repository: string; mode: 'read' | 'write' }
        return { status: 200, body: { tenant_id: 'eng', grant: GRANT(4, b.repository, b.mode), registered: true, registration_repo_id: ID(4) } }
      }
      return null
    })
    await mount('/config/github?view=choose')
    const choose = await cardTitled('Choose repositories')
    const foot = await screen.findByRole('group', { name: 'Changes on this page' })
    // MUTATION: leave Refresh in the Choose card's head.
    expect(within(choose.querySelector('.c-card-h') as HTMLElement).queryByRole('button', { name: 'Refresh' })).toBeNull()
    expect(within(foot).getByRole('button', { name: 'Refresh' })).toBeTruthy()
    // The count is the bar's live line, so a saved change is announced.
    const line = foot.querySelector('[aria-live="polite"]')
    expect(line).not.toBeNull()
    expect(visible(line)).toBe('No changes saved yet · choices save as you make them')

    await screen.findByRole('list', { name: "example-org's repositories, page 1" })
    const row = document.querySelector('[data-repo="example-org/example-service-ui"]') as HTMLElement
    fireEvent.click(within(row).getByRole('radio', { name: 'Read' }))
    // MUTATION: never count a grant change into the foot bar.
    await waitFor(() => expect(visible(foot)).toContain('1 change saved · just now'))
    fireEvent.click(within(row).getByRole('radio', { name: 'Write' }))
    await waitFor(() => expect(visible(foot)).toContain('2 changes saved · just now'))
  })

  it('Refresh in the foot bar reads the owners and the overview again', async () => {
    const calls = api({ orgs: [ORG('example-org')] })
    await mount('/config/github?view=choose')
    const foot = await screen.findByRole('group', { name: 'Changes on this page' })
    await screen.findByRole('list', { name: "example-org's repositories, page 1" })
    const before = calls.filter((c) => c.url === '/v1/access/orgs').length
    fireEvent.click(within(foot).getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(calls.filter((c) => c.url === '/v1/access/orgs').length).toBe(before + 1))
  })
})
