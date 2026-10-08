// WORK › ACCESS (#780, OB8; docs/onboarding.md §2.4, owner picks D6, D8, D9,
// D10 Chooser A / Verify A / Access A and B).
//
// WHAT EACH CASE HOLDS:
//   * the page states the owner's rules: Read enforced by SwarmCloud (D9), no
//     workflows write (D8), verification only reads (D6);
//   * owners: an enabled owner is chosen from, an installed one is enabled
//     with POST /v1/access/orgs, an uninstalled one links to the App's install
//     page, and removing one is confirmed and DELETEs it;
//   * the chooser reads ONE page, pages with next_page, searches server-side,
//     shows push ability before Write and refuses Write where GitHub would;
//     Read/Write PUT a grant, Not chosen DELETEs it, a refusal prints the
//     server's §2.3 copy;
//   * a typed owner/repo is PUT to the id `repositories.repo_id_for` mints,
//     and its REPO_NOT_INSTALLED copy is drawn with the GitHub page;
//   * the grants are failures first, Verify POSTs and prints each failure's
//     copy inline, the write test is said to be not offered;
//   * not connected: Connect GitHub, and no GitHub-reading route is called;
//   * connected but installed nowhere (#780, 2026-10-08): the notice says so
//     with Install on <login> and Install on an organisation…; mixed owners
//     get an Install button per uninstalled row; Refresh and returning to the
//     tab read the owners again;
//   * Members (admin) lists states and names; a non-admin is told so;
//   * nothing token-shaped reaches the DOM.
//
// Fixtures only; no network. Fake values are built at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { leakedValue, serve, tokenShapedIn, visible } from './repofixture'

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

const GRANT = (n: number, repository: string, mode: 'read' | 'write', checks: Record<string, string> = {}) => ({
  repo_id: ID(n),
  repository,
  owner: repository.split('/')[0],
  mode,
  can_push: true,
  archived: false,
  granted_at: null,
  granted_by: 'dev@swarm.example.com',
  checks: Object.fromEntries(Object.entries(checks).map(([k, state]) => [k, { state, code: state === 'missing' ? 'PERMISSION_MISSING' : null, checked_at: null }])),
  verified_at: null,
})

const GRANTS = [
  GRANT(1, 'example-org/example-docs', 'read', { clone: 'ok', push: 'not_required', pull_request: 'not_required' }),
  GRANT(2, 'example-org/example-service', 'write', { clone: 'ok', push: 'missing', pull_request: 'unknown' }),
  GRANT(3, 'octo-dev/example-api', 'write', { clone: 'ok', push: 'ok', pull_request: 'ok' }),
]

function overview(over: Record<string, unknown> = {}) {
  return { tenant_id: 'eng', connection: CONNECTION, orgs: [ORG('example-org'), ORG('octo-dev')], grants: GRANTS, ...over }
}

const OWNERS = {
  tenant_id: 'eng',
  orgs_listed: true,
  install_url: 'https://github.com/apps/swarmcloud/installations/new',
  owners: [
    { ...ORG('octo-dev'), install_url: null },
    { ...ORG('example-org'), install_url: null },
    { ...ORG('example-lab'), enabled: false, install_url: null },
    {
      ...ORG('example-new'),
      installation_id: null,
      repository_selection: null,
      install_state: 'not_installed',
      enabled: false,
      install_url: 'https://github.com/apps/swarmcloud/installations/new',
    },
  ],
}

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

function page(n: number, repos: unknown[], over: Record<string, unknown> = {}) {
  return { tenant_id: 'eng', owner: 'example-org', repositories: repos, page: n, per_page: 100, max_pages: 10, next_page: null, capped: false, q: null, total_count: null, ...over }
}

const PAGE1 = page(
  1,
  [
    REPO(1, 'example-docs', { granted: true, mode: 'read', visibility: 'public' }),
    REPO(4, 'example-service-ui'),
    REPO(5, 'example-tools', { can_push: false }),
  ],
  { next_page: 2, total_count: 140 },
)
const PAGE2 = page(2, [REPO(6, 'example-zeta')], { total_count: 140 })

type Handler = (m: string, url: string, body: unknown) => { status: number; body: unknown } | null

/** The standard API for a connected person, with `extra` consulted first. */
function api(extra: Handler = () => null) {
  return serve((m, url, body) => {
    const hit = extra(m, url, body)
    if (hit !== null) return hit
    if (m === 'GET' && url === '/v1/access') return { status: 200, body: overview() }
    if (m === 'GET' && url === '/v1/access/orgs') return { status: 200, body: OWNERS }
    if (m === 'GET' && url === '/v1/access/orgs/example-org/repositories?page=1') return { status: 200, body: PAGE1 }
    if (m === 'GET' && url === '/v1/access/orgs/example-org/repositories?page=2') return { status: 200, body: PAGE2 }
    return null
  })
}

