// ADMIN › PEOPLE AND ROLES (ADMIN-PEOPLE-ROLES; part of #2157, proposal E
// frames S5c and S5f, config-e.html): "Every person against every workspace;
// the lead is who is waiting for an admin."
//
// WHAT EACH CASE HOLDS:
//   * the page is "People and roles", and a platform admin's table is a
//     person × workspace grid: one column per workspace the API served, each
//     cell the person's role there (admin / member / viewer / none), then
//     their own workspace (requested, ready...), the Claude account, the
//     ceiling, Last active and Manage -- the controls stay on the right rows;
//   * the lead names the oldest waiting request, and its one button takes the
//     reader to that request's row on Waiting for you;
//   * a workspace admin's read (`scope: workspaces`) draws only their
//     columns, the people in them and Last active: no Waiting for you, no
//     Manage, no Admins, no Recent admin actions, no platform roles card;
//   * a cell the API could not read is a dash with its reason, never a role.
//
// Placeholders only; nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }

type Json = Record<string, unknown>

const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString()

function ws(state: string, id: string, over: Json = {}): Json {
  return {
    state,
    setup_url: '/setup#workspace',
    setup_command: '/sc:setup',
    workspace_id: id,
    request_id: `req-${id}`,
    requested_at: ago(5),
    requested_via: 'console',
    decision: null,
    limits: { max_active: 8, capacity_units: 8, quota_pods: 16, quota_cpu: 64 },
    steps: {},
    failure: null,
    ready_at: null,
    request_again_at: null,
    dispatch: null,
    run: null,
    retried_at: null,
    ...over,
  }
}

function person(email: string, workspace: Json, roles: Json, over: Json = {}): Json {
  return {
    email,
    teams: [],
    github: 'connected',
    workspace,
    claude_account: { source: 'none', own: 0, lent: 0, lent_by: [], provider_key: false },
    loan_request: null,
    first_seen: ago(600),
    last_seen: ago(1),
    last_submitted: null,
    last_active: ago(1),
    roles,
    ...over,
  }
}

const COLUMNS = [
  { tenant_id: 'eng', group: 'eng@example.com', display_name: 'Engineering', default_role: 'member', unavailable: false },
  { tenant_id: 'labs', group: 'labs@example.com', display_name: 'Labs', default_role: 'viewer', unavailable: false },
]

// devops asked for a workspace 40 minutes ago, fay to borrow an account 10 minutes ago.
const DEVOPS = person('devops@example.com', ws('requested', 'w-7f9285', { requested_at: ago(40) }), { eng: 'none', labs: 'none' })
const FAY = person('fay@example.com', ws('ready', 'w-fa1000'), { eng: 'member', labs: 'none' }, { loan_request: { state: 'requested', requested_at: ago(10) } })
const BOB = person('bob@example.com', ws('ready', 'w-91c0de'), { eng: 'admin', labs: 'viewer' })

function platform(rows: Json[], over: Json = {}): Json {
  return {
    scope: 'platform',
    workspaces: COLUMNS,
    workspaces_error: null,
    people: rows,
    count: rows.length,
    pending: rows.filter((r) => (r.workspace as Json).state === 'requested').length,
    audit: [],
    lendable_accounts: [],
    lendable_error: null,
    admins: [{ email: 'bob@example.com', role: 'admin', granted_by: 'owner@example.com', granted_at: ago(60) }],
    admins_error: null,
    ...over,
  }
}

const SCOPED: Json = {
  scope: 'workspaces',
  workspaces: [COLUMNS[0]],
  workspaces_error: null,
  people: [
    { email: 'bob@example.com', display_name: 'Bob', roles: { eng: 'admin' }, last_active: ago(3) },
    { email: 'fay@example.com', display_name: null, roles: { eng: 'member' }, last_active: null },
  ],
  count: 2,
  pending: 0,
}

async function mount(body: Json) {
  const calls = serve((m, url) => (m === 'GET' && url === '/v1/admin/people' ? { status: 200, body } : null))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { PeopleScreen } = await import('../People')
  render(<PeopleScreen />)
  await screen.findByRole('heading', { name: 'People and roles', level: 1 }, WAIT)
  await waitFor(() => expect(document.querySelector('.pp-people tbody tr')).not.toBeNull(), WAIT)
  return calls
}

