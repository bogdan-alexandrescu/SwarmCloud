// ONE REQUEST FOR READS THE SHELL AND THE PAGE SHARE (live QA CH-20, 2026-10-11).
//
// The QA counted, in the first 4 s of each page, the shell and the page
// reading the same routes (`/v1/stats`, `/v1/capacity`, `/v1/approvals`,
// `/v1/tenants/me`), and a finished agent's page asking for its task 3 times
// and its attempts 4 times. These are not polls: the counts did not grow.
// `fetch.ts` now joins a GET to an identical one already in flight.
//
// MEASURED THROUGH <App /> AND A STUBBED fetch, as `agentlink.anystate` does:
// every request the page makes reaches the stub, which answers each route at
// once (a 404 for one it does not know, so nothing hangs in flight and turns
// a later read into a joined one), and the counts are of what reached it.
//
// MUTATIONS: make `arrive` (fetch.ts) send every read on its own, or drop
// the just-settled reuse -- the agent page's task, attempts and the shell's
// routes are asked for again, and the counts below go red.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, render } from '@testing-library/react'

import { read, readsInFlight, REUSE_SETTLED_MS, REUSED_ROUTES, route, write } from '../fetch'
import { attempt, task as baseTask } from './runfixture'

const ID = 'task_423932f8c0de4a1b9e7f'

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

function routes(): Record<string, unknown> {
  const done = baseTask({
    id: ID, tenant_id: 'eng', state: 'SUCCEEDED', started_at: new Date(Date.now() - 600_000).toISOString(),
    completed_at: new Date(Date.now() - 60_000).toISOString(),
  })
  return {
    '/v1/tasks': { tasks: [done], next_page_token: null },
    [`/v1/tasks/${ID}`]: { task: done },
    [`/v1/tasks/${ID}/events`]: { events: [], next_page_token: null },
    [`/v1/tasks/${ID}/attempts`]: { attempts: [attempt(1, { task_id: ID, tenant_id: 'eng' })], next_page_token: null },
    [`/v1/tasks/${ID}/checkpoints`]: {
      task_id: ID, tenant_id: 'eng', prefix: `eng/${ID}/`, checkpoints: [], count: 0, total_found: 0,
      next_page_token: null, listed: true, truncated: false,
      latest_checkpoint: { pointer: null, status: 'unset', checkpoint_id: null },
    },
    '/v1/resource-classes': { resource_classes: {} },
  }
}

/** Every GET the app makes at `path` in its first `ms`, by route. */
async function readsOn(path: string, ms = 1_500): Promise<Map<string, number>> {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const answers = routes()
  const counts = new Map<string, number>()
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://ui.test')
    if ((init?.method ?? 'GET') === 'GET') counts.set(url.pathname, (counts.get(url.pathname) ?? 0) + 1)
    const answer = answers[url.pathname]
    if (answer === undefined) return json({ code: 'not_found', message: 'not in this stub' }, 404)
    return json(answer)
  }) as unknown as typeof fetch
  window.history.replaceState(null, '', path)
  const { App } = await import('../App')
  render(<App />)
  await act(async () => {
    await new Promise((r) => setTimeout(r, ms))
  })
  return counts
}

