// ADMIN › PEOPLE AND ROLES, JOIN REQUESTS (JOIN-UI-ADMIN; part of #2325;
// docs/design/join-requests.md §4.6 and §6 row 6, config-e.html frame S5f):
// "1 person is waiting to join Example Labs".
//
// WHAT EACH CASE HOLDS:
//   * a workspace admin's lead names who is waiting to join their workspace,
//     and Waiting for you draws the request with Approve and Deny;
//   * the requester's cell in that workspace's column reads `requested`;
//   * a denial cannot be sent without a reason, and the reason is what is sent;
//   * an approval with platform memberships off (`admitted: false`) shows the
//     group command the API answered, and a lowered role is what is sent;
//   * a platform admin's lead and Waiting for you count the join with the
//     personal-workspace request;
//   * a person in a workspace's members list (`GET /v1/workspace/members`,
//     read with that workspace's `X-Swarm-Tenant`) gets the "joined by
//     request" mark in that column, and a Google-group member does not;
//   * a members read that failed draws no mark and says so once.
//
// Placeholders only; nothing here is token-shaped. The request ref is built
// at runtime.

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

const MEMBERS_OK: Json = {
  platform_memberships: true,
  members: [{ email: 'kai@example.com', by: 'sam@example.com', at: ago(60 * 24), via: 'join_request', group_changed: false, still_in_group: false }],
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

describe('a workspace admin answers a request to join their workspace (S5f)', () => {
  it('leads with who is waiting to join, and draws the request on Waiting for you with Approve and Deny', async () => {
    await mount(people(scoped()))
    // MUTATION: keep the old "Nobody is waiting for you" lead -> fails.
    const lead = document.querySelector<HTMLElement>('.adm-lead')
    expect(visible(lead?.querySelector('b') ?? null)).toBe('1 person is waiting to join Example Labs')
    expect(lead?.dataset.lead).toBe('warn')
    const row = joinRow()
    expect(row).not.toBeNull()
    expect(visible(row)).toContain('lee@example.com')
    expect(visible(row)).toContain('to join Example Labs')
    expect(visible(row)).toContain('On the labs rota from Monday')
    const buttons = within(row as HTMLElement).getAllByRole('button').map((b) => b.textContent)
    expect(buttons).toEqual(['Approve', 'Deny'])
    // The lead's one button takes the reader to it.
    fireEvent.click(within(lead as HTMLElement).getByRole('button', { name: 'Review it' }))
    expect(document.activeElement && (row as HTMLElement).contains(document.activeElement)).toBe(true)
  })

  it("draws the requester's cell in that workspace's column as requested", async () => {
    await mount(people(scoped()))
    expect(cell('lee@example.com', 'labs')?.dataset.role).toBe('requested')
    expect(visible(cell('lee@example.com', 'labs'))).toBe('requested')
    // The cell says which request it stands for, as Waiting for you does.
    // MUTATION: draw `requested` as a bare role word -> no title, fails.
    expect(cell('lee@example.com', 'labs')?.querySelector('.is-requested')?.getAttribute('title')).toMatch(/^lee@example\.com asked to join Example Labs 2h ago$/)
    expect(cell('lee@example.com', 'team')?.dataset.role).toBe('none')
  })

  it('sends no denial without a reason, and sends the reason typed', async () => {
    const calls = await mount((m, url, body, tenant) => {
      if (m === 'POST' && url === `/v1/admin/join-requests/${REF}/deny`)
        return { status: 200, body: { ref: REF, state: 'denied', email: 'lee@example.com', tenant_id: 'labs', reason: (body as Json).reason } }
      return people(scoped())(m, url, body, tenant)
    })
    const row = joinRow() as HTMLElement
    fireEvent.click(within(row).getByRole('button', { name: 'Deny' }))
    const send = within(row).getAllByRole('button', { name: 'Deny' }).at(-1) as HTMLButtonElement
    // MUTATION: drop the blank-reason guard -> the button is enabled and a POST is sent.
    expect(send.disabled).toBe(true)
    fireEvent.click(send)
    expect(calls.some((c) => c.url.endsWith('/deny'))).toBe(false)
    fireEvent.change(within(row).getByLabelText('Reason, shown to lee@example.com'), { target: { value: '  Not on the labs rota  ' } })
    fireEvent.click(within(row).getAllByRole('button', { name: 'Deny' }).at(-1) as HTMLButtonElement)
    await waitFor(() => expect(calls.find((c) => c.url.endsWith('/deny'))?.body).toEqual({ reason: 'Not on the labs rota' }), WAIT)
    await screen.findByText(/Denied lee@example\.com’s request to join Example Labs/, undefined, WAIT)
  })

  it('shows the group command when the approval admitted nobody (platform memberships off)', async () => {
    const command = 'gcloud identity groups memberships add --group-email=labs@example.com --member-email=lee@example.com'
    const calls = await mount((m, url, body, tenant) => {
      if (m === 'POST' && url === `/v1/admin/join-requests/${REF}/approve`)
        return {
          status: 200,
          body: { ref: REF, state: 'approved', email: 'lee@example.com', tenant_id: 'labs', role: 'member', admitted: false, group_change: { group: 'labs@example.com', command } },
        }
      return people(scoped())(m, url, body, tenant)
    })
    fireEvent.click(within(joinRow() as HTMLElement).getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(calls.find((c) => c.url.endsWith('/approve'))).toBeDefined(), WAIT)
    // The default role is no role in the body: the API gives the default.
    expect(calls.find((c) => c.url.endsWith('/approve'))?.body).toEqual({})
    // MUTATION: say "Approved" and drop `group_change` -> the command is missing.
    const code = await screen.findByText(command, undefined, WAIT)
    expect(code.tagName).toBe('CODE')
    expect(visible(document.querySelector('.pp-waiting'))).toContain('waiting for the group change')
  })

  it('sends viewer when the approving admin lowers the role', async () => {
    const calls = await mount((m, url, body, tenant) => {
      if (m === 'POST' && url === `/v1/admin/join-requests/${REF}/approve`)
        return { status: 200, body: { ref: REF, state: 'approved', email: 'lee@example.com', tenant_id: 'labs', role: 'viewer', admitted: true, group_change: null } }
      return people(scoped())(m, url, body, tenant)
    })
    const row = joinRow() as HTMLElement
    const pick = within(row).getByLabelText('Role for lee@example.com') as HTMLSelectElement
    // The workspace default is preselected; viewer is the one below it.
    expect(pick.value).toBe('member')
    expect([...pick.options].map((o) => o.value)).toEqual(['member', 'viewer'])
    fireEvent.change(pick, { target: { value: 'viewer' } })
    fireEvent.click(within(row).getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(calls.find((c) => c.url.endsWith('/approve'))?.body).toEqual({ role: 'viewer' }), WAIT)
    await screen.findByText(/lee@example\.com joined Example Labs as viewer/, undefined, WAIT)
  })
})

describe('a platform admin sees the join beside the other requests', () => {
  it('counts it in the lead and draws it on Waiting for you', async () => {
    await mount(people(platform()))
    expect(visible(document.querySelector('.adm-lead b'))).toBe('2 requests are waiting for an admin')
    const row = joinRow()
    expect(row).not.toBeNull()
    expect(visible(row)).toContain('to join Example Labs')
    expect(document.querySelector('.pp-waiting li.pp-req[data-kind="workspace"][data-email="jo@example.com"]')).not.toBeNull()
  })
})

describe('the "joined by request" mark is drawn from the members list (§4.6)', () => {
  it('marks a person in a workspace’s members list in that column, and not a Google-group member', async () => {
    const calls = await mount(
      people(scoped(), (tenant) => (tenant === 'labs' ? { status: 200, body: MEMBERS_OK } : { status: 200, body: { platform_memberships: true, members: [] } })),
    )
    await waitFor(() => expect(cell('kai@example.com', 'labs')?.querySelector('.pp-joined')).not.toBeNull(), WAIT)
    // Each workspace's list is read with that workspace's tenant header.
    expect(calls.filter((c) => c.url === '/v1/workspace/members').map((c) => c.tenant).sort()).toEqual(['labs', 'team'])
    expect(visible(cell('kai@example.com', 'labs'))).toBe('member joined by request')
    // MUTATION: mark every member of the column -> riley (in the Google group) gains it.
    expect(cell('riley@example.com', 'labs')?.querySelector('.pp-joined')).toBeNull()
    expect(cell('kai@example.com', 'team')?.querySelector('.pp-joined')).toBeNull()
    expect(document.querySelector('.pp-joined-unread')).toBeNull()
  })

  it('draws no mark for a workspace whose members read failed, and says so once', async () => {
    await mount(
      people(scoped(), (tenant) =>
        tenant === 'labs'
          ? { status: 503, body: { code: 'membership_unavailable', message: 'this workspace’s memberships could not be read; retry shortly' } }
          : { status: 200, body: { platform_memberships: true, members: [] } },
      ),
    )
    // MUTATION: guess a mark from the cell, or say nothing -> fails.
    await waitFor(() => expect(document.querySelectorAll('.pp-joined-unread')).toHaveLength(1), WAIT)
    const note = visible(document.querySelector('.pp-joined-unread'))
    expect(note).toContain('Example Labs')
    // In words, never the code (QA1-FIX-17, admin-shell-05).
    expect(note).not.toContain('membership_unavailable')
    expect(note).toContain('its members list could not be read')
    expect(note).not.toContain('Example Team')
    expect(document.querySelector('.pp-joined')).toBeNull()
  })
})
