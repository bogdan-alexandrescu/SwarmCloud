// QA1-FIX-01 (part of #2596, console visual QA pass 1): CONFIG › GITHUB ACCESS.
//
// WHAT EACH CASE HOLDS (the finding it pins in brackets):
//   * an access token whose expiry is past is not drawn as an active,
//     automatically renewed one: the card says the next use renews it, in
//     warn tone (config-01);
//   * a refused Request install on Where the App is installed is drawn as a
//     full-width row under its owner, never inside the narrow Action cell that
//     squeezed the Owner column (config-02);
//   * over a repository list this API does not serve, Search is disabled with
//     its reason and Chosen still lists the grants the page already holds
//     (config-03);
//   * an organisation not installed offers Request install alone, on both
//     tabs (config-04);
//   * Esc or Keep on an ask-first banner gives focus back to the button that
//     opened it (config-05);
//   * an owner card's Remove is a danger button that says how many grants it
//     removes (config-06);
//   * the installs table's state column is "Installed" and its action cell
//     carries no stacked label (config-07).
//
// Fixtures only; no network. Fake values are built at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
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

describe('config-01: an expired token is not drawn as renewed automatically', () => {
  it('says the next use renews an expired token, in warn tone, and drops "renewed automatically"', async () => {
    api({ access_expires_at: new Date(Date.now() - 9 * DAY).toISOString(), refreshed_at: new Date(Date.now() - 9 * DAY).toISOString() })
    await mount()
    const token = await screen.findByText(/expired 9 days ago|expired 9d ago/)
    const card = token.closest('.ac-conn') as HTMLElement
    // MUTATION: keep the unconditional <span>renewed automatically</span> in Connection.
    expect(card.textContent).not.toContain('renewed automatically')
    // MUTATION: drop the renewal from an expired token's state (said once since config2-02).
    expect(card.textContent).toContain('token renews on next use')
    // MUTATION: draw the expired token without its warn ToneMark.
    expect(token.closest('[data-tone="warn"]')).not.toBeNull()
  })

  it('still says a token ahead of its expiry is renewed automatically', async () => {
    api()
    await mount()
    const card = (await screen.findByText(/expires in/)).closest('.ac-conn') as HTMLElement
    expect(card.textContent).toContain('renewed automatically')
    expect(card.textContent).not.toContain('renews on next use')
  })
})

describe('config-02: a refused Request install is a full-width row under its owner', () => {
  it('draws the refusal in its own row spanning the table, not inside the Action cell', async () => {
    vi.spyOn(window, 'open').mockImplementation(() => null)
    api()
    await mount()
    const table = await screen.findByRole('table', { name: 'Where the App is installed' })
    const row = table.querySelector('tr[data-owner="example-labs"]') as HTMLElement
    await act(async () => {
      fireEvent.click(within(row).getByRole('button', { name: 'Request install' }))
    })
    const refusal = await within(table).findByText('example-labs was not changed')
    // MUTATION: render <AccessRefusal> inside td.ac-installs-a again.
    expect(row.querySelector('td.ac-installs-a')?.textContent).not.toContain('was not changed')
    const cell = refusal.closest('td') as HTMLTableCellElement
    expect(cell.colSpan).toBe(4)
    expect(cell.closest('tr')?.previousElementSibling).toBe(row)
  })
})

describe('config-03: controls over a repository list this API does not serve', () => {
  it('disables Search with its reason and lists the known grants under Chosen', async () => {
    api()
    await mount('/config/github?view=choose')
    const region = await screen.findByRole('region', { name: "example-org's repositories" })
    await within(region).findByText('Not served yet')
    const search = within(region).getByRole('searchbox') as HTMLInputElement
    // MUTATION: leave the search input and Search enabled while the list read is not served.
    expect(search.disabled).toBe(true)
    expect((within(region).getByRole('button', { name: 'Search' }) as HTMLButtonElement).disabled).toBe(true)
    // MUTATION: draw the hint under All too, above the "Not served yet" box that says it again.
    expect(region.textContent).not.toContain('Search needs the repository list')
    fireEvent.click(within(region).getByRole('radio', { name: 'Chosen' }))
    expect(region.textContent).toContain('Search needs the repository list')
    // MUTATION: draw Chosen from the unserved list read only, so it shows nothing.
    const rows = region.querySelectorAll('[data-repo]')
    expect([...rows].map((r) => r.getAttribute('data-repo'))).toEqual(['example-org/example-api', 'example-org/example-docs', 'example-org/example-web'])
    expect(region.textContent).toContain('3 chosen under example-org')
  })
})

describe('config-04: one action per owner state, the same on both tabs', () => {
  it('offers an organisation not installed Request install alone on its Choose card', async () => {
    api()
    await mount('/config/github?view=choose')
    const card = (await screen.findAllByText('example-labs')).map((n) => n.closest('li.ac-owner')).find((n) => n !== null) as HTMLElement
    // MUTATION: draw the primary "Install on example-labs" link beside Request install again.
    expect(within(card).queryByText('Install on example-labs')).toBeNull()
    expect(within(card).getByRole('button', { name: 'Request install' })).toBeTruthy()
    // A person's own account is still installed from its card.
    const mine = (screen.getAllByText('octo-dev').map((n) => n.closest('li.ac-owner')).find((n) => n !== null)) as HTMLElement
    expect(within(mine).getByText('Install on octo-dev')).toBeTruthy()
  })
})

describe('config-05: closing an ask-first banner gives focus back to its opener', () => {
  it('returns focus to Disconnect GitHub on Esc and on Keep connected', async () => {
    api()
    await mount()
    const open = () => {
      const b = screen.getByRole('button', { name: 'Disconnect GitHub' })
      b.focus()
      fireEvent.click(b)
    }
    await screen.findByRole('button', { name: 'Disconnect GitHub' })
    open()
    const keep = screen.getByRole('button', { name: 'Keep connected' })
    expect(document.activeElement).toBe(keep)
    fireEvent.keyDown(keep, { key: 'Escape' })
    // MUTATION: drop the opener.focus() in AskFirst's keep path.
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Disconnect GitHub' }))
    open()
    fireEvent.click(screen.getByRole('button', { name: 'Keep connected' }))
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Disconnect GitHub' }))
  })
})

describe('config-06: an owner card’s Remove says its scope', () => {
  it('draws Remove as a danger button with the grant count in its words', async () => {
    api()
    await mount('/config/github?view=choose')
    const card = (await screen.findAllByText('example-org')).map((n) => n.closest('li.ac-owner')).find((n) => n !== null) as HTMLElement
    // MUTATION: label it plain "Remove" in a ghost button again.
    const remove = within(card).getByRole('button', { name: 'Remove all 3' })
    expect(remove.className).toContain('is-danger')
  })
})

describe('config-07: the installs table’s stacked labels', () => {
  it('heads the state column "Installed" and gives the action cell no label', async () => {
    api()
    await mount()
    const table = await screen.findByRole('table', { name: 'Where the App is installed' })
    // MUTATION: name the column "Install" again, the word the action button says.
    expect([...table.querySelectorAll('thead th')].map((th) => th.textContent)).toEqual(['Owner', 'Installed', 'Chosen', 'Action'])
    const row = table.querySelector('tr[data-owner="example-org"]') as HTMLElement
    expect([...row.children].map((c) => c.getAttribute('data-label'))).toEqual(['Owner', 'Installed', 'Chosen', null])
  })
})
