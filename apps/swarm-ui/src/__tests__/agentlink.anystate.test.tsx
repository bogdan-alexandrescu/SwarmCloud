// AN AGENT LINK OPENS THE AGENT IN ANY STATE.
//
// The API serves every task's console link as `<origin>/agents/live/<task_id>`
// -- for a QUEUED task, a RUNNING one and a SUCCEEDED or FAILED one alike. The
// path's `live` names the list the drawer sits over, not a claim about the
// task, so the drawer must read the task BY ID (`GET /v1/tasks/<id>`) rather
// than look it up among the rows the Live list happens to hold. A finished or
// queued task is never in that list, and a drawer that only opened for listed
// rows would turn every finished task's link into an empty page.
//
// WHY THROUGH <App /> AND A STUBBED fetch. The link is a PATH, resolved by
// paths.ts and then App's router, and the defect worth catching is in that
// wiring. The Live list here answers with one OTHER, running task, so the
// task the link names is never among its rows: the drawer can only show it by
// reading it by id. A path the stub does not name never answers.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import type { Task, TaskState } from '../types'
import { task as baseTask } from './runfixture'

const PROFILE = 'linkprobe-runner'
const OTHER = 'task_0ther0running0000000'

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })
}

function taskIn(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  const finished = state === 'SUCCEEDED' || state === 'FAILED'
  return baseTask({
    id,
    tenant_id: 'eng',
    state,
    runner_profile: PROFILE,
    started_at: state === 'QUEUED' ? null : new Date(Date.now() - 120_000).toISOString(),
    completed_at: finished ? new Date(Date.now() - 30_000).toISOString() : null,
    attempt_count: state === 'QUEUED' ? 0 : 1,
    ...over,
  })
}

async function openLink(id: string, state: TaskState): Promise<string[]> {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const calls: string[] = []
  const other = taskIn(OTHER, 'RUNNING', { runner_profile: 'claude-code' })
  const routes: Record<string, unknown> = {
    // The list the path names holds a different task only.
    '/v1/tasks': { tasks: [other], next_page_token: null },
    [`/v1/tasks/${id}`]: { task: taskIn(id, state) },
    [`/v1/tasks/${id}/events`]: { events: [] },
    [`/v1/tasks/${id}/attempts`]: { attempts: [] },
    '/v1/resource-classes': { resource_classes: {} },
  }
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const raw = String(input)
    calls.push(raw)
    const answer = routes[new URL(raw, 'http://ui.test').pathname]
    if (answer === undefined) return new Promise<Response>(() => {})
    return json(answer)
  }) as unknown as typeof fetch
  window.history.replaceState(null, '', `/agents/live/${id}`)
  const { App } = await import('../App')
  render(<App />)
  return calls
}

afterEach(() => {
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

describe('agent link drawer opens in any state (path /agents/live/<id>)', () => {
  const cases: Array<[TaskState, string]> = [
    ['SUCCEEDED', 'task_11nk0succeeded000000'],
    ['FAILED', 'task_11nk0failed00000000'],
    ['QUEUED', 'task_11nk0queued00000000'],
  ]

  for (const [state, id] of cases) {
    it(`opens the drawer for a ${state} task that is not in the live list`, async () => {
      const calls = await openLink(id, state)

      await waitFor(() => {
        const drawer = document.querySelector<HTMLElement>('.ctl-drawer')
        expect(drawer, `no agent drawer opened for the ${state} task`).not.toBeNull()
        expect(drawer!.getAttribute('aria-label')).toBe(`Agent ${id}`)
        // The task's own read is drawn: its profile is on no other task.
        expect(drawer!.textContent ?? '').toContain(PROFILE)
        expect(drawer!.textContent ?? '').not.toContain(OTHER)
      })

      // Read by id, not found among the list's rows.
      const paths = calls.map((c) => new URL(c, 'http://ui.test').pathname)
      expect(paths).toContain(`/v1/tasks/${id}`)
      // The address still names this agent.
      expect(window.location.pathname).toBe(`/agents/live/${id}`)
    })
  }
})
