// THE TYPED STATE-LIST READ (the owner's pick, 2026-11-02; part of #2688).
//
// The Agents list reads its live and waiting states as ONE `state=A,B,...`
// request, which swarm-api parses as one `state IN [...]` query. It used to
// cast that comma list to a single `TaskState` to pass it through
// `loadTasksInState`, whose fixture filter then matched no row. Now
// `loadTasksInStates` takes the states typed, sends the same request, and its
// fixture filter returns the rows in any of them.
//
// MUTATIONS: send one request per state; drop a state from the joined value;
// filter the fixture page on one state; have the Agents list call anything
// else, or with other states.

import { act, cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { TaskState } from '../types'

/**
 * The query the Agents list's state read sent on 2026-11-02, before this
 * change: `loadTasksInState(LIVE_AND_WAITING.join(',') as TaskState, { view:
 * 'summary' })`. Written out, so the pin does not move with the code.
 */
const BEFORE = 'state=LEASED%2CDISPATCHED%2CSTARTING%2CRUNNING%2CREADY%2CPARKED&limit=200&view=summary'

/** Every URL `fetch` was handed, as strings. */
function fetched(): string[] {
  const calls = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls
  return calls.map((c) => String(c[0] instanceof Request ? c[0].url : c[0]))
}

/** A fetch that answers every request with an empty task page. */
function answerEmpty(): void {
  globalThis.fetch = vi.fn(
    async () => new Response('{"tasks":[],"next_page_token":null,"tenant_id":"eng"}', { status: 200, headers: { 'content-type': 'application/json' } }),
  ) as unknown as typeof fetch
}

/** The api module against a live API (`VITE_LIVE`), freshly imported. */
async function liveApi(): Promise<typeof import('../api')> {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  answerEmpty()
  return import('../api')
}

const query = (url: string): string => url.slice(url.indexOf('?') + 1)

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  vi.resetModules()
})

describe('loadTasksInStates against the API', () => {
  it('sends ONE /v1/tasks request carrying every state as one comma list', async () => {
    const api = await liveApi()
    const states: readonly TaskState[] = ['LEASED', 'RUNNING', 'PARKED']
    const r = await api.loadTasksInStates(states, { view: 'summary' })
    expect(r.status).toBe('empty')
    const urls = fetched()
    expect(urls).toHaveLength(1)
    const url = new URL(urls[0]!, 'http://console.test')
    expect(url.pathname.endsWith('/v1/tasks')).toBe(true)
    expect(url.searchParams.getAll('state')).toEqual(['LEASED,RUNNING,PARKED'])
    expect(url.searchParams.get('limit')).toBe('200')
    expect(url.searchParams.get('view')).toBe('summary')
  })

  it('sends byte for byte what the single-state read sent for the cast comma list', async () => {
    const api = await liveApi()
    const states: readonly TaskState[] = ['LEASED', 'DISPATCHED', 'STARTING', 'RUNNING', 'READY', 'PARKED']
    await api.loadTasksInState(states.join(',') as TaskState, { view: 'summary' })
    await api.loadTasksInStates(states, { view: 'summary' })
    const [cast, typed] = fetched()
    expect(typed).toBe(cast)
    expect(query(typed!)).toBe(BEFORE)
  })

  it('honours the limit, and sends no view when none is asked for', async () => {
    const api = await liveApi()
    await api.loadTasksInStates(['READY'], { limit: 50 })
    expect(query(fetched()[0]!)).toBe('state=READY&limit=50')
  })
})

describe('loadTasksInStates in a fixture build', () => {
  it('returns only the rows in the states asked for, from one fixture read', async () => {
    vi.resetModules()
    const api = await import('../api')
    expect(api.USE_FIXTURES).toBe(true)
    const all = await api.loadTasks()
    if (all.status !== 'ok') throw new Error(`the fixture page read ${all.status}`)
    const asked: readonly TaskState[] = ['RUNNING', 'PARKED', 'SUCCEEDED']
    const want = all.data.tasks.filter((t) => asked.includes(t.state)).map((t) => t.id)
    // CONTROL: the fixture holds rows in more than one of them, and rows outside them.
    expect(new Set(all.data.tasks.filter((t) => asked.includes(t.state)).map((t) => t.state)).size).toBeGreaterThan(1)
    expect(all.data.tasks.length).toBeGreaterThan(want.length)

    const r = await api.loadTasksInStates(asked)
    if (r.status !== 'ok') throw new Error(`the state read was ${r.status}`)
    expect(r.data.tasks.map((t) => t.id)).toEqual(want)
    expect(r.data.tasks.every((t) => asked.includes(t.state))).toBe(true)
  })

  it('is empty, not an error, when no row is in any state asked for', async () => {
    vi.resetModules()
    const api = await import('../api')
    const r = await api.loadTasksInStates(['SUBMITTED'])
    expect(r.status).toBe('empty')
  })
})

describe('the Agents list\'s state read', () => {
  it('makes the same one request it made before the typed read', async () => {
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches: false, media: q, onchange: null,
      addEventListener: () => {}, removeEventListener: () => {}, addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
    }))
    await liveApi()
    const { AgentsScreen } = await import('../Agents')
    await act(async () => {
      render(<AgentsScreen onOpen={() => {}} />)
    })
    await vi.waitFor(() => expect(fetched().filter((u) => u.includes('state='))).toHaveLength(1))
    const stateReads = fetched().filter((u) => u.includes('state='))
    expect(query(stateReads[0]!)).toBe(BEFORE)
  })
})
