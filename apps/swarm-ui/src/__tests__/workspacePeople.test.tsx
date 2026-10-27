// SETTINGS › PEOPLE AND ROLES, A WORKSPACE'S OWN ROLES PAGE (lane
// ROLES-UI-PEOPLE; docs/design/workspace-roles.md §4.3, §4.8, Q7; part of
// #1518).
//
// WHAT EACH CASE HOLDS:
//   * `#settings/people` (and `settings/people`) is this page, a Settings
//     route and not a SECTIONS tab; `#settings/<anything else>` still lands on
//     Pool limits;
//   * every role reads the roster -- the default, the overrides -- what each
//     role permits (the server's own sentences) and the role events (Q7); a
//     `viewer` is offered no control at all;
//   * an `admin` (`can_manage`) is offered the three controls: set a
//     person's role, return a person to the default, set the default;
//   * the default selector offers `viewer` and `member` only: an admin is
//     named person by person (§4.6);
//   * a personal workspace shows the fixed-admin line and no control;
//   * a write sends the email in the BODY (never the path, §4.3), re-reads,
//     and a refusal is shown in the API's words with nothing claimed.
//
// MUTATIONS, each of which turns a case red: draw the controls on
// `can_manage: false`; offer `admin` in the default selector; drop the
// personal branch; put the email in the path.
//
// Fixtures only; no network. Nothing here is credential-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { canonical, fromAddress } from '../App'

type Json = Record<string, unknown>

const ROLES_TABLE = [
  { name: 'viewer', summary: 'Sees everything in this workspace; changes nothing.' },
  { name: 'member', summary: 'Submits, approves, answers, cancels, and manages schedules, repositories and accounts.' },
  { name: 'admin', summary: "A member who also manages secrets, MCP servers, provider keys, the workspace's settings and people's roles." },
]

function groupRoles(me: Json, over: Json = {}): Json {
  return {
    tenant_id: 'eng',
    kind: 'group',
    default_role: 'member',
    default_source: 'migration',
    me: { email: 'carol@example.com', via: 'member', ...me },
    people: [
      {
        email: 'alice@example.com', role: 'admin', source: 'override',
        granted_by: 'ops@example.com', granted_at: '2026-10-18T09:14:03.120000Z',
        granted_via: 'platform_admin', reason: 'first admin',
      },
      {
        email: 'dave@example.com', role: 'viewer', source: 'override',
        granted_by: 'alice@example.com', granted_at: '2026-10-18T10:00:00.000000Z',
        granted_via: 'console', reason: '',
      },
    ],
    roles: ROLES_TABLE,
    can_manage: false,
    audit: [
      {
        action: 'workspace_role_granted', by: 'alice@example.com', at: '2026-10-18T10:00:00Z',
        target_email: 'dave@example.com', detail: { from: 'member', to: 'viewer', via: 'console', reason: '' },
      },
      {
        action: 'platform_admin_acted', by: 'ops@example.com', at: '2026-10-18T09:14:03Z',
        detail: { method: 'PUT', route: '/v1/workspace/roles/person', path_params: {}, via: 'platform_admin', reach: 'non_member' },
      },
    ],
    ...over,
  }
}

const VIEWER = groupRoles({ role: 'viewer', source: 'override', granted_by: 'alice@example.com', granted_at: '2026-10-18T10:00:00.000000Z' })
const ADMIN = groupRoles({ role: 'admin', source: 'override', granted_by: 'ops@example.com', granted_at: '2026-10-18T09:14:03.120000Z' }, { can_manage: true })
const PERSONAL: Json = {
  tenant_id: 'u-carol',
  kind: 'user',
  default_role: null,
  default_source: null,
  me: { email: 'carol@example.com', role: 'admin', source: 'personal', via: 'personal' },
  people: [],
  roles: ROLES_TABLE,
  can_manage: false,
  audit: [],
}

const ROLES_URL = '/v1/workspace/roles'

function api(body: () => Json, write?: (m: string, url: string, b: unknown) => { status: number; body: unknown } | null) {
  return serve((m, url, b) => {
    if (m === 'GET' && url === ROLES_URL) return { status: 200, body: body() }
    if (m !== 'GET' && write) return write(m, url, b)
    return null
  })
}

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../WorkspacePeople')
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

