// THE AGENTS LIST NEEDS A HEARTBEAT IT CAN READ AS A MEMBER (#179).
//
// A worker's heartbeat lives on its LEASE, not its task, and the only route
// that served one was `/v1/admin/leases`, so a member's list could draw no
// "silent worker" line. `loadLeaseHeartbeats` reads the tenant-scoped
// `GET /v1/leases` instead, and `silentWorkersByTask` is what a list row looks
// itself up in.
//
// The live path is driven over a stubbed `fetch`, as holders.read.test.ts
// does, because the claim is about WHICH route is read: a mock of the loader
// would agree with whatever the screen assumed.

import { afterEach, describe, expect, it, vi } from 'vitest'

import { CONCURRENCY_STATES, silentWorkersByTask, type LeaseHeartbeat, type LeaseHeartbeatPage } from '../types'

function beat(taskId: string, extra: Partial<LeaseHeartbeat> = {}): LeaseHeartbeat {
  return {
    task_id: taskId,
    lease_id: `lease_${taskId}`,
    attempt_id: `att_${taskId}`,
    generation: 1,
    task_state: 'RUNNING',
    dispatch_state: 'DISPATCHED',
    created_at: '2026-10-02T10:00:00Z',
    dispatch_deadline: '2026-10-02T10:08:00Z',
    expires_at: '2026-10-02T10:30:00Z',
    heartbeat_at: '2026-10-02T10:14:50Z',
    silent_seconds: 10,
    silent: false,
    expired: false,
    dispatch_overdue: false,
    ...extra,
  }
}

function page(heartbeats: LeaseHeartbeat[]): LeaseHeartbeatPage {
  return {
    tenant_id: 'eng',
    read_at: '2026-10-02T10:15:00Z',
    thresholds: { heartbeat_grace_seconds: 90, lease_timeout_seconds: 120 },
    heartbeats,
  }
}

function serve(body: unknown): string[] {
  const calls: string[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    calls.push(String(input))
    return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })
  }) as unknown as typeof fetch
  return calls
}

/** The live path, not the development fixtures: USE_FIXTURES is decided at import. */
async function liveApi() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../api')
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.resetModules()
})

describe('loadLeaseHeartbeats', () => {
  it('reads the tenant-scoped route, never the admin one', async () => {
    const calls = serve(page([beat('task_a')]))
    const api = await liveApi()
    const r = await api.loadLeaseHeartbeats()
    expect(calls).toEqual(['/v1/leases'])
    expect(calls.some((c) => c.includes('/v1/admin/')), 'a member is refused the admin route').toBe(false)
    expect(r.status).toBe('ok')
    if (r.status !== 'ok') throw new Error('unreachable')
    expect(r.data.heartbeats.map((h) => h.task_id)).toEqual(['task_a'])
  })

  it('is empty when no task of the tenant holds a slot', async () => {
    serve(page([]))
    const api = await liveApi()
    const r = await api.loadLeaseHeartbeats()
    expect(r.status).toBe('empty')
  })
})

describe('silentWorkersByTask', () => {
  it('names a silent worker and not a healthy one', () => {
    const quiet = beat('task_quiet', { silent: true, silent_seconds: 400, heartbeat_at: '2026-10-02T10:08:20Z' })
    const map = silentWorkersByTask(page([quiet, beat('task_well')]))
    expect([...map.keys()]).toEqual(['task_quiet'])
    expect(map.get('task_quiet')).toBe(quiet)
  })

  it('never calls a worker that has not beaten yet silent', () => {
    // A booting worker: no heartbeat, so no silence to measure -- the null
    // stays null rather than becoming the time since admission.
    const booting = beat('task_new', { heartbeat_at: null, silent_seconds: null, task_state: 'DISPATCHED' })
    expect(silentWorkersByTask(page([booting])).size).toBe(0)
  })
})

describe('the development fixture', () => {
  it('serves each treatment, joined to tasks in the concurrency states', async () => {
    expect(import.meta.env.DEV, 'this case reads the fixture build').toBeTruthy()
    expect(import.meta.env.VITE_LIVE, 'VITE_LIVE turns the fixtures off').toBeFalsy()
    const api = await import('../api')
    const r = await api.loadLeaseHeartbeats()
    expect(r.status).toBe('ok')
    if (r.status !== 'ok') throw new Error('unreachable')
    const rows = r.data.heartbeats
    expect(rows.length).toBeGreaterThan(0)
    expect(rows.every((h) => CONCURRENCY_STATES.has(h.task_state))).toBe(true)
    expect(rows.some((h) => h.silent)).toBe(true)
    expect(rows.some((h) => !h.silent && h.heartbeat_at !== null)).toBe(true)
    // Only `silent` decides a warning; a never-beaten row is never silent.
    const never = rows.filter((h) => h.heartbeat_at === null)
    expect(never.length, 'the never-beaten treatment is reachable').toBeGreaterThan(0)
    expect(never.every((h) => !h.silent && h.silent_seconds === null)).toBe(true)

    const tasks = await api.loadTasks()
    if (tasks.status !== 'ok') throw new Error('the task fixture did not load')
    const ids = new Set(tasks.data.tasks.map((t) => t.id))
    expect(rows.every((h) => ids.has(h.task_id)), 'a heartbeat for no listed task exercises nothing').toBe(true)
  })
})
