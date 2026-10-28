// WORK › ACCESS: what takes access away asks first, and the asking is usable
// from a keyboard (live QA of /access, 2026-10-11: RSA-1, RSA-27, RSA-28,
// RSA-29).
//
// WHAT EACH CASE HOLDS:
//   * Remove on a grant row, a grant moved from Write to Read, and a chooser
//     row moved down (Read to Not chosen, Write to Read) write NOTHING until
//     the person says yes; Keep, or Esc, leaves the grant as it was;
//   * a move up (Not chosen to Read) is a grant, not a loss, and still lands
//     on one click;
//   * every ask-first banner takes focus to its safe button, and Esc is that
//     button: Disconnect sends nothing after Esc;
//   * Chosen lists every grant under the owner, including one GitHub put on
//     page 2, and drops the pager that pages GitHub's list;
//   * The tab is in the address, so a reload keeps Choose repositories.
//
// Fixtures only; no network. Nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

const ID = (n: number) => `repo_${String(n).padStart(16, '0')}`

const CONNECTION = {
  connection_id: 'github:eng:abc',
  forge: 'github',
  method: 'app_user',
  forge_login: 'octo-dev',
  forge_user_id: 42,
  token_id: 'tok_u',
  secret_name: null,
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
  method: 'app',
  owner_type: 'Organization',
  installation_id: 7,
  repository_selection: 'selected',
  install_state: 'installed',
  sso: 'ok',
  enabled: true,
  enabled_at: null,
  requested_at: null,
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
  checks: {},
  verified_at: null,
})

// example-zeta is granted but GitHub lists it on page 2.
const GRANTS = [GRANT(1, 'example-org/example-docs', 'read'), GRANT(2, 'example-org/example-service', 'write'), GRANT(6, 'example-org/example-zeta', 'read')]

const REPO = (n: number, name: string, over: Record<string, unknown> = {}) => ({
  repository: `example-org/${name}`,
  owner: 'example-org',
  repo: name,
  repo_id: ID(n),
  visibility: 'private',
  archived: false,
  default_branch: 'main',
  can_push: true,
  registered: false,
  granted: false,
  mode: null,
  ...over,
})

const PAGE1 = {
  tenant_id: 'eng',
  owner: 'example-org',
  repositories: [
    REPO(1, 'example-docs', { granted: true, mode: 'read' }),
    REPO(2, 'example-service', { granted: true, mode: 'write' }),
    REPO(4, 'example-service-ui'),
  ],
  page: 1,
  per_page: 50,
  max_pages: 10,
  next_page: 2,
  capped: false,
  q: null,
  total_count: 101,
}

type Handler = (m: string, url: string, body: unknown) => { status: number; body: unknown } | null

function api(extra: Handler = () => null) {
  return serve((m, url, body) => {
    const hit = extra(m, url, body)
    if (hit !== null) return hit
    if (m === 'GET' && url === '/v1/access') return { status: 200, body: { tenant_id: 'eng', connection: CONNECTION, orgs: [ORG('example-org')], grants: GRANTS } }
    if (m === 'GET' && url === '/v1/access/orgs') return { status: 200, body: { tenant_id: 'eng', orgs_listed: true, install_url: null, owners: [{ ...ORG('example-org'), install_url: null }] } }
    if (m === 'GET' && url === '/v1/access/orgs/example-org/repositories?page=1') return { status: 200, body: PAGE1 }
    if (m === 'GET' && url === '/v1/access/members') return { status: 200, body: { tenant_id: 'eng', members: [] } }
    if (m === 'DELETE' && url.startsWith('/v1/access/grants/')) return { status: 200, body: { tenant_id: 'eng', repo_id: url.split('/').pop(), revoked: true, unregistered: false } }
    if (m === 'PUT' && url.startsWith('/v1/access/grants/')) {
      const b = body as { repository: string; mode: 'read' | 'write' }
      return { status: 200, body: { tenant_id: 'eng', grant: GRANT(9, b.repository, b.mode), registered: false, registration_repo_id: ID(9) } }
    }
    return null
  })
}

/**
 * GitHub access (its Connection tab, or Choose repositories at `?view=choose`)
 * beside "Repositories SwarmCloud may reach as you", which moved out of it to
 * ReachableRepositories.tsx for Config › Repositories (GITHUB-ACCESS-PAGE,
 * part of #2157). The cases here act on both, as one person would.
 */
async function mount(at: 'mine' | 'choose' = 'mine') {
  if (at === 'choose') window.history.replaceState(null, '', '/config/github?view=choose')
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { AccessScreen } = await import('../Access')
  const { ReachableRepositoriesRead } = await import('../ReachableRepositories')
  return render(
    <>
      <AccessScreen />
      <ReachableRepositoriesRead />
    </>,
  )
}

const writes = <C extends { method: string }>(calls: C[]) => calls.filter((c) => c.method !== 'GET')
const row = (repository: string) => document.querySelector(`li.ac-row[data-repo="${repository}"]`) as HTMLElement
const grantRow = (repository: string) => document.querySelector(`li.ac-grant[data-repo="${repository}"]`) as HTMLElement
const radio = (el: HTMLElement, name: string) => within(el).getByRole('radio', { name }) as HTMLButtonElement
const settle = () => new Promise((r) => setTimeout(r, 20))

afterEach(() => {
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', window.location.pathname)
})

