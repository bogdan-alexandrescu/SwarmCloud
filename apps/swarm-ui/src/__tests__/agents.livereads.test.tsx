// AN AGENTS LIST REFRESH MAKES NO PER-ROW LIVE-SESSION READ (#2688,
// WORK-READS-SPEED; the owner's request of 2026-11-02). Measured in the
// owner's browser on Work › Agents with a live browser open: every list
// refresh fired 15-20 `GET /v1/tasks/{id}/live/session` reads at the same
// instant, one per browser row, for the row's device outline -- each a
// transaction on the server -- on top of the open viewer's own reads.
//
// The rows' devices now come from ONE `GET /v1/tasks/digests?include=live`
// per list read, and each outline still follows a device switch on the next
// refresh (QA12-29).
//
// MUTATIONS: put `loadLiveSession` back in `DeviceThumb`; read the digests
// once per row; drop the list's read time from the digests' effect.

import { act, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { TaskDigests } from '../api'
import type { Result } from '../fetch'
import type { Task, TaskPage, TaskState } from '../types'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTasks: vi.fn(),
  loadTasksInStates: vi.fn(),
  loadAttempts: vi.fn(),
  loadLiveSession: vi.fn(),
  loadTaskDigests: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

import { AgentsScreen, LIVE_POLL_MS } from '../Agents'

const BROWSER = 'claude-code-browser'
const ROWS = 18

function tid(n: number): string {
  return `task_${n.toString(16).padStart(20, '0')}`
}

function row(n: number, state: TaskState): Task {
  return runTask({
    id: tid(n),
    state,
    runner_profile: BROWSER,
    created_at: new Date(Date.UTC(2026, 10, 2, 3, 0, n)).toISOString(),
    updated_at: new Date(Date.UTC(2026, 10, 2, 3, 30, n)).toISOString(),
    started_at: new Date(Date.UTC(2026, 10, 2, 3, 0, n)).toISOString(),
    completed_at: null,
    attempt_count: 1,
    max_attempts: 3,
  })
}

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

const PRESETS = [
  { name: 'desktop', width: 1440, height: 900, scale: 1, mobile: false },
  { name: 'iphone-15', width: 393, height: 852, scale: 3, mobile: true },
  { name: 'ipad-7', width: 810, height: 1080, scale: 2, mobile: true },
]

afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('the Agents list reads its browser rows’ live state in one read', () => {
  it('a refresh with 18 browser rows makes no per-row live-session read, and draws each row’s device', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
    const advance = async (ms: number) => {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ms)
      })
    }
    const browsers = Array.from({ length: ROWS }, (_, i) => row(i + 1, 'RUNNING'))
    api.loadTasks.mockImplementation(async () => ok<TaskPage>({ tasks: browsers, next_page_token: null, tenant_id: 'eng' }))
    api.loadTasksInStates.mockImplementation(async (states: readonly TaskState[]) =>
      ok<TaskPage>({ tasks: browsers.filter((t) => states.includes(t.state)), next_page_token: null, tenant_id: 'eng' }),
    )
    api.loadAttempts.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    api.loadLiveSession.mockResolvedValue(ok({ live_browser: true, session: null }))
    let preset = 'iphone-15'
    api.loadTaskDigests.mockImplementation(async (ids: readonly string[]): Promise<Result<TaskDigests>> =>
      ok({
        digests: ids.map((id) => ({
          task_id: id,
          state: 'RUNNING' as const,
          live: { live_browser: true, session_state: 'open' as const, device: { preset, orientation: preset === 'desktop' ? 'landscape' : 'portrait', touch: false } },
        })),
        presets: PRESETS,
      }),
    )

    render(<AgentsScreen onOpen={() => {}} />)
    await advance(0)
    await advance(0)
    const outline = (n: number) => document.querySelector(`.row.is-compact[data-task-id="${tid(n)}"] .ag-device`)
    expect(document.querySelectorAll('.ag-device')).toHaveLength(ROWS)
    expect(outline(1)?.getAttribute('data-device')).toBe('phone')

    const digestsBefore = api.loadTaskDigests.mock.calls.length
    preset = 'desktop'
    await advance(LIVE_POLL_MS)
    await advance(0)

    expect(api.loadLiveSession, 'a live-session read per browser row').not.toHaveBeenCalled()
    // One digest read per list read, naming every browser row at once.
    const refreshes = api.loadTaskDigests.mock.calls.slice(digestsBefore)
    expect(refreshes.length).toBeGreaterThanOrEqual(1)
    expect(refreshes.length).toBeLessThanOrEqual(2)
    const last = api.loadTaskDigests.mock.calls.at(-1)!
    expect([...(last[0] as string[])].sort()).toEqual(browsers.map((t) => t.id).sort())
    expect(last[1]).toEqual(['live'])
    // The outline followed the switch on the refresh (QA12-29).
    expect(outline(1)?.getAttribute('data-device')).toBe('desktop')
    expect(outline(ROWS)?.getAttribute('data-orientation')).toBe('landscape')
  })
})
