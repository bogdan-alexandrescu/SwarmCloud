// OWNER QA P14 (SwarmCloud release 1e86c37e, 2026-10-13 16:07-16:22 PDT),
// lane QA14-UI; part of #1030.
//
// (1) `/agents/live/<id>/details` on a RUNNING task went to Browser or Logs:
//     Details is written with no segment, so the split could not tell an
//     address that named Details from one that named no pane. The router now
//     says so (`detailNamed`) and the running default yields to it; a bare
//     address keeps the default (owner decision 2026-10-04).
// (2) The tab bar overflowed by its gaps: seven tabs summing 529 in a 533
//     strip, plus six 2 px gaps, is 541. `fitTabs` counts the gaps.
// (3) Schedule tick lateness printed a raw float, "182.216849 s at most".
// (5) Schedules' Repository and When were cut while Next run, Last run and
//     Spend held room they never used. That table is gone since AUTO-A1
//     (frame A1's list, part of #2417), and its width case with it.
// (4), the bell's question items, is in notifications.test.tsx.
//
// MUTATIONS: drop `detailNamed` from `fromAddress` or from the split's
// default (the /details case goes red); drop `gap` from `fitTabs` (the 541
// case goes red); print `max_lateness_seconds` as it comes (lateness goes
// red).

import { act, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { LiveRead } from '../api'
import type { Result } from '../fetch'
import type { Task, TaskPage } from '../types'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTask: vi.fn(),
  loadChildren: vi.fn(),
  loadAttempts: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadAgentRun: vi.fn(),
  loadLiveSession: vi.fn(),
  loadTaskEventsPage: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

const { AgentSplit, fitTabs } = await import('../AgentSplit')
const { fromAddress } = await import('../App')
const { pathToAddress } = await import('../paths')
const { resetListSnap } = await import('../listSnap')
const { latenessWords } = await import('../AdminSchedules')

const ID = 'task_qa14detailsrunning'
const BASE = `work/task/${ID}`

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function agent(over: Partial<Task> = {}): Task {
  return runTask({
    id: ID,
    state: 'RUNNING',
    runner_profile: 'claude-code',
    resource_class: 'standard',
    workflow_id: 'wf_a',
    step_id: 'fix-heartbeat',
    attempt_count: 1,
    completed_at: null,
    result_summary: null,
    ...over,
  })
}

beforeEach(() => {
  localStorage.clear()
  resetListSnap()
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  api.loadTaskEventsPage.mockReturnValue(new Promise(() => {}))
  api.loadChildren.mockResolvedValue(ok({ tasks: [] } satisfies TaskPage))
  api.loadLiveSession.mockResolvedValue(ok<LiveRead>({ live_browser: false, session: null }))
})

const settle = () =>
  act(async () => {
    await new Promise((r) => setTimeout(r, 30))
  })

describe('QA14-UI (1): /details opens Details on a running agent', () => {
  it('reads an address that spells Details out as one that named a pane', () => {
    const named = fromAddress(pathToAddress(`/agents/live/${ID}/details`)!.address)
    expect([named.taskId, named.taskPane, named.detailNamed]).toEqual([ID, 'detail', true])
    const bare = fromAddress(pathToAddress(`/agents/live/${ID}`)!.address)
    expect([bare.taskId, bare.taskPane, bare.detailNamed === true]).toEqual([ID, 'detail', false])
  })

  it('stays on Details for a running agent opened at /details', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    const go = vi.fn()
    render(<AgentSplit taskId={ID} pane="detail" detailNamed artifact={null} closeTo="work/running/live" go={go} base={BASE} />)
    await waitFor(() => expect(document.querySelector('.ag-head-title')?.textContent).toBe('fix-heartbeat'))
    await settle()
    expect(go.mock.calls.map((c) => c[0])).not.toContain(`${BASE}/logs`)
    expect(go.mock.calls.map((c) => c[0])).not.toContain(`${BASE}/browser`)
    const tab = screen.getAllByRole('tab').find((t) => t.textContent?.startsWith('Details'))!
    expect(tab.getAttribute('aria-selected')).toBe('true')
  })

  it('stays on Details for a running browser agent opened at /details', async () => {
    api.loadTask.mockResolvedValue(ok(agent({ runner_profile: 'claude-code-browser', resource_class: 'browser' })))
    api.loadLiveSession.mockResolvedValue(ok<LiveRead>({ live_browser: true, session: null }))
    const go = vi.fn()
    render(<AgentSplit taskId={ID} pane="detail" detailNamed artifact={null} closeTo="work/running/live" go={go} base={BASE} />)
    await waitFor(() => expect(document.querySelector('.ag-head-title')?.textContent).toBe('fix-heartbeat'))
    await settle()
    expect(go.mock.calls.map((c) => c[0])).not.toContain(`${BASE}/browser`)
  })

  it('keeps the running default for the bare address: a running agent stays on Details (#1646)', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    const go = vi.fn()
    render(<AgentSplit taskId={ID} pane="detail" artifact={null} closeTo="work/running/live" go={go} base={BASE} />)
    await waitFor(() => expect(api.loadLiveSession).toHaveBeenCalled())
    await waitFor(() => expect(document.querySelector('.ag-head-title')?.textContent).toBe('fix-heartbeat'))
    await settle()
    expect(go).not.toHaveBeenCalled()
    const tab = screen.getAllByRole('tab').find((t) => t.textContent?.startsWith('Details'))!
    expect(tab.getAttribute('aria-selected')).toBe('true')
  })

  it('keeps the running default for the bare address: a running live-browser agent goes to /browser', async () => {
    api.loadTask.mockResolvedValue(ok(agent({ runner_profile: 'claude-code-browser', resource_class: 'browser' })))
    api.loadLiveSession.mockResolvedValue(ok<LiveRead>({ live_browser: true, session: null }))
    const go = vi.fn()
    render(<AgentSplit taskId={ID} pane="detail" artifact={null} closeTo="work/running/live" go={go} base={BASE} />)
    await waitFor(() => expect(go).toHaveBeenCalledWith(`${BASE}/browser`))
    expect(go.mock.calls.map((c) => c[0])).not.toContain(`${BASE}/logs`)
  })
})

