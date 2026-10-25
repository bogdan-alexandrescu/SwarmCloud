// ADMIN › PEOPLE AND ROLES (ADMIN-PEOPLE-ROLES; part of #2157; BGD picked
// docs/web-ui/mockups/config-e.html, frames S5c and S5f: "Every person against
// every workspace; the lead is who is waiting for an admin").
//
// WHAT EACH CASE HOLDS:
//   * a platform admin's page opens with a lead naming the OLDEST waiting
//     request, and its one button puts focus on that request's Approve;
//   * with nothing waiting the lead is green and says so;
//   * the table has a column per group workspace, each cell the person's
//     role there, where Teams was; a column whose roles were not read is a
//     dash, never a guessed role;
//   * a workspace admin's page (`scope: workspace_admin`) draws only their
//     workspaces' columns as role selects, and none of the platform's cards;
//     a select writes that one workspace's role route, and "workspace
//     default" clears it;
//   * `/admin/people` renders that scoped page for a workspace admin rather
//     than sending them away, and their Admin panel lists it.
//
// MUTATION: drop `PeopleLead` and the lead cases go red; take `items[0]` from
// the newest end and the "oldest" case does; draw `RoleText` for a scoped
// row and the select case does; filter `WORKSPACE_ADMIN_PAGES` back to
// Workspaces you admin alone and the routing case does.
//
// Placeholders only; nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }

type Json = Record<string, unknown>

const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString()

function ws(state: string, id: string, over: Json = {}): Json {
  return {
    state, setup_url: '/setup#workspace', setup_command: '/sc:setup', workspace_id: id, request_id: `req-${id}`,
    requested_at: ago(5), requested_via: 'console', decision: null,
    limits: { max_active: 8, capacity_units: 8, quota_pods: 16, quota_cpu: 64 },
    steps: {}, failure: null, ready_at: null, request_again_at: null, dispatch: null, run: null, retried_at: null,
    ...over,
  }
}

function person(email: string, workspace: Json, roles: Json, over: Json = {}): Json {
  return {
    email, teams: ['eng'], github: 'connected', workspace,
    claude_account: { source: 'none', own: 0, lent: 0, lent_by: [], provider_key: false },
    loan_request: null, first_seen: ago(600), last_seen: ago(1), last_submitted: null, last_active: ago(1),
    roles,
    ...over,
  }
}

const COLUMNS = [
  { tenant_id: 'eng', display_name: 'eng@example.com', default_role: 'member', unavailable: false },
  { tenant_id: 'research', display_name: 'research@example.com', default_role: null, unavailable: true },
]

// jo asked for a workspace 40 minutes ago, fay to borrow an account 10
// minutes ago, lee for a workspace 5 minutes ago: jo's is the oldest.
const LEE = person('lee@example.com', ws('requested', 'w-1ee000', { requested_at: ago(5) }), { eng: 'member', research: null })
const JO = person('jo@example.com', ws('requested', 'w-j00000', { requested_at: ago(40) }), { eng: 'none', research: null }, { teams: [], display_name: 'Jo' })
const FAY = person('fay@example.com', ws('ready', 'w-fa1000'), { eng: 'admin', research: null }, { loan_request: { state: 'requested', requested_at: ago(10) } })
const BOB = person('bob@example.com', ws('ready', 'w-b0b000'), { eng: 'viewer', research: null })

function platform(rows: Json[]): Json {
  return {
    scope: 'platform', workspaces: COLUMNS, workspaces_error: null,
    people: rows, count: rows.length,
    pending: rows.filter((r) => (r.workspace as Json).state === 'requested').length,
    audit: [], lendable_accounts: [], lendable_error: null, admins: [], admins_error: null,
  }
}

const SCOPED: Json = {
  scope: 'workspace_admin',
  workspaces: [COLUMNS[0]],
  people: [
    { email: 'fay@example.com', display_name: null, roles: { eng: 'admin' }, last_active: ago(3) },
    { email: 'bob@example.com', display_name: 'Bob', roles: { eng: 'viewer' }, last_active: ago(60) },
    { email: 'kai@example.com', display_name: null, roles: { eng: 'none' }, last_active: null },
  ],
  count: 3, pending: 0, provisioning: null, audit: [],
  lendable_accounts: null, lendable_error: 'platform_admin_only', admins: null, admins_error: 'platform_admin_only',
}

