// The workflow list reads ask for the summary view, and the Timeline's
// by-id read says whether the API batched it (live QA CH-21, CH-6).
//
// MUTATIONS: drop `WORKFLOW_LIST_VIEW` from `loadWorkflowBoard` or
// `loadWorkflows`; treat an answer with no `missing` as batched.

import { describe, expect, it, vi } from 'vitest'

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })
}

async function live(answer: (url: URL) => unknown): Promise<{ api: typeof import('../api'); urls: URL[] }> {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const urls: URL[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), 'http://ui.test')
    urls.push(url)
    return json(answer(url))
  }) as unknown as typeof fetch
  return { api: await import('../api'), urls }
}

describe('CH-21: every workflow list read asks for view=summary', () => {
  it('asks for it on the board, the Overview list, a short list and the older pages', async () => {
    const { api, urls } = await live((u) => (u.pathname === '/v1/tasks' ? { tasks: [], next_page_token: null } : { workflows: [], next_page_token: null }))
    await api.loadWorkflowBoard()
    await api.loadWorkflows()
    await api.loadWorkflows(5)
    await api.loadOlderWorkflows('tok')
    const lists = urls.filter((u) => u.pathname === '/v1/workflows')
    expect(lists).toHaveLength(4)
    for (const u of lists) expect(u.searchParams.get('view'), u.search).toBe('summary')
    expect(lists.map((u) => u.searchParams.get('limit'))).toEqual(['100', '100', '5', '100'])
    vi.unstubAllEnvs()
  })
})

describe('CH-6: GET /v1/workflows?ids=', () => {
  const row = (id: string, taskId: string) => ({ workflow_id: id, state: 'SUCCEEDED', steps: [{ step_id: 's', task_id: taskId }] })

  it('names every id, and gives each workflow its own steps\' tasks', async () => {
    const { api, urls } = await live(() => ({
      workflows: [row('wf_a', 't_a'), row('wf_b', 't_b')],
      tasks: [{ id: 't_a', state: 'SUCCEEDED' }, { id: 't_b', state: 'FAILED' }],
      missing: ['wf_gone'],
      next_page_token: null,
    }))
    const r = await api.loadWorkflowsById(['wf_a', 'wf_b', 'wf_gone'])
    expect(urls[0]!.searchParams.getAll('ids')).toEqual(['wf_a', 'wf_b', 'wf_gone'])
    expect(urls[0]!.searchParams.get('view')).toBe('summary')
    expect(r.status).toBe('ok')
    if (r.status !== 'ok') return
    expect(r.data.batched).toBe(true)
    expect(r.data.missing).toEqual(['wf_gone'])
    expect(r.data.reads.get('wf_a')!.tasks.map((t) => t.id)).toEqual(['t_a'])
    expect(r.data.reads.get('wf_b')!.tasks.map((t) => t.id)).toEqual(['t_b'])
    vi.unstubAllEnvs()
  })

  it('says an answer without `missing` was not batched, and concludes nothing from it', async () => {
    // An API older than `ids` ignores it and serves its newest page.
    const { api } = await live(() => ({ workflows: [row('wf_a', 't_a')], next_page_token: 'more' }))
    const r = await api.loadWorkflowsById(['wf_a', 'wf_x'])
    expect(r.status).toBe('ok')
    if (r.status !== 'ok') return
    expect(r.data.batched).toBe(false)
    expect(r.data.reads.size).toBe(0)
    expect(r.data.missing).toEqual([])
    vi.unstubAllEnvs()
  })
})