describe('People and roles: its address', () => {
  it('is config/people, a Config tab since CONFIG-NAV (part of #2157), and settings/people lands there', () => {
    const r = fromAddress('settings/people')
    expect(r).toMatchObject({ sectionId: 'config', tab: 'people' })
    expect(canonical(r)).toBe('config/people')
  })

  it('leaves the other Settings pages and the old hashes where they were', () => {
    expect(fromAddress('settings/keys')).toMatchObject({ sectionId: 'config', tab: 'secrets' })
    expect(fromAddress('settings/profile')).toMatchObject({ sectionId: 'settings', tab: 'profile' })
    expect(fromAddress('settings/anything-else')).toMatchObject({ sectionId: 'admin', tab: 'limits' })
  })

  it('opens the page, under its own heading, at #settings/people', async () => {
    window.history.replaceState(null, '', '/#settings/people')
    const calls = serve((m, url) => (m === 'GET' && url === ROLES_URL ? { status: 200, body: VIEWER } : null))
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { App } = await import('../App')
    render(<App />)
    const page = await screen.findByRole('heading', { level: 1, name: 'People and roles' })
    expect(page.closest('.wp-page'), 'the roles page is not what #settings/people drew').not.toBeNull()
    await screen.findByText('dave@example.com')
    expect(calls.some((c) => c.method === 'GET' && c.url === ROLES_URL)).toBe(true)
  })
})

