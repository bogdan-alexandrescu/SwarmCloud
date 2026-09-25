// WHEN THE HOLDERS READ MAY CALL ITSELF EMPTY (CP-7, #85).
//
// `empty` is drawn as a real zero -- "no lease holds capacity" -- and it used
// to be decided by the LEASE read alone. The capacity read was issued, and
// then discarded whenever the lease page came back with no rows. So a counter
// that leaked with zero live leases behind it -- the exact drift the Holders
// screen exists to find -- rendered as the calmest thing on it.
//
// These drive the real `loadHolders` over a stubbed `fetch`, the way
// `api.events.test.ts` does, because the defect is in which read decides and
// a mock of `loadHolders` would agree with whatever the screen assumed.

import { describe, expect, it, vi } from 'vitest'

const EMPTY_LIVE_PAGE = {
  leases: [],
  thresholds: { heartbeat_grace_seconds: 90, lease_timeout_seconds: 120 },
  evaluated_at: '2026-09-25T10:00:00Z',
  active_only: true,
  tenant_id: null,
  units_held: 0,
  // Every live lease is in this (empty) page: nothing was left out.
  active_beyond_window: 0,
  truncated: false,
  examined: 0,
}

function pool(name: string, active: number) {
  return {
    name,
    hard_limit: 40,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: 40,
    active,
    available: 40 - active,
    enabled: true,
    updated_at: '2026-09-25T10:00:00Z',
  }
}

function capacity(pools: ReturnType<typeof pool>[]) {
  return { pools, runner_profiles: {}, tenant_id: 'eng', generated_at: '2026-09-25T10:00:00Z' }
}

/** A `fetch` that answers the two routes, or fails the capacity one. */
function serve(leases: unknown, cap: unknown | 'fail'): string[] {
  const calls: string[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    calls.push(url)
    if (url.startsWith('/v1/capacity')) {
      if (cap === 'fail') {
        return new Response(JSON.stringify({ code: 'unavailable', message: 'Firestore did not answer.' }), {
          status: 503,
          headers: { 'content-type': 'application/json' },
        })
      }
      return new Response(JSON.stringify(cap), { status: 200, headers: { 'content-type': 'application/json' } })
    }
    return new Response(JSON.stringify(leases), { status: 200, headers: { 'content-type': 'application/json' } })
  }) as unknown as typeof fetch
  return calls
}

/** The live path, not the development fixtures: USE_FIXTURES is decided at import. */
async function liveApi() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../api')
}

describe('loadHolders', () => {
  it('is empty when no lease is live AND every counter reads zero', async () => {
    const calls = serve(EMPTY_LIVE_PAGE, capacity([pool('global', 0), pool('tenant:eng', 0)]))
    const api = await liveApi()
    const r = await api.loadHolders()
    expect(calls.some((c) => c.startsWith('/v1/capacity')), 'the counters were never read').toBe(true)
    expect(r.status).toBe('empty')
  })

  it('is NOT empty when a counter holds units no live lease accounts for', async () => {
    serve(EMPTY_LIVE_PAGE, capacity([pool('global', 0), pool('tenant:eng', 2)]))
    const api = await liveApi()
    const r = await api.loadHolders()
    expect(r.status, 'a leaked counter was reported as a real zero').toBe('ok')
    if (r.status !== 'ok') return
    expect(r.data.page.leases).toHaveLength(0)
    // The counters travel with the page, so the drift card can compare them.
    expect(r.data.pools?.find((p) => p.name === 'tenant:eng')?.active).toBe(2)
  })

  it('is NOT empty when the counters could not be read', async () => {
    // No lease is live, and nobody knows whether a counter is holding units:
    // that is not a zero, it is a comparison that was not made.
    serve(EMPTY_LIVE_PAGE, 'fail')
    const api = await liveApi()
    const r = await api.loadHolders()
    expect(r.status).toBe('ok')
    if (r.status !== 'ok') return
    expect(r.data.pools).toBeNull()
    expect(r.data.poolsDetail).toContain('Firestore did not answer.')
  })
})
