/**
 * A DETAILS READ THAT LANDS AFTER ITS PANE IS GONE CHANGES NOTHING (#605).
 *
 * `AgentDetailScreen`'s load names the heading from inside its `.then`, before
 * `Screen` (which drops a late answer itself) sees the answer. On 2026-10-05 a
 * ui run whose 217 files all passed still failed: walk.agent.test.tsx's split
 * moved a running agent off Details onto Logs, the Details read landed after
 * the test environment was torn down, and its `setName` scheduled a React
 * update into a document that was gone -- `ReferenceError: window is not
 * defined` at AgentDetail.tsx's load, as an unhandled rejection.
 *
 * The name is computed by `agentName`, so whether the late answer reached
 * `setName` is whether `agentName` ran. The first case is the control: the
 * same read, landing while the pane is mounted, does name it.
 *
 * MUTATIONS: drop `mounted.current &&` from the load's guard, or never set
 * `mounted.current = false` on unmount -- the second case turns red.
 */
import { act, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { AgentRun } from '../api'
import { task as runTask } from './runfixture'

const named = vi.hoisted(() => ({ agentName: vi.fn() }))
vi.mock('../agentlist', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../agentlist')>()
  return { ...actual, agentName: named.agentName }
})

const run = vi.hoisted(() => ({ loadAgentRun: vi.fn() }))
vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  // Every other read the pane makes never answers, so only the Details read
  // can land, and nothing of this file's is left in flight after it ends.
  const quiet = Object.fromEntries(
    Object.entries(actual)
      .filter(([k, v]) => k.startsWith('load') && typeof v === 'function')
      .map(([k]) => [k, () => new Promise(() => {})]),
  )
  return { ...actual, ...quiet, ...run }
})

const { AgentDetailScreen } = await import('../AgentDetail')
// The real name, re-attached per test: `restoreMocks` strips implementations.
const { agentName: realName } = await vi.importActual<typeof import('../agentlist')>('../agentlist')

const ID = 'task_0123456789abcdef0456'
const STEP = 'write-the-release-notes'

function answer(): Result<AgentRun> {
  const t = runTask({ id: ID, state: 'SUCCEEDED', step_id: STEP })
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: { task: t, events: [], eventsDetail: null, attempts: [], attemptsDetail: null, classes: null, classesDetail: null, classesRouteMissing: false },
  } as Result<AgentRun>
}

/** A read the test answers when it chooses. */
function deferred(): (r: Result<AgentRun>) => void {
  let settle: (r: Result<AgentRun>) => void = () => {}
  run.loadAgentRun.mockImplementation(() => new Promise<Result<AgentRun>>((resolve) => (settle = resolve)))
  return (r) => settle(r)
}

beforeEach(() => {
  named.agentName.mockImplementation(realName)
})

describe('the Details read names its pane only while the pane is mounted', () => {
  it('names the heading from a read that lands while the pane is there (control)', async () => {
    const settle = deferred()
    render(<AgentDetailScreen taskId={ID} onClose={() => {}} />)
    expect(run.loadAgentRun).toHaveBeenCalledWith(ID)
    await act(async () => {
      settle(answer())
      await new Promise((r) => setTimeout(r, 0))
    })
    expect(named.agentName).toHaveBeenCalled()
    expect(screen.getAllByText(STEP).length).toBeGreaterThan(0)
  })

  it('does nothing with a read that lands after the pane unmounted', async () => {
    const settle = deferred()
    const r = render(<AgentDetailScreen taskId={ID} onClose={() => {}} />)
    expect(run.loadAgentRun).toHaveBeenCalledWith(ID)
    r.unmount()
    await act(async () => {
      settle(answer())
      await new Promise((resolve) => setTimeout(resolve, 0))
    })
    expect(named.agentName, 'a read that outlived its pane set its name').not.toHaveBeenCalled()
  })
})
