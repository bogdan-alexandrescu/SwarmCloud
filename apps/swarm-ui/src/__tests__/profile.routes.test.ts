// SETTINGS › PROFILE'S TWO ROUTES (lane PROFILE-PAGE, part of #1505;
// docs/design/user-profile.md §4.2). `loadProfile` reads GET /v1/profile and
// `putPreferences` writes PUT /v1/profile/preferences, which PROFILE-API
// (#1544) serves. test_runtimes_screen.py::test_every_v1_path_the_ui_calls_is_served_by_this_api
// holds the same two paths to swarm-api's routers from the other side.
//
// Fixtures only; no network.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { serve } from './repofixture'

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../api')
}

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('the profile routes', () => {
  it('loadProfile reads GET /v1/profile, with no query naming a person', async () => {
    const calls = serve((m, url) => (m === 'GET' && url === '/v1/profile' ? { status: 200, body: { identity: { email: 'ada@example.com' }, unavailable: ['github'] } } : null))
    const { loadProfile } = await load()
    const r = await loadProfile()
    expect(calls.map((c) => [c.method, c.url])).toEqual([['GET', '/v1/profile']])
    expect(r.status).toBe('ok')
    if (r.status !== 'ok') return
    expect(r.data.identity.email).toBe('ada@example.com')
    // An absent section stays absent: it is never filled in as "none".
    expect(r.data.connections.github).toBeUndefined()
    expect(r.data.claude_accounts).toBeUndefined()
    expect(r.data.unavailable).toEqual(['github'])
  })

  it('putPreferences writes PUT /v1/profile/preferences with the whole set and its revision', async () => {
    const body = { display_name: 'Ada Lovelace', default_workspace: null, notifications: { muted: ['finished' as const], reminder: 'daily' as const }, revision: 4 }
    const calls = serve((m, url) =>
      m === 'PUT' && url === '/v1/profile/preferences'
        ? {
            status: 200,
            body: {
              identity: { display_name: 'Ada Lovelace', display_name_suggested: null },
              default_workspace: { tenant_id: 'u-ada', source: 'personal', ignored: null },
              notifications: { preferences: { muted: ['finished'], reminder: 'daily' }, topics: ['finished'] },
              revision: 5,
            },
          }
        : null,
    )
    const { putPreferences } = await load()
    const r = await putPreferences(body)
    expect(calls.map((c) => [c.method, c.url, c.body])).toEqual([['PUT', '/v1/profile/preferences', body]])
    expect(r.status).toBe('ok')
    if (r.status === 'ok') expect(r.data.revision).toBe(5)
  })

  it('putPreferences hands back a 409 as preferences_changed, for the page to read again', async () => {
    serve((m) => (m === 'PUT' ? { status: 409, body: { code: 'preferences_changed', message: 'Your preferences changed since you read them.' } } : null))
    const { putPreferences } = await load()
    const r = await putPreferences({ display_name: null, default_workspace: null, notifications: { muted: [], reminder: 'as_set' }, revision: 1 })
    expect(r.status).toBe('error')
    if (r.status === 'error') {
      expect(r.error.httpStatus).toBe(409)
      expect(r.error.code).toBe('preferences_changed')
    }
  })
})