const heads = () => [...document.querySelectorAll<HTMLElement>('.pp-people thead th')].map((th) => th.textContent?.trim())
const cell = (email: string, tenant: string) => document.querySelector<HTMLElement>(`.pp-people tr[data-email="${email}"] td[data-tenant="${tenant}"]`)

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('a platform admin reads every person against every workspace (S5c)', () => {
  it('draws one column per workspace, each cell the role there, and keeps Manage on each row', async () => {
    await mount(platform([BOB, DEVOPS, FAY]))
    // MUTATION: draw the Teams column instead of one per workspace -> fails.
    expect(heads()).toEqual(['Person', 'Engineering', 'Labs', 'Own workspace', 'Claude account', 'Ceiling', 'Last active', 'Manage'])
    expect(cell('bob@example.com', 'eng')?.textContent).toBe('admin')
    expect(cell('bob@example.com', 'labs')?.textContent).toBe('viewer')
    expect(cell('fay@example.com', 'eng')?.textContent).toBe('member')
    expect(cell('devops@example.com', 'eng')?.dataset.role).toBe('none')
    // Their own workspace's request is a cell of its own row.
    expect(visible(document.querySelector('.pp-people tr[data-email="devops@example.com"] td[data-label="Own workspace"]'))).toContain('w-7f9285')
    for (const tr of document.querySelectorAll<HTMLElement>('.pp-people tbody tr')) {
      expect(within(tr).getAllByRole('button').map((b) => b.textContent)).toEqual(['Manage'])
    }
  })

  it('names the platform admins in the grid', async () => {
    await mount(platform([BOB, DEVOPS]))
    expect(visible(document.querySelector('.pp-people tr[data-email="bob@example.com"] th'))).toContain('platform admin')
    expect(visible(document.querySelector('.pp-people tr[data-email="devops@example.com"] th'))).not.toContain('platform admin')
  })

  it('leads with the oldest waiting request, and its one button goes to that request', async () => {
    await mount(platform([BOB, FAY, DEVOPS]))
    const lead = document.querySelector<HTMLElement>('.pp-page .adm-lead')!
    expect(lead, 'no lead').not.toBeNull()
    expect(lead.dataset.lead).toBe('warn')
    // MUTATION: lead with the newest request, or the first row -> fay is named and this fails.
    expect(visible(lead)).toContain('2 requests are waiting for an admin')
    expect(visible(lead)).toContain('The oldest: devops@example.com asked for a workspace 40m ago')
    const buttons = within(lead).getAllByRole('button')
    expect(buttons.map((b) => b.textContent)).toEqual(['Review the oldest'])
    fireEvent.click(buttons[0]!)
    const oldest = document.querySelector<HTMLElement>('.pp-waiting li[data-kind="workspace"][data-email="devops@example.com"]')!
    await waitFor(() => expect(oldest.contains(document.activeElement)).toBe(true), WAIT)
  })

  it('says nobody is waiting when nothing is open', async () => {
    await mount(platform([BOB]))
    const lead = document.querySelector<HTMLElement>('.pp-page .adm-lead')!
    expect(lead.dataset.lead).toBe('ok')
    expect(visible(lead)).toContain('Nobody is waiting for an admin')
    expect(within(lead).queryAllByRole('button')).toHaveLength(0)
  })

  it('draws a column whose roles were not read as a dash, never a role', async () => {
    await mount(platform([{ ...BOB, roles: { eng: 'admin', labs: null } }], { workspaces: [COLUMNS[0], { ...COLUMNS[1], unavailable: true, default_role: null }] }))
    // MUTATION: fall back to the column's default for an unread cell -> fails.
    expect(cell('bob@example.com', 'labs')?.dataset.role).toBe('unread')
    expect(cell('bob@example.com', 'labs')?.textContent).toBe('—')
  })
})

describe('a workspace admin reads only their workspaces (S5f)', () => {
  it('draws their columns, the people in them and Last active, and nothing of the platform', async () => {
    await mount(SCOPED)
    // MUTATION: draw the platform table for a scoped read -> the extra heads fail this.
    expect(heads()).toEqual(['Person', 'Engineering', 'Last active'])
    expect(cell('bob@example.com', 'eng')?.textContent).toBe('admin')
    expect(cell('fay@example.com', 'eng')?.textContent).toBe('member')
    expect(document.querySelector('.pp-waiting')).toBeNull()
    expect(document.querySelector('.pp-admins')).toBeNull()
    expect(document.querySelector('.pp-audit')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Manage' })).toBeNull()
    expect(screen.queryByRole('heading', { name: 'Workspace roles' })).toBeNull()
  })

  it('leads with the workspaces it shows, and says a personal request is a platform admin\'s', async () => {
    await mount(SCOPED)
    const lead = document.querySelector<HTMLElement>('.pp-page .adm-lead')!
    expect(lead.dataset.lead).toBe('ok')
    expect(visible(lead)).toContain('Nobody is waiting for you in Engineering')
    expect(visible(lead)).toContain('Showing Engineering, the workspace you administer.')
    expect(visible(lead)).toContain('platform admin')
  })

  it('reads only the one route, and writes nothing', async () => {
    const calls = await mount(SCOPED)
    expect(calls.filter((c) => c.method !== 'GET')).toEqual([])
    expect(calls.map((c) => c.url)).toEqual(['/v1/admin/people'])
  })
})