describe('QA14-UI (2): the tab bar counts the gaps between its tabs', () => {
  // The measured case: seven tabs summing 529 in a 533 strip, 2 px gaps.
  const keys = ['detail', 'browser', 'logs', 'changes', 'attempts', 'artifacts', 'checkpoints']
  const W = [80, 76, 56, 84, 80, 78, 75]

  it('moves tabs into More when the tabs and their gaps exceed the strip', () => {
    expect(W.reduce((n, w) => n + w, 0)).toBe(529)
    // Without the gaps it "fits" -- the defect.
    expect(fitTabs(keys, W, 533, 70, 'detail')).toEqual(keys)
    const shown = fitTabs(keys, W, 533, 70, 'detail', 2)
    expect(shown.length).toBeLessThan(keys.length)
    const used = shown.reduce((n, k) => n + W[keys.indexOf(k)]!, 0) + 2 * (shown.length - 1)
    expect(used).toBeLessThanOrEqual(533 - 70)
  })

  it('keeps every tab when tabs and gaps fit exactly', () => {
    expect(fitTabs(keys, W, 529 + 12, 70, 'detail', 2)).toEqual(keys)
  })

  it('counts the gaps beside the open tab too', () => {
    const shown = fitTabs(keys, W, 533, 70, 'checkpoints', 2)
    expect(shown).toContain('checkpoints')
    const used = shown.reduce((n, k) => n + W[keys.indexOf(k)]!, 0) + 2 * (shown.length - 1)
    expect(used).toBeLessThanOrEqual(533 - 70)
  })
})

describe('QA14-UI (3): schedule tick lateness reads as a duration', () => {
  it('formats seconds, minutes and hours', () => {
    expect(latenessWords(182.216849)).toBe('3 min')
    expect(latenessWords(45.2)).toBe('45 s')
    expect(latenessWords(0.4)).toBe('0 s')
    expect(latenessWords(3725)).toBe('1 h 2 min')
    expect(latenessWords(7200)).toBe('2 h')
    expect(latenessWords(182.216849)).not.toMatch(/\./)
  })
})