async function mount() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { AccessScreen } = await import('../Access')
  return render(<AccessScreen />)
}

const row = (repository: string) => document.querySelector(`li.ac-row[data-repo="${repository}"]`) as HTMLElement
const grantRow = (repository: string) => document.querySelector(`li.ac-grant[data-repo="${repository}"]`) as HTMLElement
const owner = (name: string) => document.querySelector(`li.ac-owner[data-owner="${name}"]`) as HTMLElement
const radio = (el: HTMLElement, name: string) => within(el).getByRole('radio', { name }) as HTMLButtonElement

afterEach(() => vi.unstubAllEnvs())

describe('the page states what SwarmCloud may do as you', () => {
  it('says Read is enforced by SwarmCloud, there is no workflows write, and verifying only reads', async () => {
    api()
    await mount()
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Access')
    const d9 = visible(document.querySelector('[data-rule="D9"]'))
    expect(d9).toContain('SwarmCloud enforces it')
    expect(d9).toContain('GitHub itself would let the push through')
    const d8 = visible(document.querySelector('[data-rule="D8"]'))
    expect(d8).toContain('does not ask for workflows write')
    expect(d8).toContain('.github/workflows/')
    expect(visible(document.querySelector('[data-rule="D6"]'))).toContain('Nothing is written to GitHub')
  })
})

describe('owners (chooser A, left)', () => {
  it('lists every owner with its state and the action it needs', async () => {
    api()
    await mount()
    await screen.findByRole('list', { name: 'Owners your connection reaches' })
    expect(visible(owner('octo-dev'))).toContain('your account')
    expect(visible(owner('example-org'))).toContain('2 chosen')
    expect(within(owner('example-lab')).getByRole('button', { name: 'Enable' })).toBeTruthy()
    expect(within(owner('example-new')).getByRole('link', { name: 'Install on example-new' }).getAttribute('href')).toBe(
      'https://github.com/apps/swarmcloud/installations/new',
    )
    expect(visible(owner('example-new'))).toContain('not installed')
  })

  it('enables an installed owner with POST /v1/access/orgs', async () => {
    const calls = api((m, url) => (m === 'POST' && url === '/v1/access/orgs' ? { status: 200, body: { tenant_id: 'eng', org: ORG('example-lab') } } : null))
    await mount()
    await screen.findByRole('list', { name: 'Owners your connection reaches' })
    fireEvent.click(within(owner('example-lab')).getByRole('button', { name: 'Enable' }))
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST' && c.url === '/v1/access/orgs')).toEqual([expect.objectContaining({ body: { owner: 'example-lab' } })]))
  })

  it('removes an owner only after a confirm, and says what GitHub still allows', async () => {
    const calls = api((m, url) =>
      m === 'DELETE' && url === '/v1/access/orgs/example-org'
        ? {
            status: 200,
            body: { tenant_id: 'eng', owner: 'example-org', grants_deleted: 2, unregistered: [], installation_settings_url: 'https://github.com/settings/installations/7' },
          }
        : null,
    )
    await mount()
    await screen.findByRole('list', { name: 'Owners your connection reaches' })
    fireEvent.click(within(owner('example-org')).getByRole('button', { name: 'Remove' }))
    expect(calls.some((c) => c.method === 'DELETE')).toBe(false)
    expect(visible(owner('example-org'))).toContain('Deletes your 2 grants under example-org at once')
    fireEvent.click(within(owner('example-org')).getByRole('button', { name: 'Remove' }))
    expect(await screen.findByText('example-org was removed')).toBeTruthy()
    expect(calls.filter((c) => c.method === 'DELETE').map((c) => c.url)).toEqual(['/v1/access/orgs/example-org'])
    expect(screen.getByRole('link', { name: "The installation's settings" }).getAttribute('href')).toBe('https://github.com/settings/installations/7')
  })
})

