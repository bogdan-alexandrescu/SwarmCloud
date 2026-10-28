// QA1-FIX-17 (visual QA pass 1, part of #2596): Admin › People and roles.
//
// WHAT EACH CASE HOLDS:
//   * admin-shell-05: a members read that failed is said in words ("was not
//     found"), never the error's raw kind, and the note says the roles in the
//     column stand;
//   * admin-shell-08: Escape in a deny reason cancels it and hands focus back
//     to Deny, for a workspace request and for a request to join;
//   * admin-shell-17: no reader-facing text quotes PLATFORM_OWNER or
//     ADMIN_GROUPS;
//   * admin-shell-20: a request with no limits says what approving sets.
//
// The fetch stub and fixtures are admin.peopleJoin.test.tsx's. Placeholders
// only; nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { visible } from './repofixture'

const WAIT = { timeout: 4000 }

type Json = Record<string, unknown>
type Answer = { status: number; body: unknown }
type Call = { method: string; url: string; body: unknown; tenant: string | null }

const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString()
const REF = 'j-' + 'c0ffee'.repeat(2)

const COLUMNS = [
  { tenant_id: 'team', group: 'team@example.com', display_name: 'Example Team', default_role: 'viewer', unavailable: false },
  { tenant_id: 'labs', group: 'labs@example.com', display_name: 'Example Labs', default_role: 'member', unavailable: false },
]

const LEE_JOIN = {
  ref: REF,
  tenant_id: 'labs',
  display_name: 'Example Labs',
  requester: 'lee@example.com',
  requester_name: null,
  requested_at: ago(120),
  expires_at: ago(-60 * 24 * 13),
  message: 'On the labs rota from Monday',
}

function scoped(over: Json = {}): Json {
  return {
    scope: 'workspaces',
    workspaces: COLUMNS,
    workspaces_error: null,
    people: [
      { email: 'sam@example.com', display_name: null, roles: { team: 'admin', labs: 'admin' }, last_active: ago(60) },
      { email: 'kai@example.com', display_name: null, roles: { team: 'none', labs: 'member' }, last_active: ago(600) },
      { email: 'riley@example.com', display_name: null, roles: { team: 'viewer', labs: 'member' }, last_active: ago(12) },
      { email: 'lee@example.com', display_name: null, roles: { team: 'none', labs: 'requested' }, last_active: null },
    ],
    count: 4,
    pending: 1,
    join_requests: [LEE_JOIN],
    join_requests_error: null,
    ...over,
  }
}

function ws(state: string, id: string, over: Json = {}): Json {
  return {
    state,
    setup_url: '/setup#workspace',
    setup_command: '/sc:setup',
    workspace_id: id,
    request_id: `req-${id}`,
    requested_at: ago(40),
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

function person(email: string, workspace: Json, roles: Json): Json {
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
  }
}

function platform(): Json {
  const rows = [
    person('jo@example.com', ws('requested', 'w-0j0000'), { team: 'none', labs: 'none' }),
    person('lee@example.com', ws('none', '', { workspace_id: null }), { team: 'none', labs: 'requested' }),
  ]
  return {
    scope: 'platform',
    workspaces: COLUMNS,
    workspaces_error: null,
    people: rows,
    count: rows.length,
    pending: 2,
    audit: [],
    lendable_accounts: [],
    lendable_error: null,
    admins: [],
    admins_error: null,
    join_requests: [LEE_JOIN],
    join_requests_error: null,
  }
}

/** A fetch stub that sees the tenant header, which the members read is keyed by. */
function stub(handler: (method: string, url: string, body: unknown, tenant: string | null) => Answer | null): Call[] {
  const calls: Call[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    const body = init?.body ? JSON.parse(String(init.body)) : null
    const headers = (init?.headers ?? {}) as Record<string, string>
    const tenant = headers['X-Swarm-Tenant'] ?? null
    calls.push({ method, url, body, tenant })
    const r = handler(method, url, body, tenant) ?? { status: 404, body: { detail: 'Not Found' } }
    return new Response(JSON.stringify(r.body), { status: r.status, headers: { 'content-type': 'application/json' } })
  }) as unknown as typeof fetch
  return calls
}

async function mount(handler: (method: string, url: string, body: unknown, tenant: string | null) => Answer | null) {
  const calls = stub(handler)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { PeopleScreen } = await import('../People')
  render(<PeopleScreen />)
  await screen.findByRole('heading', { name: 'People and roles', level: 1 }, WAIT)
  await waitFor(() => expect(document.querySelector('.pp-people tbody tr')).not.toBeNull(), WAIT)
  return calls
}

const people = (doc: Json, members: (tenant: string | null) => Answer = () => ({ status: 200, body: { platform_memberships: false, members: [] } })) =>
  (method: string, url: string, _body: unknown, tenant: string | null): Answer | null => {
    if (method === 'GET' && url === '/v1/admin/people') return { status: 200, body: doc }
    if (method === 'GET' && url === '/v1/workspace/members') return members(tenant)
    return null
  }

const cell = (email: string, tenant: string) => document.querySelector<HTMLElement>(`.pp-people tr[data-email="${email}"] td[data-tenant="${tenant}"]`)
const joinRow = () => document.querySelector<HTMLElement>(`.pp-waiting li.pp-req[data-kind="join"][data-ref="${REF}"]`)

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})


