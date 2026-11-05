// A CONSOLE LINK TO A TASK IN ANOTHER OF YOUR WORKSPACES OFFERS THE SWITCH
// (#1194 comment 15; the owner's pick of 2026-11-04, "offer to switch",
// elsewhere.tsx).
//
// `GET /v1/tasks/{id}` answers the same 404 whether the task does not exist
// or belongs to another workspace (store.py), so a link pasted from a
// colleague in Engineering drew "Not found" under the caller's personal
// workspace with no way out. The drawer now asks each OTHER workspace the
// person is a member of -- the profile's list, never a platform admin's reach
// -- one read each, in parallel, with that workspace's own `X-Swarm-Tenant`,
// and offers the switch without making it.
//
// What is held here, against the real `AgentDetailScreen` and the real fetch
// helpers, with `fetch` mocked by URL and tenant header:
//
//   1. a 404 under u-bogdan and a 200 under eng draws "This agent is in
//      Engineering, not Your workspace." and a `Switch to Engineering`
//      control, which switches and re-reads;
//   2. a 404 under every workspace draws today's copy and no offer;
//   3. no request ever carries a workspace the profile did not list, and the
//      workspace already chosen is not asked twice;
//   4. a read that fails is said, never taken for an absence.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

// The LIVE read path: USE_FIXTURES is decided when api.ts loads, so the switch
// is flipped before any import of it.
vi.hoisted(() => {
  vi.stubEnv('VITE_LIVE', '1')
})

const { AgentDetailScreen } = await import('../AgentDetail')
const { TENANT_HEADER, TENANT_PREF, chosenTenant, forgetReads } = await import('../fetch')

const TASK_ID = 'task_5c0e9a41d2b74f6e8a13'
const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }

const MINE = [
  { tenant_id: 'u-bogdan', display_name: 'Your workspace (u-bogdan)', kind: 'user' },
  { tenant_id: 'eng', display_name: 'Engineering', group: 'eng@example.com', kind: 'group' },
  { tenant_id: 'ops', display_name: 'Operations', group: 'ops@example.com', kind: 'group' },
  { tenant_id: 'finance', display_name: 'Finance', group: 'finance@example.com', kind: 'group', via: 'platform_admin' },
]

/** `GET /v1/profile`'s workspaces: memberships only, a group's row named by its email. */
function profile(acting: string) {
  const row = (tenant_id: string, display_name: string, kind: string) => ({
    tenant_id,
    display_name,
    kind,
    role: { name: 'member', source: 'derived' },
    state: null,
    selectable: true,
    is_acting: tenant_id === acting,
    is_default: false,
  })
  return {
    identity: { email: 'bogdan@example.com' },
    workspaces: [row('u-bogdan', 'Your workspace (u-bogdan)', 'user'), row('eng', 'eng@example.com', 'group'), row('ops', 'ops@example.com', 'group')],
    default_workspace: { tenant_id: 'u-bogdan', source: 'personal', ignored: null },
    connections: {},
    revision: 1,
    unavailable: [],
  }
}

const TASK = {
  id: TASK_ID,
  tenant_id: 'eng',
  state: 'SUCCEEDED',
  runner_profile: 'claude-code',
  resource_class: 'standard',
  provider: 'anthropic',
  priority: 5,
  created_at: new Date(Date.now() - 600_000).toISOString(),
  updated_at: new Date(Date.now() - 60_000).toISOString(),
  started_at: new Date(Date.now() - 500_000).toISOString(),
  completed_at: new Date(Date.now() - 60_000).toISOString(),
  attempt_count: 1,
  input: {},
}

const notFound = () =>
  new Response(JSON.stringify({ code: 'not_found', message: 'Task not found.' }), { status: 404, headers: JSON_HEADERS })
const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: JSON_HEADERS })

interface Sent {
  path: string
  tenant: string | null
}

/**
 * `fetch`, answering `/v1/tasks/{id}` (and its sub-reads) 200 only under the
 * tenants in `holders`, 503 under `failing`, and 404 everywhere else.
 * `/v1/tenants/mine` lists MINE; `/v1/profile` the memberships.
 */
