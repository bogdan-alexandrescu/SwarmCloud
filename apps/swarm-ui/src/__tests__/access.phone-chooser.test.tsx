// CHOOSE REPOSITORIES ON A PHONE (GA-PHONE-CHOOSER; part of #2157, BGD's
// decision of 2026-10-24, mock-up S2f).
//
// WHAT EACH CASE HOLDS:
//   * under 560px the owner column is a select -- each owner's name with the
//     count or install state its card shows -- and only the selected owner's
//     card and repositories are drawn below it;
//   * picking an owner not enabled yet shows its card (where Install and
//     Enable are) and no other owner's repositories under it;
//   * the pick survives a reload, in this tab's sessionStorage;
//   * the deep link `/config/github?view=choose` still opens the chooser;
//   * desktop is unchanged: every owner card, no select, the first enabled
//     owner open whatever a phone stored.
//
// Fixtures only; no network. Fake values are built at runtime.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { serve } from './repofixture'

const ID = (n: number) => `repo_${String(n).padStart(16, '0')}`
const KEY = 'swarm.access.owner'

const CONNECTION = {
  connection_id: 'github:eng:abc',
  forge: 'github',
  method: 'app_user',
  forge_login: 'octo-dev',
  forge_user_id: 42,
  token_id: 'tok_u',
  secret_name: `swarm-tenant-eng-git-u-${'0'.repeat(16)}`,
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

const OWNER = (owner: string, over: Record<string, unknown> = {}) => ({
  owner,
  owner_type: owner === 'octo-dev' ? 'User' : 'Organization',
  installation_id: 7,
  repository_selection: 'selected',
  install_state: 'installed',
  sso: 'ok',
  enabled: true,
  enabled_at: null,
  checked_at: null,
  install_url: null,
  ...over,
})

const GRANT = (n: number, repository: string) => ({
  repo_id: ID(n),
  repository,
  owner: repository.split('/')[0],
  mode: 'read',
  can_push: true,
  archived: false,
  granted_at: null,
  granted_by: 'dev@swarm.example.com',
  checks: { clone: { state: 'ok', code: null, checked_at: null } },
  verified_at: null,
})

const REPO = (n: number, owner: string, name: string) => ({
  repository: `${owner}/${name}`,
  owner,
  repo: name,
  repo_id: ID(n),
  visibility: 'private',
  archived: false,
  default_branch: 'main',
  can_push: true,
  registered: false,
  granted: false,
  mode: null,
})

const page = (owner: string, repositories: unknown[]) => ({
  tenant_id: 'eng', owner, repositories, page: 1, per_page: 100, max_pages: 10, next_page: null, capped: false, q: null,
  total_count: repositories.length,
})

// example-org and second-org enabled; octo-dev installed, not enabled; far-org not installed.
const OWNERS = [
  OWNER('example-org'),
  OWNER('second-org'),
  OWNER('octo-dev', { enabled: false }),
  OWNER('far-org', { enabled: false, install_state: 'not_installed', installation_id: null, install_url: 'https://github.com/apps/swarmcloud/installations/new' }),
]

function api(owners = OWNERS) {
  const enabled = owners.filter((o) => o.enabled)
  return serve((m, url) => {
    if (m === 'GET' && url === '/v1/access') {
      return {
        status: 200,
        body: {
          tenant_id: 'eng', connection: CONNECTION, orgs: enabled,
          grants: [GRANT(1, 'example-org/example-docs'), GRANT(2, 'example-org/example-service')],
        },
      }
    }
    if (m === 'GET' && url === '/v1/access/orgs') return { status: 200, body: { tenant_id: 'eng', orgs_listed: true, install_url: null, owners } }
    if (m === 'GET' && url === '/v1/access/orgs/example-org/repositories?page=1') return { status: 200, body: page('example-org', [REPO(1, 'example-org', 'example-docs')]) }
    if (m === 'GET' && url === '/v1/access/orgs/second-org/repositories?page=1') return { status: 200, body: page('second-org', [REPO(3, 'second-org', 'second-api')]) }
    return null
  })
}

/** Stub `matchMedia`, which jsdom does not have: `phone` matches the 560px query. */
function media(phone: boolean): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: phone && query.includes('560'),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }))
}

async function mount(phone: boolean, path = '/config/github?view=choose') {
  media(phone)
  window.history.replaceState(null, '', path)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { AccessScreen } = await import('../Access')
  return render(<AccessScreen />)
}

const ownerSelect = () => screen.findByRole('combobox', { name: /^Owner/ }) as Promise<HTMLSelectElement>
const cards = () => [...document.querySelectorAll('#ac-choose li.ac-owner')].map((li) => li.getAttribute('data-owner'))

beforeEach(() => window.sessionStorage.clear())

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  window.sessionStorage.clear()
  window.history.replaceState(null, '', '/')
})

