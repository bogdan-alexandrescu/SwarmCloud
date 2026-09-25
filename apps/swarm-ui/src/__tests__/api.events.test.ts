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

// THE INPUT, MASKED (#184 follow-up). Details draws the task's input from the
// API's read-time-redacted copy, `GET /v1/tasks/{id}/input`, never from the
// task document. The input never changes after submission, so the drawer --
// which re-reads its run every 10 s -- asks for it once.
describe('the input read', () => {
  it('reads the masked copy with the run, once however often the drawer re-reads', async () => {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const calls: string[] = []
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      calls.push(url)
      const body = url.endsWith('/input')
        ? {
            task_id: 'tsk_live', tenant_id: 'acme', read_at: '2026-09-25T12:00:00Z', prompt_key: 'string',
            prompt: { text: 'use sk-proj01********', redaction_count: 1 }, rest: null,
            full: { text: '{\n  "prompt": "use sk-proj01********"\n}', redaction_count: 1 },
            redacted: true, redaction_count: 1, redaction: { applied_at_read_time: true, rules: 11 },
          }
        : url.includes('/events')
          ? { events: [] }
          : url.includes('/attempts')
            ? { attempts: [] }
            : url.includes('/resource-classes')
              ? { resource_classes: {} }
              : { task: { id: 'tsk_live', state: 'RUNNING', input: { prompt: 'use sk-proj0123456789abcdef' } } }
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    }) as unknown as typeof fetch

    const api = await import('../api')
    const first = await api.loadAgentRun('tsk_live')
    await api.loadAgentRun('tsk_live')

    const inputReads = calls.filter((c) => c.endsWith('/input'))
    expect(inputReads, 'the drawer never asked for the masked copy').toEqual(['/v1/tasks/tsk_live/input'])
    expect(first.status).toBe('ok')
    const run = (first as unknown as { data: Record<string, unknown> }).data
    const copy = run['input'] as { status: string; data?: { prompt?: { text: string } } } | undefined
    expect(copy?.status, 'the run carries no masked copy').toBe('ok')
    expect(copy?.data?.prompt?.text).toBe('use sk-proj01********')
  })
})
