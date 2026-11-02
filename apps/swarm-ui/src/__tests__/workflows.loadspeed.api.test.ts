// THE WORKFLOWS BOARD IS ONE READ (console load speed, owner's pick
// 2026-11-01). `loadWorkflowBoard` read `GET /v1/tasks?limit=200` beside the
// list -- 328 KB and 3.8 s in the owner's browser, gating the board -- to join
// each step's state. The list's summary view now serves each step's task block
// (`task_summary`, #2993), and the join is built from it.
//
// MUTATIONS: put the task read back into `loadWorkflowBoard`; build
// `taskById` empty; join a step whose block is null; drop the `limit` the
// workflow page asks for.

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

const step = (step_id: string, task_id: string | null, task_summary: unknown) => ({
  step_id,
  runner_profile: 'claude-code',
  resource_class: 'standard',
  depends_on: [],
  input_from: {},
  task_id,
  ...(task_summary === undefined ? {} : { task_summary }),
})

const page = {
  next_page_token: 'older',
  workflows: [
    {
      workflow_id: 'wf_one',
      tenant_id: 'eng',
      state: 'FAILED',
      stored_state: 'QUEUED',
      state_source: 'derived',
      created_at: '2026-11-01T10:00:00Z',
      updated_at: '2026-11-01T10:30:00Z',
      submitted_by: 'priya@example.com',
      priority: 0,
      on_step_failure: 'stop',
      cancel_requested: false,
      steps: [
        step('build', 'task_build', {
          state: 'SUCCEEDED',
          title: 'Build the thing',
          unit: 'build-unit',
          started_at: '2026-11-01T10:01:00Z',
          completed_at: '2026-11-01T10:11:00Z',
          attempts: 2,
          attempts_with_cost: 2,
          cost_usd_total: 1.25,
          cost_incomplete: false,
          duration_s_total: 900,
          duration_incomplete: false,
          first_started_at: '2026-11-01T09:58:00Z',
          last_attempt_cost_usd: 0.75,
          last_attempt_duration_s: 600,
          attempts_read: 'ok',
          last_error_line: null,
        }),
        step('test', 'task_test', {
          state: 'FAILED',
          title: null,
          unit: null,
          started_at: '2026-11-01T10:12:00Z',
          completed_at: '2026-11-01T10:20:00Z',
          attempts: null,
          attempts_with_cost: null,
          cost_usd_total: null,
          cost_incomplete: null,
          duration_s_total: null,
          duration_incomplete: null,
          first_started_at: null,
          last_attempt_cost_usd: null,
          last_attempt_duration_s: null,
          attempts_read: 'failed',
          last_error_line: 'pytest exited 1',
        }),
        // Its task was not read by the route: no block.
        step('lint', 'task_lint', null),
        // No task yet.
        step('ship', null, null),
      ],
    },
  ],
}

describe('loadWorkflowBoard: one request, the join built from the step blocks', () => {
  it('reads GET /v1/workflows?limit=100&view=summary alone, and never the task list', async () => {
    const { api, urls } = await live((u) => (u.pathname === '/v1/tasks' ? { tasks: [], next_page_token: null } : page))
    const r = await api.loadWorkflowBoard()
    expect(urls.map((u) => `${u.pathname}${u.search}`)).toEqual(['/v1/workflows?limit=100&view=summary'])
    expect(r.status).toBe('ok')
    if (r.status !== 'ok') return
    expect(r.data.nextPageToken).toBe('older')
    expect(r.data.statesDetail).toBeNull()
    const byId = r.data.taskById!
    // Every step with a block is joined; the unread one and the taskless one are not.
    expect([...byId.keys()].sort()).toEqual(['task_build', 'task_test'])
    const build = byId.get('task_build')!
    expect(build.state).toBe('SUCCEEDED')
    expect(build.metadata).toEqual({ title: 'Build the thing', unit: 'build-unit' })
    expect(build.started_at).toBe('2026-11-01T10:01:00Z')
    expect(build.completed_at).toBe('2026-11-01T10:11:00Z')
    expect(build.cost_usd_total).toBe(1.25)
    expect(build.attempts).toBe(2)
    expect(build.attempt_count).toBe(2)
    expect(build.workflow_id).toBe('wf_one')
    expect(build.step_id).toBe('build')
    // Nothing the block did not serve is invented.
    expect(build.input).toBeNull()
    expect(build.result_summary).toBeNull()
    const failed = byId.get('task_test')!
    expect(failed.state).toBe('FAILED')
    expect(failed.last_error).toBe('pytest exited 1')
    expect(failed.attempts_read).toBe('failed')
    expect(failed.cost_usd_total).toBeNull()
    vi.unstubAllEnvs()
  })

  it('asks for the page size a caller names, still one request', async () => {
    const { api, urls } = await live(() => page)
    await api.loadWorkflowBoard(50)
    expect(urls.map((u) => `${u.pathname}${u.search}`)).toEqual(['/v1/workflows?limit=50&view=summary'])
    vi.unstubAllEnvs()
  })

  it('a failed list read is the board’s error, with no second read behind it', async () => {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const urls: string[] = []
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      urls.push(String(input))
      return new Response(JSON.stringify({ error: { code: 'internal', message: 'boom' } }), { status: 500 })
    }) as unknown as typeof fetch
    const api = await import('../api')
    const r = await api.loadWorkflowBoard()
    expect(r.status).toBe('error')
    expect(urls.filter((u) => u.includes('/v1/tasks'))).toEqual([])
    vi.unstubAllEnvs()
  })
})
