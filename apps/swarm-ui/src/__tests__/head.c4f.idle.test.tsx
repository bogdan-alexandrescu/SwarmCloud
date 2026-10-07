// THE IDLE STOP, FROM BOTH SIDES (#117, owner ruling 2026-10-07).
//
// head.c4f.test.tsx proves a `Screen` stops after fifteen minutes with no
// input. This file proves the other two halves: input before the deadline
// keeps a poll alive, and Overview -- which polls through `usePoll`, not
// `Screen` -- stops on the same deadline and shows `Paused · resume` in its
// head, which reads at once and restarts the cadence.
//
// BREAK IT: drop `noteInput` from the input listeners (the first test stops a
// screen someone is using); pass `false` to Overview's `useIdleStop`, or drop
// `idle` from its `usePoll` calls (the second polls an abandoned tab forever).

import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadTasks: vi.fn(),
  loadLeases: vi.fn(),
  loadProviders: vi.fn(),
  loadAccountPool: vi.fn(),
  loadWorkflows: vi.fn(),
  loadStats: vi.fn(),
  loadSpend: vi.fn(),
}))
// `TASK_PAGE_LIMIT` beside the reads: a factory mock throws on any export it
// does not declare, and Overview names the full page it asks for.
vi.mock('../api', () => ({ ...api, TASK_PAGE_LIMIT: 200 }))

const { OverviewScreen, OVERVIEW_POLL_MS } = await import('../Overview')
const { IDLE_STOP_MS, Screen } = await import('../Shell')

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

function fake(): void {
  vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
}

afterEach(() => {
  vi.useRealTimers()
})

describe('#117: input keeps a poll alive', () => {
  it('still re-reads past fifteen minutes when someone touched the page before the deadline', async () => {
    fake()
    const load = vi.fn(
      (): Promise<Result<{ n: number }>> => Promise.resolve({ status: 'ok', data: { n: 1 }, fetchedAt: Date.now() }),
    )
    render(
      <Screen title="Pools" load={load} pollMs={30_000}>
        {() => <p>rows</p>}
      </Screen>,
    )
    await advance(0)
    // Ten minutes in, a person moves the pointer: the fifteen minutes restart.
    await advance(10 * 60_000)
    fireEvent.pointerDown(window)
    // Five more minutes would have been the deadline without that input.
    await advance(IDLE_STOP_MS - 10 * 60_000 + 60_000)
    expect(screen.queryByRole('button', { name: /resume/i }), 'stopped a screen someone was using').toBeNull()
    const calls = load.mock.calls.length
    await advance(30_000)
    expect(load, 'the poll stopped despite recent input').toHaveBeenCalledTimes(calls + 1)
  })
})

describe('#117: Overview stops after fifteen minutes idle, behind `Paused · resume`', () => {
  it('stops its poll, says so in the head, and reads at once on resume', async () => {
    fake()
    const empty = () => Promise.resolve({ status: 'empty', fetchedAt: Date.now() })
    for (const fn of Object.values(api)) fn.mockImplementation(empty)
    render(<OverviewScreen />)
    await advance(100)
    await advance(OVERVIEW_POLL_MS)
    const polled = api.loadTasks.mock.calls.length
    expect(polled, 'the control: Overview never polled').toBeGreaterThan(1)

    await advance(IDLE_STOP_MS)
    const resume = screen.getByRole('button', { name: /resume/i })
    expect(resume.textContent).toBe('Paused · resume')
    const stopped = api.loadTasks.mock.calls.length
    await advance(5 * 60_000)
    expect(api.loadTasks, 'kept polling an idle Overview').toHaveBeenCalledTimes(stopped)

    fireEvent.click(resume)
    await advance(100)
    expect(api.loadTasks.mock.calls.length, 'resume did not read').toBeGreaterThan(stopped)
    expect(screen.queryByRole('button', { name: /resume/i })).toBeNull()
    const resumed = api.loadTasks.mock.calls.length
    await advance(OVERVIEW_POLL_MS)
    expect(api.loadTasks.mock.calls.length, 'resume did not restart the poll').toBeGreaterThan(resumed)
  })
})