describe('Settings › People and roles', () => {
  it('shows a viewer the roster, the roles and the events, and no control', async () => {
    const calls = api(() => VIEWER)
    const { WorkspacePeopleScreen } = await load()
    render(<WorkspacePeopleScreen />)
    const roster = await screen.findByRole('table', { name: /who holds which role in eng/i })
    const rows = visible(roster)
    expect(rows).toContain('alice@example.com')
    expect(rows).toContain('admin')
    expect(rows).toContain('dave@example.com')
    expect(rows).toContain('viewer')
    // The caller's own row (S5b, CONFIG-PEOPLE-S5): their role, read for them too.
    const mine = within(roster).getAllByRole('row').find((r) => visible(r).includes('carol@example.com'))
    expect(mine && visible(mine)).toMatch(/you.*viewer/)
    // The default, and that it is the migration's, not a grant's.
    expect(visible(screen.getByLabelText('Default role'))).toMatch(/member/)
    // The caller's own role, and its source.
    expect(visible(screen.getByLabelText('Your role'))).toMatch(/viewer/)
    // What each role permits, in the server's own words.
    for (const r of ROLES_TABLE) expect(screen.getByText(r.summary)).toBeTruthy()
    // The events (Q7): a grant and a platform admin's action.
    const events = screen.getByRole('list', { name: /role events/i })
    expect(within(events).getAllByRole('listitem')).toHaveLength(2)
    expect(visible(events)).toMatch(/dave@example\.com/)
    expect(visible(events)).toMatch(/platform admin/i)
    // No control of any kind.
    expect(screen.queryByRole('button', { name: /set role/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /return .* to the default/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /set default/i })).toBeNull()
    expect(screen.queryByRole('combobox')).toBeNull()
    expect(screen.queryByRole('group', { name: 'Role' })).toBeNull()
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(screen.getByText(/only this workspace.s admins change roles/i)).toBeTruthy()
    expect(calls.filter((c) => c.method !== 'GET')).toHaveLength(0)
  })

  it('offers an admin the controls: set a role, return to the default, set the default', async () => {
    api(() => ADMIN)
    const { WorkspacePeopleScreen } = await load()
    render(<WorkspacePeopleScreen />)
    await screen.findByRole('table', { name: /who holds which role in eng/i })
    expect(screen.getByRole('button', { name: 'Set role' })).toBeTruthy()
    expect(screen.getByRole('textbox', { name: 'Email' })).toBeTruthy()
    // S5b's segmented control, not a <select> (CONFIG-PEOPLE-S5).
    expect(screen.getByRole('group', { name: 'Role' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Return alice@example.com to the default' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Return dave@example.com to the default' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Set default' })).toBeTruthy()
    expect(screen.queryByText(/only this workspace.s admins change roles/i)).toBeNull()
  })

  it("offers viewer and member, never admin, as the workspace's default", async () => {
    api(() => ADMIN)
    const { WorkspacePeopleScreen } = await load()
    render(<WorkspacePeopleScreen />)
    const select = await screen.findByRole('combobox', { name: 'New default role' })
    const offered = within(select).getAllByRole('option').map((o) => (o as HTMLOptionElement).value)
    expect(offered).toEqual(['viewer', 'member'])
    // A person's role may be any of the three.
    const personal = screen.getByRole('group', { name: 'Role' })
    expect(within(personal).getAllByRole('button').map((o) => o.textContent)).toEqual(['viewer', 'member', 'admin'])
  })

  it("shows a personal workspace's fixed-admin line and no control", async () => {
    api(() => PERSONAL)
    const { WorkspacePeopleScreen } = await load()
    render(<WorkspacePeopleScreen />)
    await screen.findByText(/^You are this workspace.s admin; a personal workspace has only you\.$/)
    expect(screen.queryByRole('table', { name: /who holds which role/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /set role|set default|return/i })).toBeNull()
    expect(screen.queryByRole('combobox')).toBeNull()
  })

  it('sets a role with the email in the body, then reads again', async () => {
    let current = ADMIN
    const calls = api(() => current, (m, url) => {
      if (m === 'PUT' && url === '/v1/workspace/roles/person') {
        current = groupRoles({ role: 'admin', source: 'override' }, {
          can_manage: true,
          people: [...(ADMIN.people as Json[]), { email: 'erin@example.com', role: 'viewer', source: 'override', granted_by: 'carol@example.com', granted_at: '2026-10-19T08:00:00.000000Z', granted_via: 'console', reason: '' }],
        })
        return { status: 200, body: { email: 'erin@example.com', role: 'viewer', previous_role: 'member', source: 'override', granted_at: '2026-10-19T08:00:00.000000Z', member: true } }
      }
      return null
    })
    const { WorkspacePeopleScreen } = await load()
    render(<WorkspacePeopleScreen />)
    fireEvent.change(await screen.findByRole('textbox', { name: 'Email' }), { target: { value: ' erin@example.com ' } })
    fireEvent.click(within(screen.getByRole('group', { name: 'Role' })).getByRole('button', { name: 'viewer' }))
    fireEvent.click(screen.getByRole('button', { name: 'Set role' }))
    await screen.findByText(/erin@example\.com is now viewer/i)
    const put = calls.find((c) => c.method === 'PUT')
    expect(put?.url).toBe('/v1/workspace/roles/person')
    expect(put?.body).toEqual({ email: 'erin@example.com', role: 'viewer', via: 'console' })
    await waitFor(() => expect(visible(screen.getByRole('table', { name: /who holds which role in eng/i }))).toContain('erin@example.com'))
  })

  it('returns a person to the default with a DELETE whose body names them', async () => {
    const calls = api(() => ADMIN, (m, url) =>
      m === 'DELETE' && url === '/v1/workspace/roles/person'
        ? { status: 200, body: { email: 'dave@example.com', role: 'member', previous_role: 'viewer', source: 'default', granted_at: '2026-10-19T08:00:00.000000Z' } }
        : null)
    const { WorkspacePeopleScreen } = await load()
    render(<WorkspacePeopleScreen />)
    fireEvent.click(await screen.findByRole('button', { name: 'Return dave@example.com to the default' }))
    await screen.findByText(/dave@example\.com holds the default again/i)
    const del = calls.find((c) => c.method === 'DELETE')
    expect(del?.body).toEqual({ email: 'dave@example.com', via: 'console' })
  })

  it("shows a refusal in the API's words and claims nothing", async () => {
    const calls = api(() => ADMIN, (m) =>
      m === 'PUT'
        ? { status: 409, body: { code: 'role_unchanged', message: "eng's default is already member; nothing was written" } }
        : null)
    const { WorkspacePeopleScreen } = await load()
    render(<WorkspacePeopleScreen />)
    fireEvent.change(await screen.findByRole('combobox', { name: 'New default role' }), { target: { value: 'member' } })
    fireEvent.click(screen.getByRole('button', { name: 'Set default' }))
    const alert = await screen.findByRole('alert')
    expect(visible(alert)).toMatch(/already member; nothing was written/)
    const put = calls.find((c) => c.method === 'PUT')
    expect(put?.url).toBe('/v1/workspace/roles/default')
    expect(put?.body).toEqual({ role: 'member', via: 'console' })
    expect(screen.queryByText(/default is now/i)).toBeNull()
  })
})
