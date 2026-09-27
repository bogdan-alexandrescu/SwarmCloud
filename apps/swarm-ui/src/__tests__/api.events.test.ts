// The run screens' event and attempt reads ask for the route's MAXIMUM page.
//
// THE DEFECT. `GET /v1/tasks/{id}/events` with no `limit` answers with the
// API's DEFAULT page, `default_page_size = 50` (swarm_api/settings.py), not
// its cap of `max_page_size = 200`. `loadAgentRun` and `loadAgentDetail` sent
// no limit, so every run screen read 50 events while every comment in this
// client, and redesign-v2 §4, reasoned about 200. At one heartbeat event per
// 150s and a checkpoint event per 120s, 50 events end around the half-hour
// mark of an attempt: the peak-memory line, the checkpoint strip and the
// checkpoint table's locations all stopped there.
//
// The value is written out as 200 here rather than read from api.ts, so this
// file states the API's number independently of the client constant it is
// checking.

import { describe, expect, it, vi } from 'vitest'

const API_MAX_PAGE_SIZE = 200

describe('the events read', () => {
  it('asks the events route for its maximum page, not its default', async () => {
    // The live path, not the development fixtures: USE_FIXTURES is decided
    // once at import time, so the module is loaded fresh after the stub.
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const calls: string[] = []
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      calls.push(url)
      const body = url.includes('/events')
        ? { events: [] }
        : url.includes('/attempts')
          ? { attempts: [] }
          : url.includes('/resource-classes')
            ? { resource_classes: {} }
            : { task: { id: 'tsk_live', state: 'RUNNING' } }
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    }) as unknown as typeof fetch

    const api = await import('../api')
    await api.loadAgentRun('tsk_live')
    await api.loadAgentDetail('tsk_live')

    const eventReads = calls.filter((c) => c.includes('/events'))
    expect(eventReads, 'the events route was never read, so this test checked nothing').toHaveLength(2)
    for (const c of eventReads) {
      expect(c).toBe(`/v1/tasks/tsk_live/events?limit=${API_MAX_PAGE_SIZE}`)
    }
  })
})

// THE SAME DEFECT ON THE ATTEMPTS ROUTE. `GET /v1/tasks/{id}/attempts` goes
// through the same `paged_limit` (routes/tasks.py), so with no `limit` it
// returns 50 attempts, NEWEST first. task_d18d8d8b044d469cb43c reached 83
// attempts on 2026-09-23; the inspector would have read 50 of them and the
// phase chart's sum would have said "over 50 of 50".
describe('the attempts read', () => {
  it('asks the attempts route for its maximum page, not its default', async () => {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const calls: string[] = []
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      calls.push(url)
      const body = url.includes('/events')
        ? { events: [] }
        : url.includes('/attempts')
          ? { attempts: [] }
          : url.includes('/resource-classes')
            ? { resource_classes: {} }
            : { task: { id: 'tsk_live', state: 'RUNNING' } }
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    }) as unknown as typeof fetch

    const api = await import('../api')
    await api.loadAgentRun('tsk_live')
    await api.loadAttempts('tsk_live')

    const attemptReads = calls.filter((c) => c.includes('/attempts'))
    expect(attemptReads, 'the attempts route was never read, so this test checked nothing').toHaveLength(2)
    // #184 follow-up: the CPU figures are typed attempt fields (contract
    // request #15), served on every row. The drawer's read asked for
    // `include=usage`, the interim heartbeat-event read the typed fields
    // replaced; it asks for nothing extra now. MUTATION: put it back.
    expect(attemptReads[0]).toBe(`/v1/tasks/tsk_live/attempts?limit=${API_MAX_PAGE_SIZE}`)
    expect(attemptReads[1]).toBe(`/v1/tasks/tsk_live/attempts?limit=${API_MAX_PAGE_SIZE}`)
  })
})

