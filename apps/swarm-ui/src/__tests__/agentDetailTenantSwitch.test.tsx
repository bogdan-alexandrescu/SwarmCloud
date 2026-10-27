// A CONSOLE LINK TO A TASK IN ANOTHER OF YOUR TENANTS OFFERS THE SWITCH
// (#1194 comment 15).
//
// `GET /v1/tasks/{id}` answers the same 404 whether the task does not exist
// or belongs to another tenant (store.py), so a link pasted from a colleague
// in `eng` drew "Not found" under the caller's personal workspace with no way
// out. The drawer now asks, ONLY under the caller's own tenants -- the ones
// `GET /v1/tenants/mine` lists, each with its own `X-Swarm-Tenant` -- whether
// one of them holds the task, and offers the switch without making it.
//
// What is held here, against the real `AgentDetailScreen` and the real fetch
// helpers, with `fetch` mocked by URL and tenant header:
//
//   1. a 404 under u-bogdan and a 200 under eng draws "This task is in eng"
//      and a `Switch to eng` control, which switches and re-reads;
//   2. a 404 under every tenant draws today's copy and no offer;
//   3. no request ever carries a tenant /v1/tenants/mine did not list, and the
//      tenant already chosen is not asked twice.

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
  { tenant_id: 'eng', display_name: 'eng', kind: 'group' },
  { tenant_id: 'ops', display_name: 'ops', kind: 'group' },
]

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
 * tenants in `holders`, and 404 everywhere else. `/v1/tenants/mine` lists MINE.
 */
function serve(holders: readonly string[]) {
  const sent: Sent[] = []
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const path = url.split('?')[0] ?? url
    const tenant = new Headers(init?.headers).get(TENANT_HEADER)
    sent.push({ path, tenant })
    if (path.endsWith('/v1/tenants/mine')) return ok(MINE)
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

describe('a task 404 under the chosen tenant asks the caller’s other tenants', () => {
  it('offers the switch when exactly one other tenant holds the task, and switching re-reads under it', async () => {
    const sent = serve(['eng'])
    render(<AgentDetailScreen taskId={TASK_ID} onClose={() => {}} />)

    const offer = await screen.findByRole('button', { name: 'Switch to eng' }, WAIT)
    expect(document.body.textContent).toContain('This task is in eng')
    // Offered, never made: the choice is still the caller's own.
    expect(chosenTenant()).toBe('u-bogdan')

    fireEvent.click(offer)
    expect(chosenTenant()).toBe('eng')
    // The drawer reads the task again, now under eng, and draws it.
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Switch to eng' })).toBeNull(), WAIT)
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
    // Every other tenant was asked once, before the panel settled.
    await waitFor(() => expect(taskReads(sent).map((s) => s.tenant)).toEqual(['u-bogdan', 'eng', 'ops']), WAIT)
    expect(screen.queryByRole('button', { name: /^Switch to/ })).toBeNull()
    expect(document.body.textContent).not.toContain('This task is in')
    expect(chosenTenant()).toBe('u-bogdan')
  })

  it('never sends a tenant /v1/tenants/mine did not list', async () => {
    const sent = serve(['eng', 'finance'])
    render(<AgentDetailScreen taskId={TASK_ID} onClose={() => {}} />)

    await screen.findByRole('button', { name: 'Switch to eng' }, WAIT)
    const listed = new Set(MINE.map((t) => t.tenant_id))
    const tenants = sent.map((s) => s.tenant)
    expect(tenants.every((t) => t === null || listed.has(t))).toBe(true)
    expect(tenants).not.toContain('finance')
    // The chosen tenant's own 404 is not asked again as a probe.
    expect(taskReads(sent).filter((s) => s.tenant === 'u-bogdan')).toHaveLength(1)
  })
})