async function mount(body: Json, writes: Record<string, (body: unknown) => { status: number; body: unknown }> = {}) {
  const calls = serve((m, url, b) => {
    if (m === 'GET' && url === '/v1/admin/people') return { status: 200, body }
    // The platform page's Workspace roles card: an empty page is enough here.
    if (m === 'GET' && url.startsWith('/v1/admin/workspaces/roles')) return { status: 200, body: { workspaces: [], roles: [], next_page_token: null } }
    const key = `${m} ${url}`
    if (writes[key] !== undefined) return writes[key]!(b)
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { PeopleScreen } = await import('../People')
  render(<PeopleScreen />)
  await screen.findByRole('heading', { name: 'People and roles', level: 1 }, WAIT)
  await waitFor(() => expect(document.querySelector('.pp-people tbody tr')).not.toBeNull(), WAIT)
  return calls
}

const lead = () => document.querySelector<HTMLElement>('.adm-lead')
const heads = () => [...document.querySelectorAll('.pp-people thead th')].map((th) => visible(th))
const cellOf = (email: string, col: string) => document.querySelector<HTMLElement>(`.pp-people tr[data-email="${email}"] td[data-col="${col}"]`)

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

describe('a platform admin (S5c)', () => {
  it('leads with who is waiting, names the oldest request, and its button goes to it', async () => {
    await mount(platform([LEE, JO, FAY, BOB]))
    const l = lead()!
    expect(l.getAttribute('data-lead')).toBe('warn')
    expect(visible(l.querySelector('.ws-lead-k'))).toBe('Needs an admin')
    expect(visible(l.querySelector('b'))).toBe('3 requests are waiting for an admin')
    // jo, 40 minutes ago: older than fay's loan (10) and lee's workspace (5).
    expect(visible(l.querySelector('.ws-lead-s'))).toMatch(/^The oldest: Jo asked for a personal workspace 40m ago\./)
    const go = l.querySelector<HTMLButtonElement>('.ws-lead-a button')!
    expect(visible(go)).toBe('Review the oldest')
    fireEvent.click(go)
    const jo = document.querySelector<HTMLElement>('.pp-waiting li[data-kind="workspace"][data-email="jo@example.com"]')!
    expect(jo.contains(document.activeElement)).toBe(true)
    expect(visible(document.activeElement)).toBe('Approve')
  })

  it('leads green when nothing is waiting', async () => {
    await mount(platform([{ ...FAY, loan_request: null }, BOB]))
    const l = lead()!
    expect(l.getAttribute('data-lead')).toBe('ok')
    expect(visible(l.querySelector('b'))).toBe('No one is waiting for an admin')
    expect(l.querySelector('button')).toBeNull()
  })

  it('says a request held for the platform owner is not one waiting here, so the lead and "1 pending" agree', async () => {
    const held = person('hal@example.com', ws('requested', 'w-ha1000', { held: { reason: 'migrating' } }), { eng: 'none', research: null })
    await mount(platform([held, BOB]))
    const l = lead()!
    expect(l.getAttribute('data-lead')).toBe('ok')
    expect(visible(l.querySelector('.ws-lead-s'))).toBe('1 workspace request waits for the platform owner, not for an approval here.')
  })

  it('draws a role column per group workspace where Teams was, and a dash for one not read', async () => {
    await mount(platform([LEE, JO, FAY, BOB]))
    expect(heads()).toEqual(['Person', 'eng', 'research', 'Personal workspace', 'Claude account', 'Ceiling', 'Last active', 'Manage'])
    expect(document.querySelector('.pp-people thead th[title="eng@example.com"]')).not.toBeNull()
    expect(visible(cellOf('fay@example.com', 'eng'))).toBe('admin')
    expect(visible(cellOf('bob@example.com', 'eng'))).toBe('viewer')
    expect(visible(cellOf('jo@example.com', 'eng'))).toBe('none')
    const unread = cellOf('bob@example.com', 'research')!.querySelector('.c-dash')
    expect(unread?.getAttribute('aria-label')).toBe('This workspace’s roles could not be read')
    // The platform's controls stay: Waiting for you, and Manage on each row.
    expect(document.querySelector('.pp-waiting')).not.toBeNull()
    expect(document.querySelectorAll('.pp-people tbody .pp-manage-cell button')).toHaveLength(4)
  })
})

describe('a workspace admin (S5f)', () => {
  it('draws only their workspaces as role selects, and none of the platform cards', async () => {
    await mount(SCOPED)
    const l = lead()!
    expect(l.getAttribute('data-lead')).toBe('ok')
    expect(visible(l.querySelector('.ws-lead-k'))).toBe('Your workspaces')
    expect(visible(l.querySelector('b'))).toBe('No one is waiting for you')
    expect(visible(l.querySelector('.ws-lead-s'))).toMatch(/^Showing eng, the workspace you administer\. A request for a personal workspace is a platform admin’s/)
    expect(heads()).toEqual(['Person', 'eng', 'Last active'])
    const select = cellOf('bob@example.com', 'eng')!.querySelector('select')!
    expect(select.value).toBe('viewer')
    expect([...select.options].map((o) => o.value)).toEqual(['admin', 'member', 'viewer', 'default'])
    expect(cellOf('kai@example.com', 'eng')!.querySelector('select')!.value).toBe('none')
    for (const platformOnly of ['.pp-waiting', '.pp-admins', '.pp-audit', '.wra-card', '.pp-manage-cell']) {
      expect(document.querySelector(platformOnly), platformOnly).toBeNull()
    }
    expect(document.body.textContent).not.toContain('research')
  })

  it('writes one workspace’s role route from a select, and clears it for the default', async () => {
    const calls = await mount(SCOPED, {
      'PUT /v1/admin/people/workspaces/eng/role': () => ({ status: 200, body: { email: 'bob@example.com', role: 'member', previous_role: 'viewer', source: 'override', granted_at: ago(0) } }),
      'DELETE /v1/admin/people/workspaces/eng/role': () => ({ status: 200, body: { email: 'bob@example.com', role: 'member', previous_role: 'viewer', source: 'default', granted_at: ago(0) } }),
    })
    const select = () => cellOf('bob@example.com', 'eng')!.querySelector('select')!
    fireEvent.change(select(), { target: { value: 'member' } })
    await waitFor(() => expect(visible(document.querySelector('.pp-scoped'))).toContain('bob@example.com is now member in eng.'))
    fireEvent.change(select(), { target: { value: 'default' } })
    await waitFor(() => expect(visible(document.querySelector('.pp-scoped'))).toContain('bob@example.com is now member in eng (the workspace default).'))
    const writes = calls.filter((c) => c.method !== 'GET')
    expect(writes.map((c) => [c.method, c.url])).toEqual([
      ['PUT', '/v1/admin/people/workspaces/eng/role'],
      ['DELETE', '/v1/admin/people/workspaces/eng/role'],
    ])
    expect(writes[0]!.body).toEqual({ email: 'bob@example.com', role: 'member', via: 'console' })
    expect(writes[1]!.body).toEqual({ email: 'bob@example.com', via: 'console' })
  })
})

describe('/admin/people for a workspace admin', () => {
  const ME = {
    tenant: { tenant_id: 'u-ana', display_name: 'Your workspace', kind: 'user' },
    principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: false },
    environment: 'dev', environment_declared: true,
  }
  const MINE = [
    { tenant_id: 'u-ana', display_name: 'ana.b@example.com', kind: 'user', via: 'personal', role: { name: 'admin', source: 'personal' } },
    { tenant_id: 'eng', display_name: 'eng@example.com', kind: 'group', via: 'member', role: { name: 'admin', source: 'override' } },
  ]

  it('renders the scoped page rather than sending them away, and their Admin panel lists it', async () => {
    serve((m, url) => {
      if (url.includes('/v1/tenants/mine')) return { status: 200, body: MINE }
      if (url.includes('/v1/tenants/me')) return { status: 200, body: ME }
      if (m === 'GET' && url === '/v1/admin/people') return { status: 200, body: SCOPED }
      return { status: 503, body: {} }
    })
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { App } = await import('../App')
    window.history.replaceState(null, '', '/admin/people')
    render(<App />)
    await screen.findByRole('heading', { name: 'People and roles', level: 1 }, WAIT)
    await waitFor(() => expect(document.querySelector('.pp-scoped tbody tr')).not.toBeNull(), WAIT)
    await waitFor(() => expect(document.querySelector('.sk-pfoot small')?.textContent).toBe('ana.b@example.com'), WAIT)
    // Both reads have landed, and the address is still the page's.
    await new Promise((r) => setTimeout(r, 30))
    expect(window.location.pathname).toBe('/admin/people')
    const rows = [...document.querySelectorAll('.sk-panel .sk-pk .sk-pl')].map((e) => e.textContent)
    expect(rows).toEqual(['People and roles', 'Workspaces you admin'])
  })
})
