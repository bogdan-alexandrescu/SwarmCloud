// THE TWO READS THE LANES ARE DRAWN FROM.
//
// `GET /v1/attempts` (routes/attempts.py): the tenant's attempts, newest
// first, `since` inclusive and `until` exclusive, paged by `page_token`. No
// screen called it before the Lanes page.
//
// `GET /v1/tasks/{id}/events` with `order=desc` and the page token: the UI
// half of redesign-v2 S1. Before it every event list read the OLDEST page, so
// a long run showed its beginning only.

import { describe, expect, it, vi } from 'vitest'

function liveApi(bodies: (url: string) => unknown) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const calls: string[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    calls.push(url)
    return new Response(JSON.stringify(bodies(url)), { status: 200, headers: { 'content-type': 'application/json' } })
  }) as unknown as typeof fetch
  return calls
}

describe('the attempts read', () => {
  it('asks /v1/attempts for one window, the full page, and the next page by its token', async () => {
    const calls = liveApi(() => ({ tenant_id: 'eng', read_at: '2026-10-02T14:00:00Z', attempts: [], next_page_token: null, coverage: { scope: 'page' } }))
    const api = await import('../api')
    const since = '2026-10-02T08:00:00.000Z'
    const until = '2026-10-02T14:00:00.000Z'
    const first = await api.loadAttemptsPage({ since, until })
    await api.loadAttemptsPage({ since, until, pageToken: 'tok' + 'en-2' })
    expect(calls).toHaveLength(2)
    const a = new URL(calls[0]!, 'http://x')
    expect(a.pathname).toBe('/v1/attempts')
    expect(a.searchParams.get('since')).toBe(since)
    expect(a.searchParams.get('until')).toBe(until)
    expect(a.searchParams.get('limit')).toBe('200')
    expect(a.searchParams.get('page_token')).toBeNull()
    expect(new URL(calls[1]!, 'http://x').searchParams.get('page_token')).toBe('token-2')
    // No attempt in the window is an answer, not a failure.
    expect(first.status).toBe('empty')
  })
})

describe('the events read, newest first', () => {
  it('sends order=desc and follows the page token', async () => {
    const calls = liveApi(() => ({ events: [{ event_id: 'e1', task_id: 't', type: 'running', at: '2026-10-02T09:00:00Z', attempt_id: null, lease_id: null, generation: 1, detail: null }], next_page_token: 'p2' }))
    const api = await import('../api')
    const page = await api.loadTaskEventsPage('task_1', {})
    await api.loadTaskEventsPage('task_1', { pageToken: 'p2' })
    const a = new URL(calls[0]!, 'http://x')
    expect(a.pathname).toBe('/v1/tasks/task_1/events')
    expect(a.searchParams.get('order')).toBe('desc')
    expect(a.searchParams.get('limit')).toBe('200')
    expect(new URL(calls[1]!, 'http://x').searchParams.get('page_token')).toBe('p2')
    expect(page.status).toBe('ok')
    if (page.status === 'ok') expect(page.data.next_page_token).toBe('p2')
  })
})