afterEach(() => {
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

describe('CH-20: identical reads in flight are one request', () => {
  it('reads a finished agent\'s task and attempts once each on load', async () => {
    const counts = await readsOn(`/agents/recent/${ID}`)
    console.info('CH-20 agent page GETs', JSON.stringify([...counts].sort()))
    // Measured before the change, through this same stub: task 3, attempts 4,
    // checkpoints 2, resource-classes 2 -- the QA's counts exactly.
    expect(counts.get(`/v1/tasks/${ID}`), 'task').toBe(1)
    expect(counts.get(`/v1/tasks/${ID}/attempts`), 'attempts').toBe(1)
    expect(counts.get(`/v1/tasks/${ID}/checkpoints`), 'checkpoints').toBe(1)
    expect(counts.get('/v1/resource-classes'), 'resource-classes').toBe(1)
  })

  it('reads each route the shell and the Overview share once', async () => {
    const counts = await readsOn('/overview')
    console.info('CH-20 overview GETs', JSON.stringify([...counts].sort()))
    // Measured before the change: approvals 2, capacity 2, stats 2.
    for (const shared of ['/v1/stats', '/v1/capacity', '/v1/approvals', '/v1/tenants/me']) {
      expect(counts.get(shared), shared).toBe(1)
    }
  })
})

describe('the join itself (fetch.ts `arrive`)', () => {
  function stub(): { fetch: ReturnType<typeof vi.fn>; release: () => void } {
    let release = () => {}
    const gate = new Promise<void>((r) => {
      release = r
    })
    const fn = vi.fn(async () => {
      await gate
      return json({ n: 1 })
    })
    globalThis.fetch = fn as unknown as typeof fetch
    return { fetch: fn, release }
  }

  it('sends one request for two identical reads, and each caller gets its own copy', async () => {
    const s = stub()
    const a = read<{ n: number }>(route('/v1/stats'), () => false)
    const b = read<{ n: number }>(route('/v1/stats'), () => false)
    expect(readsInFlight()).toBe(1)
    s.release()
    const [ra, rb] = await Promise.all([a, b])
    expect(s.fetch).toHaveBeenCalledTimes(1)
    expect(ra.status === 'ok' && rb.status === 'ok').toBe(true)
    if (ra.status !== 'ok' || rb.status !== 'ok') return
    expect(ra.data).toEqual({ n: 1 })
    expect(rb.data).toEqual({ n: 1 })
    expect(ra.data).not.toBe(rb.data)
    expect(readsInFlight()).toBe(0)
  })

  it('asks again once the first read settled, for a route outside REUSED_ROUTES', async () => {
    globalThis.fetch = vi.fn(async () => json({ n: 1 })) as unknown as typeof fetch
    expect(REUSED_ROUTES.has('/v1/workflows')).toBe(false)
    await read(route('/v1/workflows'), () => false)
    await read(route('/v1/workflows'), () => false)
    expect(globalThis.fetch).toHaveBeenCalledTimes(2)
  })

  it('takes a just-settled success on a reused route, with that answer\'s own age, and asks again past the window', async () => {
    let now = 1_000_000
    vi.spyOn(Date, 'now').mockImplementation(() => now)
    globalThis.fetch = vi.fn(async () => json({ n: 1 })) as unknown as typeof fetch
    const first = await read(route('/v1/stats'), () => false)
    now += REUSE_SETTLED_MS - 1
    const second = await read(route('/v1/stats'), () => false)
    expect(globalThis.fetch).toHaveBeenCalledTimes(1)
    expect(first.status === 'ok' && second.status === 'ok').toBe(true)
    if (first.status !== 'ok' || second.status !== 'ok') return
    // The data's age, not the moment it was handed over.
    expect(second.fetchedAt).toBe(first.fetchedAt)
    expect(second.data).not.toBe(first.data)
    now += 1
    await read(route('/v1/stats'), () => false)
    expect(globalThis.fetch).toHaveBeenCalledTimes(2)
  })

  it('never reuses a failure, nor an answer from before a write', async () => {
    const fn = vi.fn(async () => json({ message: 'down' }, 500))
    globalThis.fetch = fn as unknown as typeof fetch
    await read(route('/v1/stats'), () => false)
    await read(route('/v1/stats'), () => false)
    expect(fn).toHaveBeenCalledTimes(2)
    fn.mockImplementation(async () => json({ n: 1 }))
    await read(route('/v1/stats'), () => false)
    expect(fn).toHaveBeenCalledTimes(3)
    await write(route('/v1/tasks/{id}/cancel', { id: ID }), 'POST')
    await read(route('/v1/stats'), () => false)
    // the write, then stats asked again
    expect(fn).toHaveBeenCalledTimes(5)
  })

  it('never joins different URLs, nor a read asked after a write to one asked before it', async () => {
    const s = stub()
    const before = read(route('/v1/stats'), () => false)
    void read(route('/v1/capacity'), () => false)
    expect(s.fetch).toHaveBeenCalledTimes(2)
    void write(route('/v1/tasks/{id}/cancel', { id: ID }), 'POST')
    const after = read(route('/v1/stats'), () => false)
    // stats twice (before and after the write), capacity once, the write once.
    expect(s.fetch).toHaveBeenCalledTimes(4)
    s.release()
    await Promise.all([before, after])
  })

  it('abandons one caller\'s wait on its signal, and aborts the request only when every caller has', async () => {
    let seen: AbortSignal | undefined
    globalThis.fetch = vi.fn((_u: RequestInfo | URL, init?: RequestInit) => {
      seen = init?.signal ?? undefined
      return new Promise<Response>(() => {})
    }) as unknown as typeof fetch
    const one = new AbortController()
    const two = new AbortController()
    const a = read(route('/v1/stats'), () => false, { signal: one.signal })
    const b = read(route('/v1/stats'), () => false, { signal: two.signal })
    one.abort()
    expect((await a).status).toBe('error')
    expect(seen?.aborted).toBe(false)
    two.abort()
    expect((await b).status).toBe('error')
    expect(seen?.aborted).toBe(true)
    expect(readsInFlight()).toBe(0)
  })

  it('classifies a shared failure for each caller, with each one\'s own previous data', async () => {
    globalThis.fetch = vi.fn(async () => json({ code: 'boom', message: 'down' }, 500)) as unknown as typeof fetch
    const prev = { data: { n: 0 }, fetchedAt: 1 }
    const [a, b] = await Promise.all([
      read(route('/v1/stats'), () => false, { previous: prev }),
      read(route('/v1/stats'), () => false),
    ])
    expect(globalThis.fetch).toHaveBeenCalledTimes(1)
    expect(a.status).toBe('stale')
    expect(b.status).toBe('error')
  })
})

