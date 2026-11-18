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

// THE DEVICE SIGN-IN CALLS (the `openai` plan's add, routes/accounts.py's
// `/device/start` and `/device/poll`): what each sends, and the answer
// normalised as the API serves it.
//
// BREAK IT: send a provider or an owner on start, open a non-https device
// page, read a poll's unknown status as pending, or drop the poll's own
// interval -- a case below turns red.
describe('the device sign-in calls', () => {
  const STATE = 'device-state-0001'

  it('beginDeviceSignIn posts the label and lends to /device/start, nothing else', async () => {
    const api = await live()
    sent.write.mockResolvedValue({
      status: 'ok',
      fetchedAt: 0,
      data: { state: STATE, user_code: 'ABCD-1234', verification_uri: 'https://device.example.com/activate', expires_in_seconds: 900, interval_seconds: 7, provider: 'openai' },
    })
    const r = await api.beginDeviceSignIn({ label: 'chat-one', lend_to: [] })
    expect(call().method).toBe('POST')
    expect(call().url).toContain('/v1/accounts/device/start')
    expect(call().body).toEqual({ label: 'chat-one', lend_to: [] })
    expect(r).toEqual({
      status: 'ok',
      fetchedAt: 0,
      data: { state: STATE, user_code: 'ABCD-1234', verification_uri: 'https://device.example.com/activate', expires_in_seconds: 900, interval_seconds: 7 },
    })
  })

  it('refuses a start answer without a code or with a device page that is not https', async () => {
    const api = await live()
    sent.write.mockResolvedValue({ status: 'ok', fetchedAt: 0, data: { state: STATE, verification_uri: 'https://device.example.com/activate', interval_seconds: 5 } })
    expect((await api.beginDeviceSignIn({ label: 'chat-one', lend_to: [] })).status).toBe('error')
    sent.write.mockResolvedValue({ status: 'ok', fetchedAt: 0, data: { state: STATE, user_code: 'ABCD-1234', verification_uri: 'javascript:alert(1)', interval_seconds: 5 } })
    expect((await api.beginDeviceSignIn({ label: 'chat-one', lend_to: [] })).status).toBe('error')
  })

  it('passes a failed start through as its error', async () => {
    const api = await live()
    const refused = { status: 'error', error: { kind: 'forbidden', httpStatus: 403, code: null, message: 'not a writer' } }
    sent.write.mockResolvedValue(refused)
    expect(await api.beginDeviceSignIn({ label: 'chat-one', lend_to: [] })).toEqual(refused)
  })

  it('pollDeviceSignIn posts the state to /device/poll and reads pending with its interval, expired, and filed with the plan', async () => {
    const api = await live()
    sent.write.mockResolvedValueOnce({ status: 'ok', fetchedAt: 0, data: { status: 'pending', interval_seconds: 10 } })
    const pending = await api.pollDeviceSignIn({ state: STATE })
    expect(call().method).toBe('POST')
    expect(call().url).toContain('/v1/accounts/device/poll')
    expect(call().body).toEqual({ state: STATE })
    expect(pending).toEqual({ status: 'ok', fetchedAt: 0, data: { status: 'pending', interval_seconds: 10 } })

    sent.write.mockResolvedValueOnce({ status: 'ok', fetchedAt: 0, data: { status: 'pending', interval_seconds: null } })
    expect(await api.pollDeviceSignIn({ state: STATE })).toEqual({ status: 'ok', fetchedAt: 0, data: { status: 'pending', interval_seconds: null } })

    sent.write.mockResolvedValueOnce({ status: 'ok', fetchedAt: 0, data: { status: 'expired' } })
    expect(await api.pollDeviceSignIn({ state: STATE })).toEqual({ status: 'ok', fetchedAt: 0, data: { status: 'expired' } })

    const account = { account_id: 'eng:chat-one', label: 'chat-one', provider: 'openai' }
    sent.write.mockResolvedValueOnce({ status: 'ok', fetchedAt: 0, data: { status: 'filed', account, registered: null, organisation: null, expires_at: null, note: 'n' } })
    expect(await api.pollDeviceSignIn({ state: STATE })).toEqual({ status: 'ok', fetchedAt: 0, data: { status: 'filed', account, expires_at: null } })
  })

  it('refuses a poll answer whose status it does not know', async () => {
    const api = await live()
    sent.write.mockResolvedValue({ status: 'ok', fetchedAt: 0, data: { status: 'maybe' } })
    expect((await api.pollDeviceSignIn({ state: STATE })).status).toBe('error')
  })

  it('in fixture mode serves a code, then a filed plan the list then holds', async () => {
    vi.resetModules()
    const api = await import('../api')
    expect(api.USE_FIXTURES).toBe(true)
    const start = await api.beginDeviceSignIn({ label: 'chat-new', lend_to: [] })
    expect(start.status).toBe('ok')
    if (start.status !== 'ok') return
    expect(start.data.user_code).toMatch(/^[A-Z0-9]{4}-[A-Z0-9]{4}$/)
    expect(start.data.verification_uri).toMatch(/^https:\/\//)
    expect(start.data.interval_seconds).toBeGreaterThan(0)
    let polled = await api.pollDeviceSignIn({ state: start.data.state })
    for (let i = 0; i < 3 && polled.status === 'ok' && polled.data.status === 'pending'; i++) polled = await api.pollDeviceSignIn({ state: start.data.state })
    expect(polled.status === 'ok' && polled.data.status).toBe('filed')
    const board = await api.loadAccountsBoard()
    const rows = board.status === 'ok' ? board.data.page.accounts : []
    expect(rows.filter((a) => a.label === 'chat-new').map((a) => a.provider)).toEqual(['openai'])
  }, 15000)
})
