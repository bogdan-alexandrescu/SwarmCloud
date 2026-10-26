// THE PLATFORM HALF OF /v1/stats IS ASKED FOR BY THE RUN BUTTON ONLY (#2149).
//
// THE DEFECT. The shell polls `GET /v1/stats` every 30 s for the panel's Live
// and Waiting counts, and the API ran the platform half for every admin read:
// twelve `count()`s over every tenant per poll, for as long as an admin tab
// stayed open. Platform counts' decided rule (owner ruling 2026-10-07, #138)
// is that the platform count is a priced button and never auto-refreshes. The
// API now runs that half only on `?scope=platform`; these check that the shell
// never sends it and that the page's Run does.
//
// The live path, not the development fixtures: USE_FIXTURES is decided once at
// import time, so the modules are loaded fresh after the stub.

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

function stubFetch(isAdmin: boolean): string[] {
  const calls: string[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    calls.push(url)
    // THE OVER-TIME READ, which the page's one Run also starts for an admin
    // (COUNTS-ONE-RUN): refused here, since this file is about /v1/stats.
    if (url.startsWith('/v1/outcomes')) {
      return new Response(JSON.stringify({ detail: 'not in this test' }), { status: 503, headers: { 'content-type': 'application/json' } })
    }
    const body = url.startsWith('/v1/tenants/me')
      ? {
          tenant: { tenant_id: 'eng' },
          principal: { email: 'a@example.com', domain: 'example.com', groups: [], is_admin: isAdmin },
          environment: 'dev',
          environment_declared: false,
        }
      : {
          tenant_id: 'eng',
          tasks_by_state: { RUNNING: 1 },
          ...(url.includes('scope=platform') ? { platform_tasks_by_state: { RUNNING: 4 } } : {}),
          dispatch_paused: false,
          limits: {},
          generated_at: '2026-10-23T10:00:00Z',
        }
    return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })
  }) as unknown as typeof fetch
  return calls
}

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('the platform half of /v1/stats (#2149)', () => {
  it('is never asked for by the shell’s 30-second poll', async () => {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const calls = stubFetch(true)
    const { loadFrameStats } = await import('../Spine')
    await loadFrameStats()
    const reads = calls.filter((c) => c.startsWith('/v1/stats'))
    expect(reads, 'the poll never read /v1/stats, so this test checked nothing').toHaveLength(1)
    // MUTATION: make the frame read ask `{ frame: true, platform: true }`, or
    // default `loadStats` to the platform scope, and this sees the parameter.
    expect(reads[0]).toBe('/v1/stats')
  })

  it('is asked for by Platform counts’ Run, and only on the press', async () => {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const calls = stubFetch(true)
    const { PlatformCountsScreen } = await import('../PlatformCounts')
    render(<PlatformCountsScreen />)
    // Still a priced button: nothing is counted until it is pressed.
    expect(calls.filter((c) => c.startsWith('/v1/stats'))).toHaveLength(0)
    fireEvent.click(screen.getByRole('button', { name: /^Run the count · / }))
    await waitFor(() => expect(calls.filter((c) => c.startsWith('/v1/stats'))).toHaveLength(1))
    // MUTATION: call `loadStats()` from Run, without `{ platform: true }`, and
    // the Every tenant card gets no counts from the API.
    expect(calls.filter((c) => c.startsWith('/v1/stats'))[0]).toBe('/v1/stats?scope=platform')
    await waitFor(() => expect(document.querySelectorAll('.split-row').length).toBeGreaterThan(0))
  })
})