describe('admin-shell-05: the members-unread note is in words and leaves the roles standing', () => {
  it('says "not found" for a 404, never the raw kind, and that the roles are a separate read', async () => {
    await mount(
      people(scoped(), (tenant) =>
        tenant === 'team' || tenant === 'labs' ? { status: 404, body: { detail: 'Not Found' } } : { status: 200, body: { platform_memberships: true, members: [] } },
      ),
    )
    await waitFor(() => expect(document.querySelectorAll('.pp-joined-unread')).toHaveLength(1), WAIT)
    const note = visible(document.querySelector('.pp-joined-unread'))
    // MUTATION: print `${name} (${read.why})` again -> "not_found" is back.
    expect(note).not.toContain('not_found')
    expect(note).toContain('Example Team and Example Labs columns')
    expect(note).toContain('their members lists were not found')
    // MUTATION: drop the second sentence -> the note reads as if the roles were unread.
    expect(note).toContain('roles shown in them are read separately and are not affected')
    // The roles are drawn in the same columns.
    expect(cell('kai@example.com', 'labs')?.dataset.role).toBe('member')
  })

  it('maps each kind to words', async () => {
    const { membersUnreadWhy } = await import('../People')
    expect(membersUnreadWhy('not_found', false)).toBe('was not found')
    expect(membersUnreadWhy('forbidden', true)).toBe('were refused to you')
    expect(membersUnreadWhy('server_error', false)).toBe('could not be read')
  })
})

describe('admin-shell-08: Escape cancels a deny reason and returns focus to Deny', () => {
  it('on a request to join', async () => {
    await mount(people(scoped()))
    const row = joinRow() as HTMLElement
    fireEvent.click(within(row).getByRole('button', { name: 'Deny' }))
    const reason = within(row).getByLabelText('Reason, shown to lee@example.com')
    await waitFor(() => expect(document.activeElement).toBe(reason), WAIT)
    // MUTATION: drop the form's onKeyDown -> the reason stays open and focus stays in it.
    fireEvent.keyDown(reason, { key: 'Escape' })
    await waitFor(() => expect(within(row).queryByLabelText('Reason, shown to lee@example.com')).toBeNull(), WAIT)
    const deny = within(row).getByRole('button', { name: 'Deny' })
    await waitFor(() => expect(document.activeElement).toBe(deny), WAIT)
  })

  it('on a workspace request', async () => {
    await mount(people(platform()))
    const row = document.querySelector<HTMLElement>('.pp-waiting li.pp-req[data-kind="workspace"][data-email="jo@example.com"]') as HTMLElement
    fireEvent.click(within(row).getByRole('button', { name: 'Deny' }))
    const reason = within(row).getByLabelText('Reason, shown to jo@example.com')
    await waitFor(() => expect(document.activeElement).toBe(reason), WAIT)
    // MUTATION: drop the form's onKeyDown -> fails.
    fireEvent.keyDown(reason, { key: 'Escape' })
    await waitFor(() => expect(within(row).queryByLabelText('Reason, shown to jo@example.com')).toBeNull(), WAIT)
    await waitFor(() => expect(document.activeElement).toBe(within(row).getByRole('button', { name: 'Deny' })), WAIT)
  })
})

describe('admin-shell-17: no configuration variable names in reader-facing text', () => {
  it('the Admins card names where the owner is set and admin groups in words', async () => {
    const doc = platform()
    doc.admins = [{ email: 'owner@example.com', role: 'owner', granted_by: 'config:' + 'PLATFORM_OWNER', granted_at: null }]
    await mount(people(doc))
    const card = await waitFor(() => {
      const c = document.querySelector<HTMLElement>('.pp-admins')
      expect(c).not.toBeNull()
      return c as HTMLElement
    }, WAIT)
    // MUTATION: restore 'set by PLATFORM_OWNER in configuration' -> fails.
    expect(visible(card)).not.toMatch(/PLATFORM_OWNER|ADMIN_GROUPS/)
    // The review's minor (2026-10-28): 'platform owner · set by the platform
    // owner setting' repeated itself, and 'holds no admin role' read as if a
    // group admin were not one.
    // MUTATION: restore 'set by the platform owner setting' -> fails.
    expect(visible(card)).toContain('set in the deployment’s configuration')
    expect(visible(card)).not.toContain('set by the platform owner setting')
    expect(visible(card)).toContain('An admin through a group has no role recorded here')
  })
})

describe('admin-shell-20: a request with no limits says what approving sets', () => {
  it('reads "No ceiling was asked for: approving sets the default"', async () => {
    const doc = platform()
    ;(doc.people as Json[])[0] = person('jo@example.com', ws('requested', 'w-0j0000', { limits: {} }), { team: 'none', labs: 'none' })
    await mount(people(doc))
    const row = document.querySelector<HTMLElement>('.pp-waiting li.pp-req[data-kind="workspace"][data-email="jo@example.com"]') as HTMLElement
    // MUTATION: restore "the record carries no limits" -> fails.
    expect(visible(row)).not.toContain('the record carries no limits')
    expect(visible(row)).toContain('No ceiling was asked for: approving sets the default, 8 agents at once')
  })
})
