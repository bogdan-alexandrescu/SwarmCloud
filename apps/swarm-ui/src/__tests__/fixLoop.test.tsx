// THE FIX LOOP ON THE WORKFLOW PAGE (docs/design/fix-loop.md §6.2, lane FL-UI).
//
// What this file pins:
//   * the loop step's row reads `round k of N`: k the last round claimed, N the
//     signed `max_rounds` re-capped as swarm-api re-caps it, plus the rounds a
//     person added, never past the ceiling of 10;
//   * the two actions -- Add a round, Stop -- are buttons, drawn only on a loop
//     parked HUMAN_REQUIRED on one of §4.4's person phases, and each calls its
//     route (`POST /v1/workflows/{id}/fix-loop/rounds` with one round, `.../end`
//     with `stop`);
//   * each round row (`review-fix:k`, `re-review:k`) links its own agent, and
//     no other row does.
//
// BREAK IT: drop the `added` sum in `fixLoopRoundsOf` -- the added-round case
// reads `of 3`. Offer the actions on any HUMAN_REQUIRED park -- the
// other-phase case finds a button. Send the end route `{}` -- the stop case
// fails on its body. Match round ids without the `:<n>` -- the bare
// `re-review` row grows a link.

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { NO_ATTEMPT_YET, absentCell } from '../measure'
import { agentPath } from '../OverviewRegions'
import { stepTimes } from '../stepviews'
import type { Task, WorkflowStep } from '../types'
import { fixLoopOf, fixLoopRoundOf, fixLoopRoundsOf } from '../types'
import { WorkflowTable, type StepLook, type StepRowModel } from '../WorkflowViews'
import { task } from './runfixture'

const api = vi.hoisted(() => ({ loadTask: vi.fn(), write: vi.fn() }))

vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  USE_FIXTURES: false,
  loadTask: api.loadTask,
}))
vi.mock('../fetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../fetch')>()),
  write: api.write,
}))
vi.mock('../Profile', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../Profile')>()),
  useRoleGate: () => null,
}))

const WF = 'wf_loop'
const NOW = Date.UTC(2026, 10, 9, 10, 0, 0)

const LOOKS: Readonly<Record<'parked' | 'running' | 'succeeded', StepLook>> = {
  parked: { tone: 'wait', word: 'parked', title: 'parked', mark: 'parked', hue: 'park' },
  running: { tone: 'live', word: 'running', title: 'running', mark: 'running', hue: 'live' },
  succeeded: { tone: 'ok', word: 'succeeded', title: 'succeeded', mark: 'succeeded', hue: 'neu' },
}

function row(stepId: string, taskId: string | null, look: keyof typeof LOOKS, profile = 'claude-code'): StepRowModel {
  const step: WorkflowStep = {
    step_id: stepId,
    runner_profile: profile,
    resource_class: 'standard',
    depends_on: [],
    input_from: {},
    task_id: taskId,
  }
  const absent = absentCell(NO_ATTEMPT_YET)
  return {
    step,
    taskId,
    look: LOOKS[look],
    times: stepTimes({ kind: 'unstarted' }, NOW),
    ran: absent,
    attempts: absent,
    attemptsOver: 0,
    cost: absent,
    tokens: absent,
    costFrom: null,
    tokensFrom: null,
    result: null,
    pending: false,
    inputs: { declared: new Map(), stray: [], malformed: 0 },
    why: null,
    sort: { order: 0, stateRank: null, waitedMs: null, ranMs: null, attempts: null, costUsd: null, tokens: null },
  }
}

const ROUNDS = [
  { round: 1, kind: 'review', task_id: 'tsk_rr1', verdict: 'NOT_YET', findings_count: 2, state: 'SUCCEEDED' },
  { round: 2, kind: 'fix', task_id: 'tsk_rf2', state: 'SUCCEEDED' },
  { round: 2, kind: 'review', task_id: 'tsk_rr2', verdict: 'NOT_YET', findings_count: 1, state: 'SUCCEEDED' },
]

function loopTask(over: Partial<Task> = {}, state: Record<string, unknown> = { rounds: ROUNDS }, maxRounds: unknown = 3): Task {
  return task({
    id: 'tsk_loop',
    runner_profile: 'fix-loop',
    workflow_id: WF,
    step_id: 'loop',
    state: 'PARKED',
    park_reason: 'FIX_ROUNDS_RUNNING',
    metadata: { dispatch: { fix_loop: { review: 'review', fix: 'fix', max_rounds: maxRounds } }, fix_loop_state: state },
    ...over,
  })
}