describe('repositories (chooser A, right)', () => {
  it("reads the selected owner's first page and shows push ability before Write", async () => {
    api()
    await mount()
    await screen.findByRole('list', { name: "example-org's repositories, page 1" })
    expect(owner('example-org').className).toContain('is-selected')
    expect(radio(row('example-org/example-docs'), 'Read').getAttribute('aria-checked')).toBe('true')
    expect(radio(row('example-org/example-service-ui'), 'Not chosen').getAttribute('aria-checked')).toBe('true')
    // GitHub would refuse the push, so Write is not offered.
    const tools = row('example-org/example-tools')
    expect(visible(tools)).toContain('read only')
    expect(radio(tools, 'Write').disabled).toBe(true)
    expect(radio(tools, 'Write').title).toBe('GitHub does not let you push here')
    expect(radio(row('example-org/example-service-ui'), 'Write').disabled).toBe(false)
    expect(screen.getByText(/Page 1 of 2 · 140 repositories in the installation/)).toBeTruthy()
  })

  it('pages with next_page and searches server-side', async () => {
    const calls = api((m, url) => (m === 'GET' && url === '/v1/access/orgs/example-org/repositories?page=1&q=service' ? { status: 200, body: page(1, [REPO(4, 'example-service-ui')], { q: 'service' }) } : null))
    await mount()
    await screen.findByRole('list', { name: "example-org's repositories, page 1" })
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }))
    await screen.findByRole('list', { name: "example-org's repositories, page 2" })
    expect(row('example-org/example-zeta')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Next page' }) as HTMLButtonElement).disabled).toBe(true)

    fireEvent.change(screen.getByRole('searchbox', { name: "Search example-org's repositories" }), { target: { value: 'service' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))
    await waitFor(() => expect(calls.some((c) => c.url === '/v1/access/orgs/example-org/repositories?page=1&q=service')).toBe(true))
    expect(await screen.findByText(/matching “service”/)).toBeTruthy()
  })

  it('grants Read or Write with PUT, and Not chosen revokes with DELETE', async () => {
    const calls = api((m, url, body) => {
      if (m === 'PUT' && url === `/v1/access/grants/${ID(4)}`) {
        const b = body as { repository: string; mode: 'read' | 'write' }
        return { status: 200, body: { tenant_id: 'eng', grant: GRANT(4, b.repository, b.mode), registered: true, registration_repo_id: ID(4) } }
      }
      if (m === 'DELETE' && url === `/v1/access/grants/${ID(1)}`) return { status: 200, body: { tenant_id: 'eng', repo_id: ID(1), revoked: true, unregistered: false } }
      return null
    })
    await mount()
    await screen.findByRole('list', { name: "example-org's repositories, page 1" })
    fireEvent.click(radio(row('example-org/example-service-ui'), 'Write'))
    await waitFor(() =>
      expect(calls.filter((c) => c.method === 'PUT')).toEqual([
        expect.objectContaining({ url: `/v1/access/grants/${ID(4)}`, body: { repository: 'example-org/example-service-ui', mode: 'write' } }),
      ]),
    )
    fireEvent.click(radio(row('example-org/example-docs'), 'Not chosen'))
    await waitFor(() => expect(calls.filter((c) => c.method === 'DELETE').map((c) => c.url)).toEqual([`/v1/access/grants/${ID(1)}`]))
  })

  it("prints a refused grant's §2.3 copy word for word", async () => {
    const COPY = "You chose write for example-org/example-service-ui, but GitHub does not let octo-dev push there through SwarmCloud. Ask for write access to example-org/example-service-ui, or change the grant to read."
    api((m, url) =>
      m === 'PUT' && url === `/v1/access/grants/${ID(4)}`
        ? { status: 422, body: { code: 'access_refused', message: 'GitHub does not let octo-dev push to example-org/example-service-ui', detail: { failure_code: 'PERMISSION_MISSING', recovery: COPY, url: null } } }
        : null,
    )
    await mount()
    await screen.findByRole('list', { name: "example-org's repositories, page 1" })
    fireEvent.click(radio(row('example-org/example-service-ui'), 'Write'))
    expect(await screen.findByText(COPY)).toBeTruthy()
    expect(within(row('example-org/example-service-ui')).getByText(/PERMISSION_MISSING/)).toBeTruthy()
  })
})