function serve(holders: readonly string[], failing: readonly string[] = []) {
  const sent: Sent[] = []
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const path = url.split('?')[0] ?? url
    const tenant = new Headers(init?.headers).get(TENANT_HEADER)
    sent.push({ path, tenant })
    if (path.endsWith('/v1/tenants/mine')) return ok(MINE)
    if (path.endsWith('/v1/profile')) return ok(profile(tenant ?? 'u-bogdan'))
    if (tenant !== null && failing.includes(tenant) && path.endsWith(`/v1/tasks/${TASK_ID}`)) {
      return new Response(JSON.stringify({ code: 'unavailable', message: 'Firestore did not answer.' }), { status: 503, headers: JSON_HEADERS })
    }
    const held = tenant !== null && holders.includes(tenant)
    if (path.endsWith(`/v1/tasks/${TASK_ID}`)) return held ? ok({ task: TASK }) : notFound()
    if (path.endsWith(`/v1/tasks/${TASK_ID}/events`)) return held ? ok({ events: [] }) : notFound()
    if (path.endsWith(`/v1/tasks/${TASK_ID}/attempts`)) return held ? ok({ attempts: [] }) : notFound()
    return notFound()
  })
  vi.stubGlobal('fetch', fn)
  return sent
}

const taskReads = (sent: readonly Sent[]) => sent.filter((s) => s.path.endsWith(`/v1/tasks/${TASK_ID}`))

beforeEach(() => {
  forgetReads()
  window.sessionStorage.setItem(TENANT_PREF, 'u-bogdan')
})

afterEach(() => {
  cleanup()
  forgetReads()
  window.sessionStorage.clear()
})

describe('a task 404 under the chosen workspace asks the person’s other workspaces', () => {
  it('offers the switch when exactly one other tenant holds the task, and switching re-reads under it', async () => {
    const sent = serve(['eng'])
    render(<AgentDetailScreen taskId={TASK_ID} onClose={() => {}} />)

    const offer = await screen.findByRole('button', { name: 'Switch to Engineering' }, WAIT)
    expect(document.body.textContent).toContain('This agent is in Engineering, not Your workspace.')
    expect(document.body.textContent).not.toContain('eng@example.com')
    // A separate notice under the failed panel, not fused to its border.
    const notice = offer.closest('.tenant-switch-offer') as HTMLElement | null
    expect(notice?.style.marginTop).toBe('var(--ctl-s3)')
    // Offered, never made: the choice is still the caller's own.
    expect(chosenTenant()).toBe('u-bogdan')

    fireEvent.click(offer)
    expect(chosenTenant()).toBe('eng')
    // The drawer reads the task again, now under eng, and draws it.
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Switch to Engineering' })).toBeNull(), WAIT)
    await waitFor(
      () => expect(sent.some((s) => s.path.endsWith(`/v1/tasks/${TASK_ID}/events`) && s.tenant === 'eng')).toBe(true),
      WAIT,
    )
  })

  it('keeps today’s copy, and offers nothing, when no tenant holds it', async () => {
    const sent = serve([])
    render(<AgentDetailScreen taskId={TASK_ID} onClose={() => {}} />)

    await waitFor(
      () => expect(document.body.textContent).toContain('It does not exist, or it belongs to another tenant'),
      WAIT,
    )
    // Every other workspace was asked once.
    await waitFor(() => expect([...taskReads(sent).map((s) => s.tenant)].sort()).toEqual(['eng', 'ops', 'u-bogdan']), WAIT)
    await waitFor(() => expect(document.querySelector('[data-elsewhere]')).toBeNull(), WAIT)
    expect(screen.queryByRole('button', { name: /^Switch to/ })).toBeNull()
    expect(document.body.textContent).not.toContain('This agent is in')
    expect(chosenTenant()).toBe('u-bogdan')
  })

  it('never sends a workspace the profile did not list, nor a platform admin’s reach', async () => {
    const sent = serve(['eng', 'finance'])
    render(<AgentDetailScreen taskId={TASK_ID} onClose={() => {}} />)

    await screen.findByRole('button', { name: 'Switch to Engineering' }, WAIT)
    const listed = new Set(['u-bogdan', 'eng', 'ops'])
    const tenants = sent.map((s) => s.tenant)
    expect(tenants.every((t) => t === null || listed.has(t))).toBe(true)
    expect(tenants).not.toContain('finance')
    // The chosen tenant's own 404 is not asked again as a probe.
    expect(taskReads(sent).filter((s) => s.tenant === 'u-bogdan')).toHaveLength(1)
  })

  it('says a failed read plainly, and offers the workspace it could be in', async () => {
    serve([], ['ops'])
    const closed = vi.fn()
    render(<AgentDetailScreen taskId={TASK_ID} onClose={closed} />)

    await waitFor(() => expect(document.body.textContent).toContain('Could not tell which of your workspaces holds this agent.'), WAIT)
    expect(document.body.textContent).toContain('Reading it in Operations failed')
    expect(chosenTenant()).toBe('u-bogdan')
    fireEvent.click(screen.getByRole('button', { name: 'Back to the list' }))
    expect(closed).toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Operations' }))
    expect(chosenTenant()).toBe('ops')
  })
})
