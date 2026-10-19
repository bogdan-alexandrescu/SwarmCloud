// ADMIN › PEOPLE'S WORKSPACE ROLES (lanes ROLES-UI-ADMIN, ROLES-ADMIN-LIST;
// part of #1518; docs/design/workspace-roles.md §4.3, §4.8, owner answer Q2).
//
// WHAT EACH CASE HOLDS:
//   * Admin › People has a "Workspace roles" card listing every registered
//     group workspace from `GET /v1/admin/workspaces/roles` -- Terraform
//     groups and job-made ones -- with its default, its admins, its number of
//     overrides and each override's role, granter and when, from ONE read per
//     page and never a read per workspace;
//   * "Show more workspaces" reads the next page with its `page_token`;
//   * an entry the list could not serve shows the API's reason and offers
//     that workspace's own roster read;
//   * "Name an admin" sends `PUT /v1/admin/workspaces/{id}/roles/person` with
//     `{email, role: "admin", via: "console"}` -- the email in the body, never
//     the path -- and nothing without an email, and re-reads the page;
//   * the default selector offers `viewer` and `member` only, and "Set
//     default" sends the admin route's `/default`;
//   * the Manage drawer has "Roles in workspaces", read from
//     `GET /v1/admin/roles/person?email=`.
//
// Placeholders only; nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }

type Json = Record<string, unknown>

const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString()

function person(email: string): Json {
  return {
    email,
    teams: ['eng'],
    github: 'connected',
    workspace: { state: 'ready', workspace_id: `w-${email.slice(0, 3)}000`, setup_url: '/setup#workspace', setup_command: '/sc:setup', limits: { max_active: 8 } },
    claude_account: { source: 'none', own: 0, lent: 0, lent_by: [], provider_key: false },
    loan_request: null,
    first_seen: ago(600),
    last_seen: ago(1),
    last_submitted: null,
    last_active: ago(1),
  }
}

const PEOPLE = {
  people: [person('bob@example.com'), person('carol@example.com')],
  count: 2,
  pending: 0,
  audit: [],
  lendable_accounts: [],
  lendable_error: null,
  admins: [{ email: 'owner@example.com', role: 'owner', granted_by: 'config:PLATFORM_OWNER', granted_at: null }],
  admins_error: null,
}

const ROLES = [
  { name: 'viewer', summary: 'Sees everything in this workspace; changes nothing.' },
  { name: 'member', summary: 'Submits, approves, answers, cancels.' },
  { name: 'admin', summary: 'A member who also manages secrets and roles.' },
]

function override(email: string, role: string): Json {
  return { email, role, source: 'override', granted_by: 'owner@example.com', granted_at: ago(30), granted_via: 'platform_admin' }
}

const ROSTERS: Record<string, Json> = {
  eng: {
    tenant_id: 'eng', kind: 'group', group: 'eng@example.com', registered_by: 'terraform', default_role: 'member', default_source: 'grant',
    people: [override('bob@example.com', 'admin'), override('carol@example.com', 'viewer')],
  },
  ops: { tenant_id: 'ops', kind: 'group', group: 'ops@example.com', registered_by: 'workspace_job', default_role: 'member', default_source: 'migration', people: [] },
  sec: {
    tenant_id: 'sec', kind: 'group', group: 'sec@example.com', registered_by: 'terraform',
    unavailable: true, code: 'too_many_grants', message: 'this page holds more grants than one read looks at; read this workspace alone',
  },
  design: { tenant_id: 'design', kind: 'group', group: 'design@example.com', registered_by: 'workspace_job', default_role: 'viewer', default_source: 'grant', people: [] },
}

/** The list's two pages: eng, ops and sec, then design. */
const PAGES: Record<string, Json> = {
  '': { workspaces: [ROSTERS.eng, ROSTERS.ops, ROSTERS.sec], roles: ROLES, next_page_token: 'sec' },
  sec: { workspaces: [ROSTERS.design], roles: ROLES, next_page_token: null },
}

/** `GET /v1/admin/workspaces/{id}/roles`: what the per-workspace route serves for a workspace read alone. */
const ALONE: Record<string, Json> = {
  sec: { tenant_id: 'sec', kind: 'group', default_role: 'viewer', default_source: 'grant', people: [override('dan@example.com', 'admin')], roles: ROLES, audit: [] },
}

type Write = (body: unknown) => { status: number; body: unknown }