const PERSON_PARK: Partial<Task> = {
  park_reason: 'HUMAN_REQUIRED',
  blocked_by: [{ park_phase: 'fix_loop_exhausted', fix_loop: { task_id: 'tsk_rr2', round: 2, max_rounds: 2 } }] as unknown as Task['blocked_by'],
}

function table(rows: StepRowModel[]) {
  return render(<WorkflowTable rows={rows} picked={null} onPick={() => undefined} />)
}

function loopRow(): StepRowModel {
  return row('loop', 'tsk_loop', 'parked', 'fix-loop')
}

beforeEach(() => {
  api.loadTask.mockReset()
  api.write.mockReset()
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('round k of N', () => {
  it('reads the last round claimed over the signed max_rounds', () => {
    const loop = fixLoopOf(loopTask())!
    expect(loop.round).toBe(2)
    expect(loop.of).toBe(3)
    expect(loop.waiting).toBe(false)
    expect(loop.verdict).toBe('NOT_YET')
    expect(loop.findings).toBe(1)
  })

  it('counts the rounds a person added, never past the ceiling', () => {
    const added = { rounds: ROUNDS, added: [{ by: 'm', at: '2026-11-09T09:00:00Z', rounds: 1 }] }
    expect(fixLoopRoundsOf(loopTask({}, added))).toBe(4)
    const many = { rounds: [], added: [{ by: 'm', at: '2026-11-09T09:00:00Z', rounds: 40 }] }
    expect(fixLoopRoundsOf(loopTask({}, many, 5))).toBe(10)
  })

  it('re-caps the signed max_rounds to 1-5 and defaults it to 3, as swarm-api does', () => {
    expect(fixLoopRoundsOf(loopTask({}, {}, 9))).toBe(5)
    expect(fixLoopRoundsOf(loopTask({}, {}, 0))).toBe(1)
    expect(fixLoopRoundsOf(loopTask({}, {}, 'three'))).toBe(3)
    expect(fixLoopRoundsOf(loopTask({ metadata: null }))).toBeNull()
  })

  it('names the half that runs while the rounds run', () => {
    const live = [...ROUNDS, { round: 3, kind: 'fix', task_id: 'tsk_rf3' }]
    expect(fixLoopOf(loopTask({}, { rounds: live }))!.running).toBe('fix')
    expect(fixLoopOf(loopTask())!.running).toBeNull()
    expect(fixLoopOf(task({ runner_profile: 'claude-code' }))).toBeNull()
  })

  it('draws round k of N on the loop step row, and rounds running while they run', async () => {
    api.loadTask.mockResolvedValue({ status: 'ok', data: loopTask(), fetchedAt: NOW })
    table([loopRow()])
    const tr = document.querySelector('tr[data-step="loop"]') as HTMLElement
    await waitFor(() => expect(within(tr).getByText('round 2 of 3')).toBeTruthy())
    expect(api.loadTask).toHaveBeenCalledWith('tsk_loop')
    expect(tr.querySelector('[data-loop="running"]')?.textContent).toContain('rounds running')
    expect(screen.queryByRole('button', { name: 'Add a round' })).toBeNull()
  })
})

describe('the two actions on a loop parked for a person', () => {
  it('appear on each person phase and on nothing else', async () => {
    for (const phase of ['fix_loop_exhausted', 'fix_changed_nothing', 'round_failed', 'review_unreadable', 'round_refused']) {
      const parked = loopTask({ ...PERSON_PARK, blocked_by: [{ park_phase: phase, fix_loop: {} }] as unknown as Task['blocked_by'] })
      expect(fixLoopOf(parked)!.waiting, phase).toBe(true)
      expect(fixLoopOf(parked)!.phase).toBe(phase)
    }
    // A HUMAN_REQUIRED park that is not a loop phase, a running loop, an ended one.
    const other = loopTask({ park_reason: 'HUMAN_REQUIRED', blocked_by: [{ park_phase: 'owner_questions' }] as unknown as Task['blocked_by'] })
    expect(fixLoopOf(other)!.waiting).toBe(false)
    expect(fixLoopOf(loopTask())!.waiting).toBe(false)
    expect(fixLoopOf(loopTask({ ...PERSON_PARK, state: 'FAILED' }))!.waiting).toBe(false)

    api.loadTask.mockResolvedValue({ status: 'ok', data: other, fetchedAt: NOW })
    table([loopRow()])
    await waitFor(() => expect(screen.getByText('round 2 of 3')).toBeTruthy())
    expect(screen.queryByRole('button', { name: 'Add a round' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull()
  })

  it('Add a round posts one round to the rounds route', async () => {
    api.loadTask.mockResolvedValue({ status: 'ok', data: loopTask(PERSON_PARK), fetchedAt: NOW })
    api.write.mockResolvedValue({ status: 'ok', data: { fix_loop: { task_id: 'tsk_loop', max_rounds: 4 } }, fetchedAt: NOW })
    table([loopRow()])
    const add = await screen.findByRole('button', { name: 'Add a round' })
    expect(add.tagName).toBe('BUTTON')
    expect(document.querySelector('[data-loop="waiting"]')?.textContent).toContain('waiting for you')
    fireEvent.click(add)
    await waitFor(() => expect(api.write).toHaveBeenCalledTimes(1))
    const [target, method, body] = api.write.mock.calls[0]!
    expect(target.url).toBe(`/v1/workflows/${WF}/fix-loop/rounds`)
    expect(method).toBe('POST')
    expect(body).toEqual({ rounds: 1 })
    // The answer reads the loop again.
    await waitFor(() => expect(api.loadTask).toHaveBeenCalledTimes(2))
  })

  it('Stop asks once more, then posts stop to the end route', async () => {
    api.loadTask.mockResolvedValue({ status: 'ok', data: loopTask(PERSON_PARK), fetchedAt: NOW })
    api.write.mockResolvedValue({ status: 'ok', data: { fix_loop: { task_id: 'tsk_loop', state: 'FAILED' } }, fetchedAt: NOW })
    table([loopRow()])
    const stop = await screen.findByRole('button', { name: 'Stop' })
    expect(stop.tagName).toBe('BUTTON')
    fireEvent.click(stop)
    expect(api.write).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Stop the loop' }))
    await waitFor(() => expect(api.write).toHaveBeenCalledTimes(1))
    const [target, method, body] = api.write.mock.calls[0]!
    expect(target.url).toBe(`/v1/workflows/${WF}/fix-loop/end`)
    expect(method).toBe('POST')
    expect(body).toEqual({ outcome: 'stop' })
  })

  it('a refused answer says so, and keeps the buttons', async () => {
    api.loadTask.mockResolvedValue({ status: 'ok', data: loopTask(PERSON_PARK), fetchedAt: NOW })
    api.write.mockResolvedValue({
      status: 'error',
      error: { kind: 'conflict', httpStatus: 409, code: 'conflict', message: 'the fix loop is not parked for a person' },
    })
    table([loopRow()])
    fireEvent.click(await screen.findByRole('button', { name: 'Add a round' }))
    const alert = await screen.findByRole('alert')
    expect(alert.getAttribute('title')).toBe('the fix loop is not parked for a person')
    expect(screen.getByRole('button', { name: 'Add a round' })).toBeTruthy()
  })
})

describe('round rows link their agents', () => {
  it('each review-fix:k and re-review:k row opens its own agent, and no other row links one', async () => {
    api.loadTask.mockResolvedValue({ status: 'ok', data: loopTask(), fetchedAt: NOW })
    table([
      row('review', 'tsk_review', 'succeeded'),
      row('re-review', 'tsk_bare', 'succeeded'),
      loopRow(),
      row('re-review:1', 'tsk_rr1', 'succeeded'),
      row('review-fix:2', 'tsk_rf2', 'running'),
      row('re-review:2', null, 'parked'),
    ])
    await waitFor(() => expect(screen.getByText('round 2 of 3')).toBeTruthy())
    const rr1 = screen.getByRole('link', { name: 'Open the agent of re-review:1' })
    expect(rr1.getAttribute('href')).toBe(agentPath({ id: 'tsk_rr1', state: 'SUCCEEDED' }))
    const rf2 = screen.getByRole('link', { name: 'Open the agent of review-fix:2' })
    expect(rf2.getAttribute('href')).toBe(agentPath({ id: 'tsk_rf2', state: 'RUNNING' }))
    // A round with no task yet, the bare `re-review`, the review and the loop link nothing.
    expect(document.querySelectorAll('a.wf-round-agent')).toHaveLength(2)
    expect(fixLoopRoundOf('re-review')).toBeNull()
    expect(fixLoopRoundOf('ci-fix:2')).toBeNull()
    expect(fixLoopRoundOf('review-fix:3')).toEqual({ kind: 'fix', round: 3 })
  })
})
