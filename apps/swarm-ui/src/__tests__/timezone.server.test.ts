// THE TIME ZONE IS KEPT ON THE SERVER (lane TIMEZONE-SERVER, part of #2391).
// BGD decided on 2026-10-26 that a person's zone lives in their server-side
// preferences, so it follows them to every browser and survives sign-out.
// `timezone.ts` reads it from GET /v1/profile and saves it through PUT
// /v1/profile/preferences; this browser's `swarm.timezone:<email>` is only a
// first-paint cache. Its interface is unchanged, so these drive it directly.
//
// Fixtures only; no network.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { waitFor } from '@testing-library/react'
import { serve } from './repofixture'

type Json = Record<string, unknown>

const LA = 'America/Los_Angeles'
const TOKYO = 'Asia/Tokyo'
const EMAIL = 'ada@example.com'
const KEY = `swarm.timezone:${EMAIL}`

function profile(over: Json = {}): Json {
  return {
    identity: { email: EMAIL, display_name: 'Ada', display_name_suggested: null, domain: 'example.com', platform_role: null, is_admin: false, is_admin_unresolved: false },
    workspaces: [],
    default_workspace: { tenant_id: 'eng', source: 'profile', ignored: null },
    connections: {},
    notifications: { preferences: { muted: ['finished'], reminder: 'daily' }, topics: ['finished'] },
    revision: 7,
    unavailable: [],
    time_zone: 'UTC',
    time_zone_source: 'default',
    ...over,
  }
}

/** The API: GET /v1/profile answers `current`, and a PUT stores what it was sent unless `write` says otherwise. */
function api(start: Json, write?: (body: Json) => { status: number; body: unknown } | null) {
  let current = start
  const puts: Json[] = []
  const calls = serve((m, url, body) => {
    if (m === 'GET' && url === '/v1/profile') return { status: 200, body: current }
    if (m === 'PUT' && url === '/v1/profile/preferences') {
      const b = body as Json
      puts.push(b)
      const forced = write?.(b)
      if (forced) return forced
      if (b.revision !== current.revision) return { status: 409, body: { code: 'preferences_changed', message: 'changed elsewhere' } }
      const revision = (current.revision as number) + 1
      current = { ...current, time_zone: b.time_zone ?? current.time_zone, time_zone_source: 'profile', revision }
      return { status: 200, body: { identity: { display_name: b.display_name, display_name_suggested: null }, default_workspace: current.default_workspace, notifications: current.notifications, time_zone: current.time_zone, time_zone_source: 'profile', revision } }
    }
    return null
  })
  return {
    calls,
    puts,
    set: (j: Json) => {
      current = j
    },
    get current() {
      return current
    },
  }
}

/** A fresh page: the modules read again, as a reload would. */
async function page() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const tz = await import('../timezone')
  const me = vi.fn(async () => ({ status: 'ok' as const, fetchedAt: Date.now(), data: { principal: { email: EMAIL } } as never }))
  return { tz, me }
}

const profileReads = (calls: { method: string; url: string }[]) => calls.filter((c) => c.method === 'GET' && c.url === '/v1/profile').length

afterEach(() => {
  vi.unstubAllEnvs()
  window.localStorage.clear()
})