describe('a typed owner/repo', () => {
  it("is PUT to the id repositories.repo_id_for mints, and its REPO_NOT_INSTALLED copy is drawn", async () => {
    // sha256("eng" + "github.com/" + "example-org/example-legacy")[:16], from the Python recipe.
    const LEGACY = 'repo_b0d776eb1931a71e'
    const COPY =
      'SwarmCloud is installed on example-org but not for example-org/example-legacy. Add example-org/example-legacy to the installation at https://github.com/settings/installations/7 (an org owner may have to), then press Re-check. SwarmCloud never reaches a repository the installation leaves out.'
    const calls = api((m, url) =>
      m === 'PUT' && url === `/v1/access/grants/${LEGACY}`
        ? {
            status: 403,
            body: {
              code: 'access_refused',
              message: 'GitHub answered HTTP 404 for example-org/example-legacy',
              detail: { failure_code: 'REPO_NOT_INSTALLED', recovery: COPY, url: 'https://github.com/settings/installations/7' },
            },
          }
        : null,
    )
    await mount()
    const input = await screen.findByRole('textbox', { name: 'Not listed? Type owner/repo' })
    fireEvent.change(input, { target: { value: 'Example-Org/example-legacy' } })
    fireEvent.click(screen.getByRole('button', { name: 'Grant' }))
    expect(await screen.findByText(COPY)).toBeTruthy()
    expect(calls.filter((c) => c.method === 'PUT')).toEqual([
      expect.objectContaining({ url: `/v1/access/grants/${LEGACY}`, body: { repository: 'Example-Org/example-legacy', mode: 'read' } }),
    ])
    const banner = screen.getByText(COPY).closest('.c-banner') as HTMLElement
    expect(within(banner).getByRole('link', { name: 'Open on GitHub' }).getAttribute('href')).toBe('https://github.com/settings/installations/7')
    expect(within(banner).getByRole('button', { name: 'Re-check' })).toBeTruthy()
  })

  it('refuses a value that is not owner/repo before calling anything', async () => {
    const calls = api()
    await mount()
    const input = await screen.findByRole('textbox', { name: 'Not listed? Type owner/repo' })
    fireEvent.change(input, { target: { value: 'just-a-name' } })
    expect((screen.getByRole('button', { name: 'Grant' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText(/Type it as owner\/repo/)).toBeTruthy()
    expect(calls.some((c) => c.method === 'PUT')).toBe(false)
  })
})

describe('the grants, verified (Verify A)', () => {
  it('lists failures first, a read grant needs no push, and the write test is not offered', async () => {
    api()
    await mount()
    const grid = await screen.findByRole('list', { name: 'Your granted repositories, failures first' })
    const order = [...grid.querySelectorAll('li.ac-grant')].map((li) => (li as HTMLElement).dataset.repo)
    expect(order).toEqual(['example-org/example-service', 'example-org/example-docs', 'octo-dev/example-api'])
    const svc = grantRow('example-org/example-service')
    expect(svc.querySelector('[data-check="push"] [data-cap]')?.getAttribute('data-cap')).toBe('missing')
    expect(svc.querySelector('[data-check="pull_request"] [data-cap]')?.getAttribute('data-cap')).toBe('unknown')
    const docs = grantRow('example-org/example-docs')
    expect(docs.querySelector('[data-check="push"] .c-dash')?.getAttribute('aria-label')).toBe('Push is not needed for a read grant')
    expect(svc.querySelector('[data-check="write_test"] .c-dash')?.getAttribute('aria-label')).toContain('not served by this API yet')
  })

  it("verifies one grant with POST and prints each failure's copy inline", async () => {
    const COPY = 'You chose write for example-org/example-service, but GitHub does not let octo-dev push there through SwarmCloud. Ask for write access to example-org/example-service, or change the grant to read.'
    const calls = api((m, url) =>
      m === 'POST' && url === `/v1/access/grants/${ID(2)}/verify`
        ? { status: 200, body: { tenant_id: 'eng', grant: GRANTS[1], passed: false, failures: [{ check: 'push', code: 'PERMISSION_MISSING', copy: COPY }] } }
        : null,
    )
    await mount()
    await screen.findByRole('list', { name: 'Your granted repositories, failures first' })
    fireEvent.click(within(grantRow('example-org/example-service')).getByRole('button', { name: 'Verify' }))
    expect(await screen.findByText(COPY)).toBeTruthy()
    expect(calls.filter((c) => c.method === 'POST' && c.url.endsWith('/verify'))).toEqual([expect.objectContaining({ url: `/v1/access/grants/${ID(2)}/verify`, body: {} })])
  })

  it('Re-check all verifies every grant', async () => {
    const calls = api((m, url) => {
      const hit = /^\/v1\/access\/grants\/(repo_\d+)\/verify$/.exec(url)
      if (m === 'POST' && hit !== null) return { status: 200, body: { tenant_id: 'eng', grant: GRANTS.find((g) => g.repo_id === hit[1]), passed: true, failures: [] } }
      return null
    })
    await mount()
    await screen.findByRole('list', { name: 'Your granted repositories, failures first' })
    fireEvent.click(screen.getByRole('button', { name: 'Re-check all' }))
    await waitFor(() => expect(calls.filter((c) => c.url.endsWith('/verify')).map((c) => c.url).sort()).toEqual([ID(1), ID(2), ID(3)].map((id) => `/v1/access/grants/${id}/verify`)))
  })

  it('changes a grant from Write to Read with PUT', async () => {
    const calls = api((m, url) => (m === 'PUT' && url === `/v1/access/grants/${ID(3)}` ? { status: 200, body: { tenant_id: 'eng', grant: GRANT(3, 'octo-dev/example-api', 'read'), registered: false, registration_repo_id: ID(3) } } : null))
    await mount()
    await screen.findByRole('list', { name: 'Your granted repositories, failures first' })
    fireEvent.click(radio(grantRow('octo-dev/example-api'), 'Read'))
    await waitFor(() => expect(calls.filter((c) => c.method === 'PUT')).toEqual([expect.objectContaining({ body: { repository: 'octo-dev/example-api', mode: 'read' } })]))
  })
})

describe('not connected', () => {
  it('offers Connect GitHub and reads nothing from GitHub', async () => {
    const calls = serve((m, url) => (m === 'GET' && url === '/v1/access' ? { status: 200, body: overview({ connection: null, orgs: [], grants: [] }) } : null))
    await mount()
    expect(await screen.findByRole('button', { name: 'Connect GitHub' })).toBeTruthy()
    expect(screen.queryByText('Choose repositories')).toBeNull()
    expect(calls.map((c) => c.url)).toEqual(['/v1/access'])
  })
})

const APP_INSTALL = 'https://github.com/apps/swarmcloud-saga/installations/new'

/** What GET /v1/access/orgs answered live on 2026-10-08: the account, installed nowhere. */
const NOWHERE = {
  tenant_id: 'eng',
  orgs_listed: true,
  install_url: APP_INSTALL,
  owners: [
    {
      owner: 'octo-dev',
      owner_type: 'User',
      installation_id: null,
      repository_selection: null,
      install_state: 'not_installed',
      sso: 'unknown',
      enabled: false,
      install_url: APP_INSTALL,
    },
  ],
}

const notice = () => document.querySelector('section.ac-install') as HTMLElement | null

describe('connected but not installed (#780)', () => {
  it('says plainly the App is installed nowhere, and offers Install on the account and on an organisation', async () => {
    serve((m, url) => {
      if (m === 'GET' && url === '/v1/access') return { status: 200, body: overview({ orgs: [], grants: [] }) }
      if (m === 'GET' && url === '/v1/access/orgs') return { status: 200, body: NOWHERE }
      return null
    })
    await mount()
    await screen.findByRole('list', { name: 'Owners your connection reaches' })
    const box = notice()
    expect(box).not.toBeNull()
    expect(visible(box)).toContain(
      "You're connected as @octo-dev, but SwarmCloud Saga isn't installed anywhere yet. Install it on the accounts and repositories you want SwarmCloud to use.",
    )
    const mine = within(box as HTMLElement).getByRole('link', { name: 'Install on octo-dev' })
    expect(mine.getAttribute('href')).toBe(APP_INSTALL)
    expect(mine.className).toContain('is-primary')
    expect(within(box as HTMLElement).getByRole('link', { name: 'Install on an organisation…' }).getAttribute('href')).toBe(APP_INSTALL)
    expect(visible(box)).toContain(
      "Organisations you don't see (for example your company's) appear here after the App is installed there; if you're not an owner, GitHub sends the owners a request.",
    )
    // The row itself offers Install, and the right column says why it is empty.
    expect(within(owner('octo-dev')).getByRole('link', { name: 'Install on octo-dev' }).getAttribute('href')).toBe(APP_INSTALL)
    expect(screen.getByText('Nothing to list until the App is installed')).toBeTruthy()
    // Authorized and installed are told apart in one line.
    expect(visible(document.querySelector('.ac-authz'))).toContain('Authorized is not installed')
  })

  it('draws mixed rows: Enable for the installed owner, Install for the other, and no notice', async () => {
    const mixed = {
      ...NOWHERE,
      owners: [
        { ...NOWHERE.owners[0], installation_id: 9, repository_selection: 'all', install_state: 'installed', install_url: null },
        { ...NOWHERE.owners[0], owner: 'example-co', owner_type: 'Organization' },
      ],
    }
    serve((m, url) => {
      if (m === 'GET' && url === '/v1/access') return { status: 200, body: overview({ orgs: [], grants: [] }) }
      if (m === 'GET' && url === '/v1/access/orgs') return { status: 200, body: mixed }
      return null
    })
    await mount()
    await screen.findByRole('list', { name: 'Owners your connection reaches' })
    expect(notice()).toBeNull()
    expect(within(owner('octo-dev')).getByRole('button', { name: 'Enable' })).toBeTruthy()
    expect(within(owner('octo-dev')).queryByRole('link', { name: /Install/ })).toBeNull()
    expect(visible(owner('example-co'))).toContain('not installed')
    expect(within(owner('example-co')).getByRole('link', { name: 'Install on example-co' }).getAttribute('href')).toBe(APP_INSTALL)
    expect(screen.getByText('No owner enabled yet')).toBeTruthy()
  })

  it('reads the owners again on Refresh and when the window regains focus', async () => {
    let installed = false
    const calls = serve((m, url) => {
      if (m === 'GET' && url === '/v1/access') return { status: 200, body: overview({ orgs: [], grants: [] }) }
      if (m === 'GET' && url === '/v1/access/orgs') {
        return {
          status: 200,
          body: installed ? { ...NOWHERE, owners: [{ ...NOWHERE.owners[0], installation_id: 9, install_state: 'installed', install_url: null }] } : NOWHERE,
        }
      }
      return null
    })
    await mount()
    await screen.findByRole('list', { name: 'Owners your connection reaches' })
    const reads = () => calls.filter((c) => c.method === 'GET' && c.url === '/v1/access/orgs').length
    expect(reads()).toBe(1)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(reads()).toBe(2))
    // Back from GitHub's install page: the tab regains focus.
    installed = true
    window.dispatchEvent(new Event('focus'))
    await waitFor(() => expect(reads()).toBe(3))
    await waitFor(() => expect(notice()).toBeNull())
    expect(within(owner('octo-dev')).getByRole('button', { name: 'Enable' })).toBeTruthy()
    // Focus and visibility fire together on one return: one read, not two.
    document.dispatchEvent(new Event('visibilitychange'))
    await new Promise((r) => setTimeout(r, 20))
    expect(reads()).toBe(3)
  })
})

describe('members (Access B)', () => {
  it('lists every member by state and name for an admin', async () => {
    api((m, url) =>
      m === 'GET' && url === '/v1/access/members'
        ? {
            status: 200,
            body: {
              tenant_id: 'eng',
              members: [
                { user: 'operator@swarm.example.com', connection: CONNECTION, orgs: [ORG('example-org')], grants: GRANTS },
                { user: 'second@swarm.example.com', connection: { ...CONNECTION, forge_login: 'example-user2', state: 'refresh_failed', failure: 'REFRESH_FAILED' }, orgs: [], grants: [] },
                { user: 'third@swarm.example.com', connection: null, orgs: [], grants: [] },
              ],
            },
          }
        : null,
    )
    await mount()
    fireEvent.click(screen.getByRole('radio', { name: 'Members' }))
    const list = await screen.findByRole('list', { name: 'Members of eng' })
    const rows = [...list.querySelectorAll('li.ac-member')] as HTMLElement[]
    expect(rows.map((r) => r.dataset.member)).toEqual(['operator@swarm.example.com', 'second@swarm.example.com', 'third@swarm.example.com'])
    expect(visible(rows[0]!)).toContain('3 · 2 write')
    expect(visible(rows[1]!)).toContain('REFRESH_FAILED')
    expect(visible(rows[2]!)).toContain('not started')
  })

  it('tells a non-admin the view is for admins', async () => {
    api((m, url) => (m === 'GET' && url === '/v1/access/members' ? { status: 403, body: { code: 'admin_required', message: "the tenant's members view is for admins" } } : null))
    await mount()
    fireEvent.click(screen.getByRole('radio', { name: 'Members' }))
    await waitFor(() => expect(visible(document.querySelector('.ac-members'))).toMatch(/admin/i))
    expect(screen.queryByRole('list', { name: 'Members of eng' })).toBeNull()
  })
})

describe('no value reaches the page', () => {
  it('draws nothing token-shaped even when a response carries one', async () => {
    api((m, url) => (m === 'GET' && url === '/v1/access' ? { status: 200, body: overview({ connection: { ...CONNECTION, leaked: leakedValue() } }) } : null))
    await mount()
    await screen.findByRole('list', { name: 'Your granted repositories, failures first' })
    expect(tokenShapedIn(document.documentElement.outerHTML)).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// QA of https://swarm.saga.xyz/access, 2026-10-08: four defects.
// ---------------------------------------------------------------------------

/**
 * Hold every request `match` names until the returned `open()` is called. The
 * held request reaches `serve` (and its `calls`) only when it is let through,
 * so a call recorded before `open()` was made while the held one was pending.
 */
function hold(match: (method: string, url: string) => boolean): () => void {
  const inner = globalThis.fetch
  let open: () => void = () => undefined
  const gate = new Promise<void>((r) => {
    open = r
  })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    if (match(init?.method ?? 'GET', String(input))) await gate
    return inner(input, init)
  }) as typeof fetch
  return open
}

const CONFLICT = { status: 409, body: { code: 'conflict', message: 'a refresh of your GitHub token is running; try again in a minute' } }

describe('(1) the token expiry counts forward', () => {
  it('says how long until an instant ahead, and expired for one behind', async () => {
    const { tokenExpiry } = await import('../Access')
    const now = Date.parse('2026-10-08T10:00:00Z')
    expect(tokenExpiry(new Date(now + (7 * 60 + 58) * 60_000 + 20_000).toISOString(), now)).toBe('expires in 7h 58m')
    expect(tokenExpiry(new Date(now + 45_000).toISOString(), now)).toBe('expires in 45s')
    expect(tokenExpiry(new Date(now - 3 * 60_000).toISOString(), now)).toBe('expired 3m ago')
    expect(tokenExpiry('not a date', now)).toBe('expiry unreadable')
  })

  it('draws a token eight hours ahead as expires in 7h 5xm, renewed automatically, never just now', async () => {
    api((m, url) =>
      m === 'GET' && url === '/v1/access'
        ? { status: 200, body: overview({ connection: { ...CONNECTION, access_expires_at: new Date(Date.now() + 8 * 3600_000 - 90_000).toISOString() } }) }
        : null,
    )
    await mount()
    await screen.findByRole('list', { name: 'Your granted repositories, failures first' })
    // The spans are joined by a CSS "·": expires in 7h 58m · renewed automatically.
    const spans = [...document.querySelectorAll('.ac-conn .ur-cmeta')[1]!.children].map((el) => visible(el))
    expect(spans[0]).toMatch(/^Token expires in 7h 5\dm$/)
    expect(spans[1]).toBe('renewed automatically')
    expect(visible(document.querySelector('.ac-conn'))).not.toContain('just now')
  })
})

describe('(2) the chooser says GitHub is being asked', () => {
  it('shows Asking GitHub for your repositories… while the owners are read, and not after', async () => {
    api()
    const open = hold((m, url) => m === 'GET' && url === '/v1/access/orgs')
    await mount()
    const card = (await screen.findByText('Choose repositories')).closest('.ac-choose') as HTMLElement
    expect(within(card).getByRole('status').textContent).toBe('Asking GitHub for your repositories…')
    expect(within(card).queryByRole('list', { name: 'Owners your connection reaches' })).toBeNull()
    open()
    await screen.findByRole('list', { name: 'Owners your connection reaches' })
    await screen.findByRole('list', { name: "example-org's repositories, page 1" })
    expect(within(card).queryByText('Asking GitHub for your repositories…')).toBeNull()
  })
})

describe('(3) Enable, then the repositories', () => {
  it('reads the new owner only after the POST answered, and retries one 409 with Still setting up…', async () => {
    let lab = 0
    let enabled = false
    const calls = api((m, url) => {
      if (m === 'GET' && url === '/v1/access' && enabled) return { status: 200, body: overview({ orgs: [ORG('example-org'), ORG('octo-dev'), ORG('example-lab')] }) }
      if (m === 'POST' && url === '/v1/access/orgs') {
        enabled = true
        return { status: 200, body: { tenant_id: 'eng', org: ORG('example-lab') } }
      }
      if (m === 'GET' && url === '/v1/access/orgs/example-lab/repositories?page=1') {
        lab += 1
        return lab === 1 ? CONFLICT : { status: 200, body: page(1, [REPO(9, 'example-lab-api', { repository: 'example-lab/example-lab-api', owner: 'example-lab' })]) }
      }
      return null
    })
    const open = hold((m, url) => m === 'POST' && url === '/v1/access/orgs')
    await mount()
    await screen.findByRole('list', { name: 'Owners your connection reaches' })
    const enable = within(owner('example-lab')).getByRole('button', { name: 'Enable' }) as HTMLButtonElement
    fireEvent.click(enable)
    // Pending at once, and nothing of example-lab's is read while the POST is out.
    await waitFor(() => expect(within(owner('example-lab')).getByRole('button', { name: 'Enabling…' }).getAttribute('aria-busy')).toBe('true'))
    await new Promise((r) => setTimeout(r, 30))
    expect(calls.some((c) => c.url.startsWith('/v1/access/orgs/example-lab/'))).toBe(false)
    open()
    await waitFor(() => expect(lab).toBe(1))
    expect(await screen.findByText('Still setting up…')).toBeTruthy()
    await screen.findByRole('list', { name: "example-lab's repositories, page 1" }, { timeout: 4000 })
    expect(lab).toBe(2)
    const urls = calls.map((c) => `${c.method} ${c.url}`)
    expect(urls.indexOf('POST /v1/access/orgs')).toBeLessThan(urls.indexOf('GET /v1/access/orgs/example-lab/repositories?page=1'))
    expect(screen.queryByText('Still setting up…')).toBeNull()
  })

  it('does not retry a 409 that is a refusal', async () => {
    const { passingConflict } = await import('../Access')
    const e = (code: string | null) => ({ kind: 'conflict' as const, httpStatus: 409, code, message: 'm' })
    expect(passingConflict(e('conflict'))).toBe(true)
    expect(passingConflict(e(null))).toBe(true)
    expect(passingConflict(e('access_refused'))).toBe(false)
    expect(passingConflict(e('github_not_connected'))).toBe(false)
    expect(passingConflict({ ...e('conflict'), httpStatus: 404 })).toBe(false)
  })
})

describe('(4) the controls survive a background re-read', () => {
  it('keeps the typed owner/repo, and the first Grant saves, when a re-read on focus fails', async () => {
    let reads = 0
    const calls = api((m, url, body) => {
      if (m === 'GET' && url === '/v1/access') {
        reads += 1
        return reads === 1 ? null : CONFLICT
      }
      if (m === 'GET' && url === '/v1/access/orgs' && reads > 1) return CONFLICT
      if (m === 'PUT' && url.startsWith('/v1/access/grants/')) {
        const b = body as { repository: string; mode: 'read' | 'write' }
        return { status: 200, body: { tenant_id: 'eng', grant: GRANT(8, b.repository, b.mode), registered: false, registration_repo_id: ID(8) } }
      }
      return null
    })
    await mount()
    const input = (await screen.findByRole('textbox', { name: 'Not listed? Type owner/repo' })) as HTMLInputElement
    await screen.findByRole('list', { name: 'Owners your connection reaches' })
    fireEvent.change(input, { target: { value: 'example-org/example-legacy' } })
    fireEvent.click(radio(document.querySelector('.ac-typed') as HTMLElement, 'Write'))
    // Back from GitHub: the tab regains focus and both re-reads meet a 409.
    window.dispatchEvent(new Event('focus'))
    await waitFor(() => expect(reads).toBe(2))
    await waitFor(() => expect(document.querySelectorAll('.ur-stale-why').length).toBeGreaterThan(0))
    // The same input, still holding what was typed: nothing remounted.
    expect(screen.getByRole('textbox', { name: 'Not listed? Type owner/repo' })).toBe(input)
    expect(input.value).toBe('example-org/example-legacy')
    expect(radio(document.querySelector('.ac-typed') as HTMLElement, 'Write').getAttribute('aria-checked')).toBe('true')
    const open = hold((m) => m === 'PUT')
    fireEvent.click(screen.getByRole('button', { name: 'Grant' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Granting…' }).getAttribute('aria-busy')).toBe('true'))
    open()
    expect(await screen.findByText(/example-org\/example-legacy granted write/)).toBeTruthy()
    expect(calls.filter((c) => c.method === 'PUT')).toEqual([expect.objectContaining({ body: { repository: 'example-org/example-legacy', mode: 'write' } })])
  })

  it('keeps an Enable that is out when the owners re-read fails, and the first click lands', async () => {
    let ownerReads = 0
    let enabled = false
    const calls = api((m, url) => {
      if (m === 'GET' && url === '/v1/access' && enabled) return { status: 200, body: overview({ orgs: [ORG('example-org'), ORG('octo-dev'), ORG('example-lab')] }) }
      if (m === 'GET' && url === '/v1/access/orgs') {
        ownerReads += 1
        return ownerReads === 2 ? CONFLICT : null
      }
      if (m === 'POST' && url === '/v1/access/orgs') {
        enabled = true
        return { status: 200, body: { tenant_id: 'eng', org: ORG('example-lab') } }
      }
      return null
    })
    const open = hold((m) => m === 'POST')
    await mount()
    await screen.findByRole('list', { name: 'Owners your connection reaches' })
    const row = owner('example-lab')
    fireEvent.click(within(row).getByRole('button', { name: 'Enable' }))
    fireEvent.click(within(row).getByRole('button', { name: 'Enabling…' }))
    window.dispatchEvent(new Event('focus'))
    await waitFor(() => expect(ownerReads).toBe(2))
    await waitFor(() => expect(document.querySelectorAll('.ur-stale-why').length).toBeGreaterThan(0))
    // The same row, still pending.
    expect(owner('example-lab')).toBe(row)
    expect(within(row).getByRole('button', { name: 'Enabling…' })).toBeTruthy()
    open()
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST')).toHaveLength(1))
    await waitFor(() => expect(owner('example-lab').className).toContain('is-selected'))
  })
})
