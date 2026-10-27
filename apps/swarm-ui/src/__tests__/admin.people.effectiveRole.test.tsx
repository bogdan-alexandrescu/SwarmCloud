// ADMIN › PEOPLE AND ROLES: THE EFFECTIVE ROLE AND THE WORKSPACE'S NAME (lane
// ROLE-EFFECTIVE-CELL, part of #2391).
//
// WHAT EACH CASE HOLDS:
//   * BGD 2026-10-26 (VQ-ROLES, "Effective role"): a platform admin's cell in
//     a group workspace reads "admin (platform admin)" -- the answer Config ›
//     People and roles, This workspace and Profile give -- not the stored
//     `member`; a plain member's cell is unchanged. The Manage drawer still
//     edits the stored grant, so it is not asked here;
//   * BGD 2026-10-26 ("Mark the source", ROLE-MARK): the suffix is drawn from
//     the row's `platform_admin` marker, which both reads serve, not from the
//     admins list, which a workspace admin's scoped read does not -- so a
//     workspace admin sees "admin (platform admin)" too;
//   * BGD 2026-10-26 (VQ-056): a column is headed by the workspace's name (the
//     switcher's rule: `/v1/tenants/mine`'s stored name, an address counting
//     as no name), else its id -- never its group email, which stays the
//     head's title.
//
// Placeholders only; nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }

type Json = Record<string, unknown>

const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString()

function person(email: string, roles: Json): Json {
  return {
    email,
    teams: [],
    github: 'connected',
    workspace: { state: 'none', workspace_id: null, steps: {}, limits: null },
    claude_account: { source: 'none', own: 0, lent: 0, lent_by: [], provider_key: false },
    loan_request: null,
    first_seen: ago(600),
    last_seen: ago(1),
    last_submitted: null,
    last_active: ago(1),
    roles,
  }
}

// As `people.role_columns` serves them: each named by its group email.
const COLUMNS = [
  { tenant_id: 'eng', group: 'eng@example.com', display_name: 'eng@example.com', default_role: 'member', unavailable: false },
  { tenant_id: 'smoke', group: 'smoke@example.com', display_name: 'smoke@example.com', default_role: 'member', unavailable: false },
]

// The platform admin, stored `member` in both (the grant the drawer edits),
// marked as the API marks them (`people.mark_platform_admin`).
const OPS = { ...person('ops@example.com', { eng: 'member', smoke: 'none' }), platform_admin: true }
const FAY = person('fay@example.com', { eng: 'member', smoke: 'viewer' })

function platform(rows: Json[], over: Json = {}): Json {
  return {
    scope: 'platform',
    workspaces: COLUMNS,
    workspaces_error: null,
    people: rows,
    count: rows.length,
    pending: 0,
    audit: [],
    lendable_accounts: [],
    lendable_error: null,
    admins: [{ email: 'ops@example.com', role: 'admin', granted_by: 'owner@example.com', granted_at: ago(60) }],
    admins_error: null,
    ...over,
  }
}

// `/v1/tenants/mine` for the platform admin: eng has a stored name, smoke none.
const MINE = [
  { tenant_id: 'eng', display_name: 'Engineering', group: 'eng@example.com', kind: 'group', via: 'platform_admin' },
  { tenant_id: 'smoke', display_name: 'smoke@example.com', kind: 'group', via: 'platform_admin' },
]

async function mount(body: Json) {
  serve((m, url) => {
    if (m === 'GET' && url === '/v1/admin/people') return { status: 200, body }
    if (m === 'GET' && url === '/v1/tenants/mine') return { status: 200, body: MINE }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { PeopleScreen } = await import('../People')
  render(<PeopleScreen />)
  await screen.findByRole('heading', { name: 'People and roles', level: 1 }, WAIT)
  await waitFor(() => expect(document.querySelector('.pp-people tbody tr')).not.toBeNull(), WAIT)
}

const cell = (email: string, tenant: string) => document.querySelector<HTMLElement>(`.pp-people tr[data-email="${email}"] td[data-tenant="${tenant}"]`)
const head = (tenant: string) => document.querySelector<HTMLElement>(`.pp-people thead th[data-tenant="${tenant}"]`)

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  window.localStorage.clear()
})