describe('a grant is never removed or narrowed on one click (RSA-1)', () => {
  it('Remove on a grant row writes nothing until Remove access; Keep and Esc leave it', async () => {
    const calls = api()
    await mount()
    await screen.findByRole('list', { name: 'Your granted repositories, failures first' })
    const g = () => grantRow('example-org/example-service')

    // The row's trigger is `Remove access` too (QA1 config-15); while the ask
    // is open it steps aside, so the one `Remove access` is the ask's act.
    fireEvent.click(within(g()).getByRole('button', { name: 'Remove access' }))
    await settle()
    expect(writes(calls)).toEqual([])
    fireEvent.click(within(g()).getByRole('button', { name: 'Keep Write' }))
    expect(within(g()).queryByRole('button', { name: 'Keep Write' })).toBeNull()

    fireEvent.click(within(g()).getByRole('button', { name: 'Remove access' }))
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
    expect(within(g()).queryByRole('button', { name: 'Keep Write' })).toBeNull()
    await settle()
    expect(writes(calls)).toEqual([])

    fireEvent.click(within(g()).getByRole('button', { name: 'Remove access' }))
    expect(within(g()).getByRole('button', { name: 'Keep Write' })).toBeTruthy()
    fireEvent.click(within(g()).getByRole('button', { name: 'Remove access' }))
    await waitFor(() => expect(writes(calls).map((c) => `${c.method} ${c.url}`)).toEqual([`DELETE /v1/access/grants/${ID(2)}`]))
  })

  it('a grant moved from Write to Read writes nothing until Change to Read', async () => {
    const calls = api()
    await mount()
    await screen.findByRole('list', { name: 'Your granted repositories, failures first' })
    fireEvent.click(radio(grantRow('example-org/example-service'), 'Read'))
    await settle()
    expect(writes(calls)).toEqual([])
    fireEvent.click(within(grantRow('example-org/example-service')).getByRole('button', { name: 'Keep Write' }))
    await settle()
    expect(writes(calls)).toEqual([])
  })

  it('the chooser asks before Read to Not chosen and Write to Read, and not before a new grant', async () => {
    const calls = api()
    await mount('choose')
    await screen.findByRole('list', { name: "example-org's repositories, page 1" })

    fireEvent.click(radio(row('example-org/example-docs'), 'Not chosen'))
    expect(visible(row('example-org/example-docs'))).toContain('Remove example-org/example-docs?')
    fireEvent.click(within(row('example-org/example-docs')).getByRole('button', { name: 'Keep Read' }))
    fireEvent.click(radio(row('example-org/example-service'), 'Read'))
    expect(visible(row('example-org/example-service'))).toContain('Make example-org/example-service read only?')
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
    await settle()
    expect(writes(calls)).toEqual([])
    expect(radio(row('example-org/example-service'), 'Write').getAttribute('aria-checked')).toBe('true')

    fireEvent.click(radio(row('example-org/example-service-ui'), 'Read'))
    await waitFor(() => expect(writes(calls).map((c) => c.method)).toEqual(['PUT']))
  })
})

describe('an ask-first banner is reachable from the keyboard (RSA-27)', () => {
  it('takes focus to the safe button, and Esc is Keep: Disconnect sends nothing', async () => {
    const calls = api()
    await mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect GitHub' }))
    const keep = screen.getByRole('button', { name: 'Keep connected' })
    expect(document.activeElement).toBe(keep)
    fireEvent.keyDown(keep, { key: 'Escape' })
    expect(screen.queryByRole('button', { name: 'Keep connected' })).toBeNull()
    await settle()
    expect(writes(calls)).toEqual([])
  })

  it('focuses Keep Write when a grant Remove asks', async () => {
    api()
    await mount()
    await screen.findByRole('list', { name: 'Your granted repositories, failures first' })
    fireEvent.click(within(grantRow('example-org/example-service')).getByRole('button', { name: 'Remove access' }))
    expect((document.activeElement as HTMLElement).textContent).toBe('Keep Write')
  })
})

describe('Chosen is every grant under the owner (RSA-29)', () => {
  it('lists a grant GitHub put on page 2, and drops the page pager', async () => {
    api()
    await mount('choose')
    await screen.findByRole('list', { name: "example-org's repositories, page 1" })
    expect(row('example-org/example-zeta')).toBeNull()
    fireEvent.click(screen.getByRole('radio', { name: 'Chosen' }))
    const shown = [...document.querySelectorAll('li.ac-row')].map((li) => (li as HTMLElement).dataset.repo)
    expect(shown).toEqual(['example-org/example-docs', 'example-org/example-service', 'example-org/example-zeta'])
    expect(radio(row('example-org/example-zeta'), 'Read').getAttribute('aria-checked')).toBe('true')
    expect(screen.queryByRole('button', { name: 'Next page' })).toBeNull()
  })
})

describe('the tab is in the address (RSA-28)', () => {
  // Members was the switch's second view until GA-MOVE-VIEWS moved it to
  // People and roles (ga.moveviews.test.tsx); the address now carries the tab.
  it('writes ?view=choose, and a mount on it opens Choose repositories', async () => {
    api()
    const first = await mount()
    fireEvent.click(screen.getByRole('tab', { name: 'Choose repositories' }))
    expect(new URLSearchParams(window.location.search).get('view')).toBe('choose')
    first.unmount()
    await mount()
    expect(screen.getByRole('tab', { name: 'Choose repositories' }).getAttribute('aria-selected')).toBe('true')
    fireEvent.click(screen.getByRole('tab', { name: 'Connection' }))
    expect(window.location.search).toBe('')
  })
})