describe('the time zone is kept on the server (TIMEZONE-SERVER, part of #2391)', () => {
  /** MUTATION: read the zone from local storage only, as before TIMEZONE-SERVER. */
  it('shows the zone the person saved in another browser, in a browser that never held one', async () => {
    const server = api(profile({ time_zone: LA, time_zone_source: 'profile' }))
    const { tz, me } = await page()
    expect(tz.currentZone()).toBe('UTC')
    tz.learnPerson(me)
    await waitFor(() => expect(tz.currentZone()).toBe(LA))
    // Cached for the next first paint, and nothing written.
    expect(window.localStorage.getItem(KEY)).toBe(LA)
    expect(server.puts).toEqual([])
    expect(profileReads(server.calls)).toBe(1)
  })

  /** MUTATION: let the cache win over the server. */
  it('lets the server’s zone win over a different one this browser cached', async () => {
    window.localStorage.setItem(KEY, TOKYO)
    api(profile({ time_zone: LA, time_zone_source: 'profile' }))
    const { tz, me } = await page()
    // First paint: the cache.
    expect(tz.currentZone()).toBe(TOKYO)
    tz.learnPerson(me)
    await waitFor(() => expect(tz.currentZone()).toBe(LA))
    expect(window.localStorage.getItem(KEY)).toBe(LA)
  })

  /** MUTATION: keep a server-saved UTC as "nothing saved", leaving the cache's zone. */
  it('treats a saved UTC as a pick: the cached zone gives way and the key goes', async () => {
    window.localStorage.setItem(KEY, TOKYO)
    const server = api(profile({ time_zone: 'UTC', time_zone_source: 'profile' }))
    const { tz, me } = await page()
    tz.learnPerson(me)
    await waitFor(() => expect(tz.currentZone()).toBe('UTC'))
    expect(window.localStorage.getItem(KEY)).toBeNull()
    expect(server.puts).toEqual([])
  })

  /** MUTATION: save the pick to local storage only, or send a partial set the API refuses. */
  it('saves a pick through PUT /v1/profile/preferences with the whole set, at the revision read', async () => {
    const server = api(profile())
    const { tz } = await page()
    tz.notePerson(EMAIL)
    expect(tz.setZone(LA)).toBe(LA)
    // The page moves at once.
    expect(tz.currentZone()).toBe(LA)
    expect(await tz.zoneSaved()).toBe(true)
    expect(server.puts).toEqual([{
      display_name: 'Ada',
      default_workspace: 'eng',
      notifications: { muted: ['finished'], reminder: 'daily' },
      revision: 7,
      time_zone: LA,
    }])
    expect(server.current.time_zone).toBe(LA)
    // A reload, in this browser or another, opens on it.
    window.localStorage.clear()
    const again = await page()
    again.tz.learnPerson(again.me)
    await waitFor(() => expect(again.tz.currentZone()).toBe(LA))
  })

  /** MUTATION: send the computed default as if it were stored. */
  it('sends no default workspace when none is stored, so a zone save never pins one', async () => {
    const server = api(profile({ default_workspace: { tenant_id: 'u-ada', source: 'personal', ignored: null } }))
    const { tz } = await page()
    tz.notePerson(EMAIL)
    tz.setZone(LA)
    expect(await tz.zoneSaved()).toBe(true)
    expect(server.puts[0]?.default_workspace).toBeNull()
  })

  /** MUTATION: give up on the first 409. */
  it('reads again and saves once more when another save landed first (409)', async () => {
    let first = true
    const server = api(profile(), () => {
      if (!first) return null
      first = false
      return { status: 409, body: { code: 'preferences_changed', message: 'changed elsewhere' } }
    })
    const { tz } = await page()
    tz.notePerson(EMAIL)
    tz.setZone(LA)
    expect(await tz.zoneSaved()).toBe(true)
    expect(server.puts.length).toBe(2)
    expect(server.current.time_zone).toBe(LA)
  })

  /** MUTATION: drop the pick when the server refuses it. */
  it('keeps the pick in this browser when the save fails', async () => {
    api(profile(), () => ({ status: 503, body: { code: 'upstream_unavailable', message: 'down' } }))
    const { tz } = await page()
    tz.notePerson(EMAIL)
    tz.setZone(LA)
    expect(await tz.zoneSaved()).toBe(false)
    expect(tz.currentZone()).toBe(LA)
    expect(window.localStorage.getItem(KEY)).toBe(LA)
  })

  /** MUTATION: save while the preferences are unread, overwriting what was not seen. */
  it('writes nothing when the preferences could not be read, and keeps the cached zone', async () => {
    window.localStorage.setItem(KEY, TOKYO)
    const server = api(profile({ notifications: undefined, revision: null, unavailable: ['preferences'], time_zone: null, time_zone_source: 'unavailable' }))
    const { tz, me } = await page()
    tz.learnPerson(me)
    await waitFor(() => expect(profileReads(server.calls)).toBe(1))
    await tz.zoneSaved()
    expect(tz.currentZone()).toBe(TOKYO)
    tz.setZone(LA)
    expect(await tz.zoneSaved()).toBe(false)
    expect(server.puts).toEqual([])
    expect(window.localStorage.getItem(KEY)).toBe(LA)
  })

  /** MUTATION: read a missing field as UTC. */
  it('keeps the cache, and writes nothing, against an API that does not serve a zone yet', async () => {
    window.localStorage.setItem(KEY, TOKYO)
    const old = profile()
    delete old.time_zone
    delete old.time_zone_source
    const server = api(old)
    const { tz, me } = await page()
    tz.learnPerson(me)
    await waitFor(() => expect(profileReads(server.calls)).toBe(1))
    await tz.zoneSaved()
    expect(tz.currentZone()).toBe(TOKYO)
    expect(server.puts).toEqual([])
  })

  describe('a zone this browser kept before the server held one', () => {
    /** MUTATION: drop the browser's zone when the server has none, so the person loses their pick. */
    it('is written to the server, once', async () => {
      window.localStorage.setItem(KEY, LA)
      const server = api(profile())
      const { tz, me } = await page()
      tz.learnPerson(me)
      await waitFor(() => expect(server.puts.length).toBe(1))
      expect(await tz.zoneSaved()).toBe(true)
      expect(server.puts[0]).toMatchObject({ time_zone: LA, revision: 7, default_workspace: 'eng' })
      expect(tz.currentZone()).toBe(LA)
      // The next page load finds it saved and writes nothing more.
      const again = await page()
      again.tz.learnPerson(again.me)
      await waitFor(() => expect(profileReads(server.calls)).toBeGreaterThanOrEqual(3))
      await again.tz.zoneSaved()
      expect(again.tz.currentZone()).toBe(LA)
      expect(server.puts.length).toBe(1)
    })

    /** MUTATION: move it while the API is keeping a default it cannot honour, clearing that default. */
    it('is not moved while a stored default workspace is being kept but not honoured', async () => {
      window.localStorage.setItem(KEY, LA)
      const server = api(profile({ default_workspace: { tenant_id: 'u-ada', source: 'personal', ignored: 'not_a_member' } }))
      const { tz, me } = await page()
      tz.learnPerson(me)
      await waitFor(() => expect(profileReads(server.calls)).toBeGreaterThanOrEqual(2))
      expect(await tz.zoneSaved()).toBe(false)
      expect(server.puts).toEqual([])
      expect(tz.currentZone()).toBe(LA)
    })

    it('is not written when this browser kept nothing: UTC is the default already', async () => {
      const server = api(profile())
      const { tz, me } = await page()
      tz.learnPerson(me)
      await waitFor(() => expect(profileReads(server.calls)).toBe(1))
      await tz.zoneSaved()
      expect(tz.currentZone()).toBe('UTC')
      expect(server.puts).toEqual([])
    })
  })

  /** MUTATION: let a read that began before a pick undo it. */
  it('never undoes a pick with a read that began before it', async () => {
    const server = api(profile({ time_zone: TOKYO, time_zone_source: 'profile' }))
    const { tz, me } = await page()
    tz.notePerson(EMAIL)
    tz.learnPerson(me)
    tz.setZone(LA)
    await waitFor(() => expect(server.puts.length).toBe(1))
    expect(await tz.zoneSaved()).toBe(true)
    expect(tz.currentZone()).toBe(LA)
  })

  /** MUTATION: read the profile again for the zone on a page that already read it (Profile). */
  it('takes the zone from a profile read the page already made, with no second read', async () => {
    const server = api(profile({ time_zone: LA, time_zone_source: 'profile' }))
    const { tz, me } = await page()
    const { loadProfile } = await import('../api')
    await loadProfile()
    expect(tz.currentZone()).toBe(LA)
    tz.learnPerson(me)
    await new Promise((r) => setTimeout(r, 20))
    expect(profileReads(server.calls)).toBe(1)
    expect(me).not.toHaveBeenCalled()
  })

  /** MUTATION: let any later read move the zone, so a reload of Profile after a failed save reverts the pick. */
  it('keeps a pick made on this page when a later read still names the old zone', async () => {
    const server = api(profile({ time_zone: TOKYO, time_zone_source: 'profile' }), () => ({ status: 503, body: { code: 'upstream_unavailable', message: 'down' } }))
    const { tz } = await page()
    const { loadProfile } = await import('../api')
    await loadProfile()
    expect(tz.currentZone()).toBe(TOKYO)
    tz.setZone(LA)
    expect(await tz.zoneSaved()).toBe(false)
    await loadProfile()
    expect(server.current.time_zone).toBe(TOKYO)
    expect(tz.currentZone()).toBe(LA)
  })

  it('learns the person from /v1/tenants/me, as before, when the profile cannot be read', async () => {
    window.localStorage.setItem(KEY, TOKYO)
    window.localStorage.setItem('swarm.timezone:someone@example.com', LA)
    serve(() => ({ status: 503, body: { code: 'upstream_unavailable', message: 'down' } }))
    const { tz, me } = await page()
    expect(tz.currentZone()).toBe('UTC')
    tz.learnPerson(me)
    await waitFor(() => expect(me).toHaveBeenCalled())
    await waitFor(() => expect(tz.currentZone()).toBe(TOKYO))
  })
})

