// THE PLAN CAP CALLS, as api.ts sends them on a live build (docs/design/
// llm-providers.md §4.6): `setAccountLending` sends today's bare `lend_to`
// for a list and `lends`, each workspace with its agents cap, for a map;
// `setAccountPerWorkspaceMax` sends the whole per-workspace map.
//
// BREAK IT: send a map as `lend_to`, drop a cap, or point the per-workspace
// call at another route -- a case below turns red.

import { afterEach, describe, expect, it, vi } from 'vitest'

const sent = vi.hoisted(() => ({ write: vi.fn() }))

vi.mock('../fetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../fetch')>()),
  write: sent.write,
}))

afterEach(() => {
  vi.unstubAllEnvs()
  sent.write.mockReset()
})

async function live() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  sent.write.mockResolvedValue({ status: 'ok', fetchedAt: 0, data: {} })
  return import('../api')
}

function call(i = 0): { url: string; method: string; body: unknown } {
  const [target, method, body] = sent.write.mock.calls[i]!
  return { url: (target as { url: string }).url, method: String(method), body }
}

describe('the plan cap calls', () => {
  it('setAccountLending sends a list as lend_to, as it always did', async () => {
    const api = await live()
    await api.setAccountLending('u-owner:full-six', ['smoke'])
    expect(call().method).toBe('PUT')
    expect(call().url).toContain('/v1/accounts/u-owner%3Afull-six/lending')
    expect(call().body).toEqual({ lend_to: ['smoke'] })
  })

  it('setAccountLending sends a map as lends, each with its agents cap', async () => {
    const api = await live()
    await api.setAccountLending('u-owner:full-six', { smoke: 2, harbor: 1 })
    expect(call().body).toEqual({ lends: { smoke: { max_agents: 2 }, harbor: { max_agents: 1 } } })
  })

  it('setAccountPerWorkspaceMax sends the whole map to its route', async () => {
    const api = await live()
    await api.setAccountPerWorkspaceMax('u-owner:full-six', { 'u-owner': 3, smoke: 1 })
    expect(call().method).toBe('PUT')
    expect(call().url).toContain('/v1/accounts/u-owner%3Afull-six/per-workspace-max')
    expect(call().body).toEqual({ per_workspace_max: { 'u-owner': 3, smoke: 1 } })
  })
})
