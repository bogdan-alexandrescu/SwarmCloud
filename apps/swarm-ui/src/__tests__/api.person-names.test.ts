// The person-name fields PROFILE-NAMES-API serves (user-profile.md §4.4)
// arrive through the real loaders, typed: `PersonRow.display_name` from
// `GET /v1/admin/people`, and `decided_by_name` / `approver_names` from
// `GET /v1/approvals/{id}` and the inbox. The reads below use no cast, so a
// field the shared types stop declaring fails the typecheck, and a field a
// loader stops passing through fails here.
//
// MUTATIONS: drop `display_name` from `PersonRow`, or `decided_by_name` /
// `approver_names` from `ApprovalItem` / `ApprovalDetail`; make `loadPeople`
// or `loadApproval` rebuild the row without them.

import { afterEach, describe, expect, it, vi } from 'vitest'

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })
}

async function live(answer: (url: URL) => unknown): Promise<typeof import('../api')> {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => json(answer(new URL(String(input), 'http://ui.test')))) as unknown as typeof fetch
  return import('../api')
}

afterEach(() => vi.unstubAllEnvs())

function person(email: string, extra: Record<string, unknown>) {
  return {
    email,
    teams: [],
    github: 'none',
    workspace: { state: 'approved' },
    claude_account: { state: null },
    loan_request: null,
    first_seen: null,
    last_seen: null,
    last_submitted: null,
    last_active: null,
    ...extra,
  }
}

describe('People rows carry display_name', () => {
  it('passes the name through, and null when the API serves none', async () => {
    const api = await live(() => ({
      people: [person('ada@example.test', { display_name: 'Ada' }), person('bob@example.test', { display_name: null }), person('old@example.test', {})],
      count: 3,
      pending: 0,
      audit: [],
      lendable_accounts: [],
      lendable_error: null,
    }))
    const r = await api.loadPeople()
    expect(r.status).toBe('ok')
    if (r.status !== 'ok') return
    const names = r.data.people.map((row) => row.display_name ?? null)
    expect(names).toEqual(['Ada', null, null])
  })
})

describe('Approvals carry decided_by_name and approver_names', () => {
  const item = {
    approval_id: 'ap_1',
    kind: 'plan',
    subject: { run_id: 'r_1' },
    summary: 'A plan',
    state: 'approved',
    requested_at: null,
    expires_at: null,
  }

  it('passes the detail names through', async () => {
    const api = await live(() => ({
      approval: { ...item, decided_by: 'ada@example.test', decided_by_name: 'Ada', approvers: ['ada@example.test', 'bob@example.test'], approver_names: ['Ada', null] },
    }))
    const r = await api.loadApproval('ap_1')
    expect(r.status).toBe('ok')
    if (r.status !== 'ok') return
    const a = r.data.approval
    expect(a.decided_by_name).toBe('Ada')
    expect(a.approver_names).toEqual(['Ada', null])
  })

  it('reads null when the API serves no names: a rule, an expiry, an older API', async () => {
    const api = await live(() => ({
      approval: { ...item, decided_by: 'approval-expiry', decided_by_name: null, approvers: 'members', approver_names: null },
    }))
    const r = await api.loadApproval('ap_1')
    if (r.status !== 'ok') throw new Error(r.status)
    expect(r.data.approval.decided_by_name ?? null).toBeNull()
    expect(r.data.approval.approver_names ?? null).toBeNull()

    const older = await live(() => ({ approval: item }))
    const o = await older.loadApproval('ap_1')
    if (o.status !== 'ok') throw new Error(o.status)
    expect(o.data.approval.decided_by_name ?? null).toBeNull()
    expect(o.data.approval.approver_names ?? null).toBeNull()
  })

  it('passes approver_names through on the inbox rows', async () => {
    const api = await live(() => ({ approvals: [{ ...item, state: 'pending', approvers: ['ada@example.test'], approver_names: ['Ada'] }] }))
    const r = await api.loadApprovals()
    if (r.status !== 'ok') throw new Error(r.status)
    expect(r.data.approvals.map((a) => a.approver_names)).toEqual([['Ada']])
  })
})