async function mount(writes: Record<string, Write> = {}) {
  const calls = serve((m, url, b) => {
    if (m === 'GET' && url === '/v1/admin/people') return { status: 200, body: PEOPLE }
    if (m === 'GET' && (url === '/v1/admin/workspaces/roles' || url.startsWith('/v1/admin/workspaces/roles?'))) {
      const token = new URLSearchParams(url.split('?')[1] ?? '').get('page_token') ?? ''
      const page = PAGES[token]
      return page === undefined ? { status: 422, body: { code: 'validation_failed', message: 'page_token names no registered group workspace' } } : { status: 200, body: page }
    }
    const ws = /^\/v1\/admin\/workspaces\/([^/]+)\/roles$/.exec(url)
    if (m === 'GET' && ws !== null) {
      const r = ALONE[ws[1]!]
      return r === undefined ? { status: 404, body: { code: 'not_found', message: 'no registered group workspace by that id' } } : { status: 200, body: r }
    }
    if (m === 'GET' && url.startsWith('/v1/admin/roles/person?')) {
      const email = new URLSearchParams(url.split('?')[1]).get('email')
      return {
        status: 200,
        body: {
          email,
          workspaces: email === 'bob@example.com'
            ? [{ ...override('bob@example.com', 'admin'), tenant_id: 'eng', group: 'eng@example.com', key: 'k1' }, { tenant_id: 'ops', group: 'ops@example.com', unavailable: true }]
            : [],
        },
      }
    }
    const key = `${m} ${url}`
    if (writes[key] !== undefined) return writes[key]!(b)
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { PeopleScreen } = await import('../People')
  render(<PeopleScreen />)
  await screen.findByRole('heading', { name: 'People', level: 1 }, WAIT)
  const card = await waitFor(() => {
    const c = document.querySelector<HTMLElement>('.wra-card')
    expect(c, 'no Workspace roles card').not.toBeNull()
    return c!
  }, WAIT)
  return { calls, card }
}

const rowOf = (id: string) => document.querySelector<HTMLElement>(`.wra-card li[data-tenant="${id}"]`)
const writesOf = (calls: { method: string; url: string; body: unknown }[]) => calls.filter((c) => c.method !== 'GET')

async function loaded(id: string): Promise<HTMLElement> {
  return waitFor(() => {
    const r = rowOf(id)
    expect(r, `no row for ${id}`).not.toBeNull()
    expect(r!.querySelector('[data-default]')).not.toBeNull()
    return r!
  }, WAIT)
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.localStorage.clear()
  window.sessionStorage.clear()
})

describe('Admin › People: the Workspace roles card', () => {
  it('lists every group workspace from one read of the admin list, with its default, its admins and its overrides', async () => {
    // MUTATION: read each roster alone, or list `/v1/tenants/mine`, or drop the card from People.
    const { calls, card } = await mount()
    expect(within(card).getByRole('heading', { name: 'Workspace roles' })).toBeTruthy()
    const eng = await loaded('eng')
    expect(eng.querySelector('[data-default]')!.getAttribute('data-default')).toBe('member')
    expect(visible(eng.querySelector('.wra-admins'))).toContain('bob@example.com')
    expect(visible(eng.querySelector('.wra-count'))).toBe('2 overrides')
    const carol = visible(eng.querySelector('li[data-email="carol@example.com"]'))
    expect(carol).toContain('viewer')
    expect(carol).toContain('given by owner@example.com (platform admin), 30m ago')
    // A group the workspace job made is listed beside the Terraform ones.
    const ops = await loaded('ops')
    expect(visible(ops.querySelector('.wra-admins'))).toBe('no admin yet')
    expect(visible(ops)).toContain('made by the workspace job')
    expect(visible(ops)).toContain('as before roles')
    const reads = calls.filter((c) => c.method === 'GET' && c.url.startsWith('/v1/admin/workspaces/'))
    expect(reads.map((c) => c.url)).toEqual(['/v1/admin/workspaces/roles'])
    expect(calls.some((c) => c.url === '/v1/tenants/mine')).toBe(false)
  })

  it('reads the next page with its token on "Show more workspaces"', async () => {
    const { calls, card } = await mount()
    await loaded('eng')
    expect(rowOf('design')).toBeNull()
    fireEvent.click(within(card).getByRole('button', { name: 'Show more workspaces' }))
    const design = await loaded('design')
    expect(design.querySelector('[data-default]')!.getAttribute('data-default')).toBe('viewer')
    expect(calls.some((c) => c.method === 'GET' && c.url === '/v1/admin/workspaces/roles?page_token=sec')).toBe(true)
    // The last page offers no more.
    expect(within(card).queryByRole('button', { name: 'Show more workspaces' })).toBeNull()
  })

  it('shows why an entry was not listed, and reads that workspace alone on request', async () => {
    // MUTATION: render an unavailable entry as an empty roster.
    const { calls } = await mount()
    await loaded('eng')
    const sec = rowOf('sec')!
    expect(visible(sec)).toContain('read this workspace alone (too_many_grants)')
    expect(sec.querySelector('[data-default]')).toBeNull()
    fireEvent.click(within(sec).getByRole('button', { name: 'Read this workspace alone' }))
    const alone = await loaded('sec')
    expect(visible(alone.querySelector('.wra-admins'))).toBe('dan@example.com')
    expect(calls.some((c) => c.method === 'GET' && c.url === '/v1/admin/workspaces/sec/roles')).toBe(true)
  })

  it('"Name an admin" puts {email, role: admin} in the body of the admin route, and sends nothing without an email', async () => {
    // MUTATION: name the person in the path, or send role "member".
    const { calls } = await mount({
      'PUT /v1/admin/workspaces/ops/roles/person': () => ({
        status: 200,
        body: { email: 'carol@example.com', role: 'admin', previous_role: 'member', source: 'override', granted_at: ago(0), member: true },
      }),
    })
    const ops = await loaded('ops')
    const name = within(ops).getByRole('button', { name: 'Name an admin' })
    fireEvent.click(name)
    expect(writesOf(calls)).toHaveLength(0)
    fireEvent.change(within(ops).getByLabelText('Email of the admin to name in ops'), { target: { value: ' carol@example.com ' } })
    fireEvent.click(name)
    await waitFor(() => expect(writesOf(calls)).toHaveLength(1), WAIT)
    const w = writesOf(calls)[0]!
    expect(w.method).toBe('PUT')
    expect(w.url).toBe('/v1/admin/workspaces/ops/roles/person')
    expect(w.body).toEqual({ email: 'carol@example.com', role: 'admin', via: 'console' })
    await waitFor(() => expect(visible(rowOf('ops'))).toContain('carol@example.com is now admin in ops'), WAIT)
    // The write re-reads the page it was made from.
    await waitFor(() => expect(calls.filter((c) => c.method === 'GET' && c.url === '/v1/admin/workspaces/roles')).toHaveLength(2), WAIT)
  })

  it('offers viewer and member as the default, and "Set default" writes the admin route', async () => {
    // MUTATION: offer every role in the default selector.
    const { calls } = await mount({
      'PUT /v1/admin/workspaces/eng/roles/default': () => ({ status: 200, body: { role: 'viewer', previous_role: 'member' } }),
    })
    const eng = await loaded('eng')
    const select = within(eng).getByLabelText('Default role in eng') as HTMLSelectElement
    expect([...select.options].map((o) => o.value)).toEqual(['viewer', 'member'])
    fireEvent.change(select, { target: { value: 'viewer' } })
    fireEvent.click(within(eng).getByRole('button', { name: 'Set default' }))
    await waitFor(() => expect(writesOf(calls)).toHaveLength(1), WAIT)
    expect(writesOf(calls)[0]).toMatchObject({ method: 'PUT', url: '/v1/admin/workspaces/eng/roles/default', body: { role: 'viewer', via: 'console' } })
  })

  it('returns one person to the default through the admin route, with the email in the body', async () => {
    const { calls } = await mount({
      'DELETE /v1/admin/workspaces/eng/roles/person': () => ({
        status: 200,
        body: { email: 'carol@example.com', role: 'member', previous_role: 'viewer', source: 'default', granted_at: ago(0) },
      }),
    })
    const eng = await loaded('eng')
    const carol = eng.querySelector<HTMLElement>('li[data-email="carol@example.com"]')!
    fireEvent.click(within(carol).getByRole('button', { name: 'Return to default' }))
    await waitFor(() => expect(writesOf(calls)).toHaveLength(1), WAIT)
    expect(writesOf(calls)[0]).toMatchObject({ method: 'DELETE', url: '/v1/admin/workspaces/eng/roles/person', body: { email: 'carol@example.com', via: 'console' } })
  })
})

describe('Admin › People: the drawer reads the person’s roles', () => {
  it('lists "Roles in workspaces" from the admin person route', async () => {
    // MUTATION: drop the drawer section.
    const { calls } = await mount()
    const row = document.querySelector<HTMLElement>('.pp-people tr[data-email="bob@example.com"]')!
    fireEvent.click(within(row).getByRole('button', { name: 'Manage' }))
    const drawer = await screen.findByRole('dialog', { name: 'Manage bob@example.com' }, WAIT)
    const sec = await waitFor(() => {
      const s = drawer.querySelector<HTMLElement>('[data-section="workspace-roles"]')
      expect(s).not.toBeNull()
      expect(s!.querySelector('li[data-tenant="eng"]')).not.toBeNull()
      return s!
    }, WAIT)
    expect(within(sec).getByRole('heading', { name: 'Roles in workspaces' })).toBeTruthy()
    expect(visible(sec.querySelector('li[data-tenant="eng"]'))).toContain('admin')
    expect(visible(sec.querySelector('li[data-tenant="ops"]'))).toContain('not read')
    expect(calls.some((c) => c.url === '/v1/admin/roles/person?email=bob%40example.com')).toBe(true)
  })
})