// THE INPUT, MASKED (#184 follow-up). Details and Inputs draw the task's input
// from the API's read-time-redacted copy, `GET /v1/tasks/{id}/input`, never
// from the task document.
//
// NOT PART OF THE DRAWER'S READ (PR #210 re-review). The copy was a fifth read
// in `loadAgentRun`'s `Promise.all`, so the whole drawer -- the task's state,
// its events, its attempts -- waited on it, and nothing timed it out. Details'
// Input reads it on its own now. And one request per task at a time: Details
// and Inputs ask together when a drawer opens, and each poll re-asks a copy
// that failed. MUTATIONS: put `loadTaskInputOnce` back in the `Promise.all`;
// drop the in-flight map; keep a failed copy.

const COPY = {
  task_id: 'tsk_live', tenant_id: 'acme', read_at: '2026-09-25T12:00:00Z', prompt_key: 'string',
  prompt: { text: 'use sk-proj01********', redaction_count: 1 }, rest: null,
  full: { text: '{\n  "prompt": "use sk-proj01********"\n}', redaction_count: 1 },
  redacted: true, redaction_count: 1, redaction: { applied_at_read_time: true, rules: 11 },
}

function runRoutes(url: string): unknown {
  return url.includes('/events')
    ? { events: [] }
    : url.includes('/attempts')
      ? { attempts: [] }
      : url.includes('/resource-classes')
        ? { resource_classes: {} }
        : { task: { id: 'tsk_live', state: 'RUNNING', input: { prompt: 'use sk-proj0123456789abcdef' } } }
}

const okJson = (body: unknown) =>
  new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })

describe('the input read', () => {
  it('is not part of the drawer’s read: the run lands while the copy has not answered', async () => {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const calls: string[] = []
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      calls.push(url)
      // A copy that never answers: the drawer must not wait on it.
      if (url.endsWith('/input')) return new Promise<Response>(() => {})
      return okJson(runRoutes(url))
    }) as unknown as typeof fetch

    const api = await import('../api')
    const landed = await Promise.race([
      api.loadAgentRun('tsk_live').then(() => 'landed' as const),
      new Promise<'waiting'>((r) => setTimeout(() => r('waiting'), 1000)),
    ])
    expect(landed, 'the drawer waited on the masked copy of the input').toBe('landed')
    expect(calls.filter((c) => c.endsWith('/input')), 'the drawer’s read still asks for the input').toEqual([])
  })

  it('asks once for concurrent readers, answers a read copy from memory, and asks again after a failure', async () => {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const calls: string[] = []
    let failFirst = true
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      calls.push(url)
      if (url.endsWith('/input')) {
        await new Promise((r) => setTimeout(r, 20))
        if (failFirst) {
          failFirst = false
          return new Response(JSON.stringify({ code: 'upstream_unavailable', message: 'Busy.' }), {
            status: 503,
            headers: { 'content-type': 'application/json' },
          })
        }
        return okJson(COPY)
      }
      return okJson(runRoutes(url))
    }) as unknown as typeof fetch

    const api = await import('../api')
    const inputReads = () => calls.filter((c) => c.endsWith('/input')).length

    // Details and Inputs open together, while the first read is in flight.
    const [a, b] = await Promise.all([api.loadTaskInputOnce('tsk_live'), api.loadTaskInputOnce('tsk_live')])
    expect(inputReads(), 'two readers of one copy made two requests').toBe(1)
    expect(a.status).toBe('error')
    expect(b.status).toBe('error')

    // A failed copy is not kept: the next poll asks again, and gets it.
    const second = await api.loadTaskInputOnce('tsk_live')
    expect(inputReads(), 'a failed copy was kept instead of asked again').toBe(2)
    expect(second.status).toBe('ok')

    // A copy that was read is answered from memory, however often it is asked.
    const third = await api.loadTaskInputOnce('tsk_live')
    await api.loadTaskInputOnce('tsk_live')
    expect(inputReads(), 'a read copy was asked for again').toBe(2)
    expect(third.status === 'ok' ? third.data.prompt?.text : null).toBe('use sk-proj01********')
  })
})