describe('normProfile and the save carry the zone', () => {
  it('reads time_zone and its source, and an unknown source as absent rather than UTC', async () => {
    vi.resetModules()
    const { normProfile } = await import('../api')
    expect(normProfile(profile({ time_zone: LA, time_zone_source: 'profile' }))).toMatchObject({ time_zone: LA, time_zone_source: 'profile' })
    expect(normProfile(profile())).toMatchObject({ time_zone: 'UTC', time_zone_source: 'default' })
    expect(normProfile(profile({ time_zone: null, time_zone_source: 'unavailable' }))).toMatchObject({ time_zone: null, time_zone_source: 'unavailable' })
    const odd = normProfile(profile({ time_zone_source: 'somewhere' }))
    expect('time_zone' in odd).toBe(false)
    expect(odd.time_zone_source).toBeUndefined()
  })

  it('serves the saved zone back from a save', async () => {
    api(profile())
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { putPreferences } = await import('../api')
    const r = await putPreferences({ display_name: null, default_workspace: null, notifications: { muted: [], reminder: 'as_set' }, revision: 7, time_zone: LA })
    expect(r.status).toBe('ok')
    if (r.status === 'ok') expect(r.data).toMatchObject({ time_zone: LA, time_zone_source: 'profile', revision: 8 })
  })
})