describe("a platform admin's cell is their effective role (VQ-ROLES)", () => {
  it('reads admin (platform admin) in every group workspace, whatever is stored', async () => {
    await mount(platform([OPS, FAY]))
    // MUTATION: draw the stored cell -> "member" and "none", and this fails.
    expect(visible(cell('ops@example.com', 'eng'))).toBe('admin (platform admin)')
    expect(visible(cell('ops@example.com', 'smoke'))).toBe('admin (platform admin)')
    expect(cell('ops@example.com', 'eng')?.dataset.role).toBe('admin')
  })

  it('draws "(platform admin)" on a line of its own, under "admin"', async () => {
    await mount(platform([OPS, FAY]))
    // The cell is `white-space: nowrap`: on one line, "admin (platform admin)"
    // widened the grid from 1009px to 1102px at 1440 and pushed Manage behind
    // the table's scroll (rendered review, 2026-10-26). A <br> is the break.
    // MUTATION: drop the <br> -> one line again, and this fails.
    const td = cell('ops@example.com', 'eng')
    const br = td?.querySelector('br')
    expect(br).not.toBeNull()
    expect(br?.previousElementSibling?.textContent).toBe('admin')
    expect(br?.nextElementSibling?.textContent).toBe('(platform admin)')
  })

  it("leaves a plain member's cell as the API said it", async () => {
    await mount(platform([OPS, FAY]))
    expect(visible(cell('fay@example.com', 'eng'))).toBe('member')
    expect(visible(cell('fay@example.com', 'smoke'))).toBe('viewer')
  })

  it('reads the platform admin as admin even where the API already said admin', async () => {
    await mount(platform([{ ...OPS, roles: { eng: 'admin', smoke: 'admin' } }]))
    expect(visible(cell('ops@example.com', 'eng'))).toBe('admin (platform admin)')
  })
})

// A workspace admin's read (`people.in_workspaces`): no admins list at all.
function scoped(rows: Json[]): Json {
  return { scope: 'workspaces', workspaces: [COLUMNS[0]], workspaces_error: null, people: rows, count: rows.length, pending: 0 }
}

describe('the suffix comes from the row\'s marker, not the admins list (ROLE-MARK)', () => {
  it("draws admin (platform admin) in a workspace admin's scoped read, which serves no admins list", async () => {
    await mount(scoped([
      { email: 'ops@example.com', display_name: null, roles: { eng: 'admin' }, last_active: ago(2), platform_admin: true },
      { email: 'fay@example.com', display_name: null, roles: { eng: 'admin' }, last_active: ago(3) },
    ]))
    // MUTATION: draw the suffix from `admins` -> the scoped read has none, "admin" alone, and this fails.
    expect(visible(cell('ops@example.com', 'eng'))).toBe('admin (platform admin)')
    expect(cell('ops@example.com', 'eng')?.querySelector('br')).not.toBeNull()
    expect(cell('ops@example.com', 'eng')?.dataset.source).toBe('platform_admin')
    // fay holds admin by grant: no marker, no suffix.
    expect(visible(cell('fay@example.com', 'eng'))).toBe('admin')
    expect(cell('fay@example.com', 'eng')?.dataset.source).toBeUndefined()
  })

  it('draws no suffix for a row the API did not mark, even when the admins list names them', async () => {
    // An unmarked row's cell is the API's answer: the list alone no longer decides it.
    await mount(platform([person('ops@example.com', { eng: 'member', smoke: 'none' }), FAY]))
    expect(visible(cell('ops@example.com', 'eng'))).toBe('member')
    expect(visible(cell('ops@example.com', 'smoke'))).toBe('none')
  })
})

describe('a workspace is headed by its name, not its email (VQ-056)', () => {
  it("heads eng by its stored name and smoke, which has none, by its id", async () => {
    await mount(platform([OPS, FAY]))
    // MUTATION: head the column with `display_name` as served -> the emails, and this fails.
    expect(visible(head('eng'))).toBe('Engineering')
    expect(visible(head('smoke'))).toBe('smoke')
    // The group's address is still there, as the head's title.
    expect(head('eng')?.title).toBe('eng@example.com')
    expect(cell('fay@example.com', 'eng')?.dataset.label).toBe('Engineering')
  })
})
