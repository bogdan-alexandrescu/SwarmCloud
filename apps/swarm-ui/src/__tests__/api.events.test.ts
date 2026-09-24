// The run screens' event read asks for the route's MAXIMUM page.
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