describe('on a phone the owner column is a select', () => {
  it('lists every owner with its count or state, and draws only the selected owner and its repositories', async () => {
    api()
    await mount(true)
    // MUTATION: drop the `phone` select from Owners (the stack of cards comes back).
    const select = await ownerSelect()
    expect([...select.options].map((o) => o.textContent)).toEqual([
      'example-org · 2 chosen',
      'second-org · 0 chosen',
      'octo-dev · installed, not enabled',
      'far-org · not installed',
    ])
    expect(select.value).toBe('example-org')
    // MUTATION: drop the count from the select's label.
    expect(screen.getByText('Owner · one of 4 your connection reaches')).toBeTruthy()
    // MUTATION: draw every owner card at phone width (`drawn = owners`).
    expect(cards()).toEqual(['example-org'])
    expect(await screen.findByText('example-org/example-docs')).toBeTruthy()

    fireEvent.change(select, { target: { value: 'second-org' } })
    expect(cards()).toEqual(['second-org'])
    expect(await screen.findByText('second-org/second-api')).toBeTruthy()
    expect(screen.queryByText('example-org/example-docs')).toBeNull()
  })

  it('shows an owner not enabled yet with its own actions, and no other owner’s repositories under it', async () => {
    api()
    await mount(true)
    const select = await ownerSelect()
    await screen.findByText('example-org/example-docs')
    // MUTATION: offer only enabled owners in the select (Install and Enable become unreachable).
    fireEvent.change(select, { target: { value: 'octo-dev' } })
    expect(cards()).toEqual(['octo-dev'])
    const card = document.querySelector('li.ac-owner[data-owner="octo-dev"]') as HTMLElement
    expect(within(card).getByRole('button', { name: 'Enable' })).toBeTruthy()
    // MUTATION: list the first enabled owner's repositories when the selected one is not enabled.
    expect(screen.queryByText('example-org/example-docs')).toBeNull()
    // MUTATION: say "Enable it above" for every owner (a requested owner has no Enable).
    expect(screen.getByText('octo-dev is not enabled')).toBeTruthy()
    expect(screen.queryByText(/Enable it above/)).toBeNull()

    fireEvent.change(select, { target: { value: 'far-org' } })
    const far = document.querySelector('li.ac-owner[data-owner="far-org"]') as HTMLElement
    // An organisation not installed is asked for, as on the Connection tab (QA1-FIX-01 config-04).
    expect(within(far).getByRole('button', { name: 'Request install' })).toBeTruthy()
    expect(within(far).queryByRole('link', { name: 'Install on far-org' })).toBeNull()
  })

  it('keeps the selected owner across a reload, in this tab’s session', async () => {
    api()
    const first = await mount(true)
    fireEvent.change(await ownerSelect(), { target: { value: 'second-org' } })
    // MUTATION: drop storeOwner from the phone pick.
    expect(window.sessionStorage.getItem(KEY)).toBe('second-org')
    first.unmount()

    api()
    await mount(true)
    // MUTATION: start `picked` at null on a phone instead of the stored owner.
    expect((await ownerSelect()).value).toBe('second-org')
    expect(await screen.findByText('second-org/second-api')).toBeTruthy()
  })

  it('falls back to the first enabled owner when the stored one is no longer reached', async () => {
    window.sessionStorage.setItem(KEY, 'gone-org')
    api()
    await mount(true)
    expect((await ownerSelect()).value).toBe('example-org')
    expect(cards()).toEqual(['example-org'])
  })

  it('still opens from the deep link, with one owner as with several', async () => {
    api([OWNER('example-org')])
    await mount(true, '/config/github?view=choose')
    expect(screen.getByRole('tab', { name: 'Choose repositories' }).getAttribute('aria-selected')).toBe('true')
    const select = await ownerSelect()
    expect([...select.options].map((o) => o.textContent)).toEqual(['example-org · 2 chosen'])
    expect(screen.getByText('Owner · the one your connection reaches')).toBeTruthy()
    expect(await screen.findByText('example-org/example-docs')).toBeTruthy()
  })
})

describe('on a desktop the chooser is as it was', () => {
  it('draws every owner card and no select, and ignores an owner a phone stored', async () => {
    window.sessionStorage.setItem(KEY, 'octo-dev')
    api()
    await mount(false)
    expect(await screen.findByText('example-org/example-docs')).toBeTruthy()
    // MUTATION: render the select whatever the width.
    expect(screen.queryByRole('combobox', { name: /^Owner/ })).toBeNull()
    expect(cards()).toEqual(['example-org', 'second-org', 'octo-dev', 'far-org'])
    // MUTATION: read the stored owner at desktop width too.
    expect(document.querySelector('li.ac-owner.is-selected')?.getAttribute('data-owner')).toBe('example-org')
    fireEvent.click(screen.getByRole('button', { name: 'second-org' }))
    expect(await screen.findByText('second-org/second-api')).toBeTruthy()
    // MUTATION: write the pick to the session at desktop width.
    expect(window.sessionStorage.getItem(KEY)).toBe('octo-dev')
  })
})
