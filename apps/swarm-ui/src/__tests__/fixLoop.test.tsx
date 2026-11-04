/**
 * LANE FL-UI (docs/design/fix-loop.md §6.2, part of #2509): the fix loop on
 * the workflow page.
 *
 * The loop step's row reads `round k of N` -- N the signed `max_rounds` plus
 * the rounds people added, never past the ceiling of 10 (BGD Q4 (a)) -- then
 * the half that is running, then the outcome: `MERGE after k rounds`, or
 * `waiting for you: <phase>` with Add a round and Stop. The appended
 * `review-fix:<k>` / `re-review:<k>` rounds link their own agents. Stop is
 * confirmed before it is sent; a note is at most 4 KiB. A FIX_ROUNDS_RUNNING
 * park reads as rounds running, never as waiting for a person: it holds
 * nothing and ends on its own (invariant 1).
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Task, TaskState, WorkflowStep } from '../types'

const api = vi.hoisted(() => ({
  addFixLoopRound: vi.fn(),
  endFixLoop: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { FixLoopStepCard, FixLoopRowLine } = await import('../WorkflowViews')
const { ParkPill } = await import('../components/StatePill')
const types = await import('../types')
const { fixLoopOf, FIX_LOOP_PROFILE, FIX_LOOP_ROUNDS_CEILING, FIX_LOOP_NOTE_MAX_BYTES } = types

const T0 = Date.parse('2026-10-28T12:00:00.000Z')
const iso = (s: number) => new Date(T0 + s * 1000).toISOString()
const sha = (c: string) => c.repeat(40)

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: 'eng',
    state,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 0,
    created_at: iso(-3600),
    updated_at: iso(-60),
    started_at: null,
    completed_at: null,
    submitted_by: 'alex@example.com',
    attempt_count: 0,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: 'wf_loop',
    step_id: null,
    depends_on: null,
    cancel_requested: false,
    repository_url: null,
    model: null,
    timeout_seconds: null,
    next_eligible_at: null,
    metadata: null,
    repository_ref: null,
    input: null,
    last_error: null,
    result_summary: null,
    latest_checkpoint: null,
    current_generation: 0,
    current_lease_id: null,
    ...over,
  }
}

const ROUND_1 = [
  { round: 1, kind: 'review', task_id: 't_rr1', workflow_id: 'wf_rr1', head: sha('a'), state: 'SUCCEEDED', verdict: 'NOT_YET', findings_count: 2 },
]
const ROUND_2_FIX_RUNNING = [
  ...ROUND_1,
  { round: 2, kind: 'fix', task_id: 't_rf2', workflow_id: 'wf_rf2', start_head: sha('a') },
]

/** The loop step's task: `fix-loop`, its signed block, its rounds so far. */
function loop(state: TaskState, over: Partial<Task> = {}, rounds: unknown[] = ROUND_1, added: unknown[] = [], maxRounds: number | null = 3): Task {
  const block = { review: 'review', fix: 'fix', ...(maxRounds === null ? {} : { max_rounds: maxRounds }) }
  return task('t_loop', state, {
    runner_profile: FIX_LOOP_PROFILE,
    step_id: 'fix-loop',
    metadata: { dispatch: { strategy: 'integrate', fix_loop: block }, fix_loop_state: { rounds, added } },
    ...over,
  })
}

const running = (rounds: unknown[] = ROUND_2_FIX_RUNNING) =>
  loop('PARKED', { park_reason: 'FIX_ROUNDS_RUNNING' }, rounds)

const parked = (phase = 'fix_loop_exhausted', rounds: unknown[] = ROUND_1, added: unknown[] = [], maxRounds: number | null = 1) =>
  loop(
    'PARKED',
    {
      park_reason: 'HUMAN_REQUIRED',
      next_eligible_at: iso(-30),
      blocked_by: [{ park_phase: phase, fix_loop: { task_id: 't_rr1', round: 1, step: 're-review:1' } } as never],
      result_summary: { fix_loop: { rounds, verdict: 'NOT_YET', findings_count: 2, phase } },
    },
    rounds,
    added,
    maxRounds,
  )

const steps: WorkflowStep[] = [
  { step_id: 'review', runner_profile: 'claude-code', resource_class: 'standard', depends_on: [], input_from: {}, task_id: 't_review' },
  { step_id: 'fix-loop', runner_profile: FIX_LOOP_PROFILE, resource_class: 'standard', depends_on: ['review'], input_from: {}, task_id: 't_loop' },
  { step_id: 're-review:1', runner_profile: 'claude-code', resource_class: 'standard', depends_on: ['fix-loop'], input_from: {}, task_id: 't_rr1' },
  { step_id: 'review-fix:2', runner_profile: 'claude-code', resource_class: 'standard', depends_on: ['fix-loop'], input_from: {}, task_id: 't_rf2' },
]

const ok = (data: unknown) => ({ status: 'ok' as const, data, fetchedAt: Date.now() })

beforeEach(() => {
  api.addFixLoopRound.mockReset()
  api.endFixLoop.mockReset()
})
afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('fixLoopOf: round k of N from the dispatch block and the loop state', () => {
  it('is null for a step that is not a fix loop', () => {
    expect(fixLoopOf(task('t_x', 'RUNNING'))).toBeNull()
  })

  it('counts the latest round against the signed max_rounds', () => {
    const v = fixLoopOf(running())!
    expect(v.round).toBe(2)
    expect(v.maxRounds).toBe(3)
    expect(v.running).toBe('fix')
    expect(v.line).toBe('round 2 of 3 · fix running')
  })

  it('reads a re-review half as re-review running', () => {
    const rounds = [...ROUND_2_FIX_RUNNING.slice(0, 1), { ...ROUND_2_FIX_RUNNING[1], state: 'SUCCEEDED', pushed_head: sha('b') }, { round: 2, kind: 'review', task_id: 't_rr2' }]
    expect(fixLoopOf(running(rounds))!.line).toBe('round 2 of 3 · re-review running')
  })

  it('defaults max_rounds to 3 and caps it to 1-5, as the server re-reads it (Q5)', () => {
    expect(fixLoopOf(loop('PARKED', { park_reason: 'FIX_ROUNDS_RUNNING' }, ROUND_1, [], null))!.maxRounds).toBe(3)
    expect(fixLoopOf(loop('PARKED', { park_reason: 'FIX_ROUNDS_RUNNING' }, ROUND_1, [], 9))!.maxRounds).toBe(5)
  })

  it('adds the rounds people added, never past the ceiling of 10 (Q4)', () => {
    const added = [{ by: 'a@example.com', at: iso(-10), rounds: 2 }]
    expect(fixLoopOf(parked('fix_loop_exhausted', ROUND_1, added, 3))!.maxRounds).toBe(5)
    const many = [{ by: 'a@example.com', at: iso(-10), rounds: 40 }]
    const v = fixLoopOf(parked('fix_loop_exhausted', ROUND_1, many, 5))!
    expect(v.maxRounds).toBe(FIX_LOOP_ROUNDS_CEILING)
    expect(v.canAddRound).toBe(false)
  })

  it('says MERGE after k rounds on a loop that ended SUCCEEDED', () => {
    const v = fixLoopOf(
      loop('SUCCEEDED', { result_summary: { fix_loop: { rounds: ROUND_1, verdict: 'MERGE', final_review: 't_rr1', final_head: sha('a') } } }),
    )!
    expect(v.line).toBe('round 1 of 3 · MERGE after 1 round')
  })

  it('says waiting for you with the park phase in words on a person park', () => {
    const v = fixLoopOf(parked('fix_changed_nothing'))!
    expect(v.person).toBe('fix_changed_nothing')
    expect(v.line).toBe('round 1 of 1 · waiting for you: a fix changed nothing')
    expect(v.canAddRound).toBe(true)
  })

  it('prints an unknown phase verbatim rather than hiding it', () => {
    expect(fixLoopOf(parked('some_new_phase'))!.line).toBe('round 1 of 1 · waiting for you: some_new_phase')
  })

  it('says stopped without MERGE on FAILED fix_loop_exhausted', () => {
    const v = fixLoopOf(loop('FAILED', { end_cause: 'fix_loop_exhausted' }))!
    expect(v.line).toBe('round 1 of 3 · ended without MERGE after 1 round')
  })
})

describe('the step row', () => {
  it('draws round k of N beside a fix-loop step', () => {
    render(<FixLoopRowLine task={running()} />)
    expect(screen.getByText('round 2 of 3 · fix running')).toBeTruthy()
  })

  it('draws nothing for any other step', () => {
    const { container } = render(<FixLoopRowLine task={task('t_x', 'RUNNING')} />)
    expect(container.textContent).toBe('')
  })
})

describe('FixLoopStepCard', () => {
  it('links every round row to its own agent', () => {
    render(<FixLoopStepCard workflowId="wf_loop" task={running()} steps={steps} onChanged={() => {}} />)
    const list = screen.getByRole('list', { name: 'Fix loop rounds' })
    const links = within(list).getAllByRole('link')
    expect(links.map((a) => a.textContent)).toEqual(['re-review:1', 'review-fix:2'])
    expect(links[0]!.getAttribute('href')).toContain('t_rr1')
    expect(links[1]!.getAttribute('href')).toContain('t_rf2')
    expect(within(list).getByText(/NOT_YET/)).toBeTruthy()
  })

  it('offers no action while rounds run: it is not waiting for anyone', () => {
    render(<FixLoopStepCard workflowId="wf_loop" task={running()} steps={steps} onChanged={() => {}} />)
    expect(screen.queryByRole('button', { name: 'Add a round' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull()
    expect(screen.queryByText(/waiting for you/)).toBeNull()
  })

  it('Add a round posts the optional note and reloads', async () => {
    api.addFixLoopRound.mockResolvedValue(ok({ fix_loop: { task_id: 't_loop', max_rounds: 2 } }))
    const changed = vi.fn()
    render(<FixLoopStepCard workflowId="wf_loop" task={parked()} steps={steps} onChanged={changed} />)
    expect(screen.getByText(/waiting for you: the rounds ran out without MERGE/)).toBeTruthy()
    fireEvent.change(screen.getByLabelText(/Note for the next fix/), { target: { value: 'keep the API stable' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add a round' }))
    await waitFor(() => expect(api.addFixLoopRound).toHaveBeenCalledWith('wf_loop', 'keep the API stable'))
    await waitFor(() => expect(changed).toHaveBeenCalled())
  })

  it('Add a round sends no note when none is written', async () => {
    api.addFixLoopRound.mockResolvedValue(ok({ fix_loop: {} }))
    render(<FixLoopStepCard workflowId="wf_loop" task={parked()} steps={steps} onChanged={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Add a round' }))
    await waitFor(() => expect(api.addFixLoopRound).toHaveBeenCalledWith('wf_loop', null))
  })

  it('refuses a note over 4 KiB before sending it', () => {
    render(<FixLoopStepCard workflowId="wf_loop" task={parked()} steps={steps} onChanged={() => {}} />)
    fireEvent.change(screen.getByLabelText(/Note for the next fix/), { target: { value: 'é'.repeat(FIX_LOOP_NOTE_MAX_BYTES / 2 + 1) } })
    const add = screen.getByRole('button', { name: 'Add a round' }) as HTMLButtonElement
    expect(add.disabled).toBe(true)
    fireEvent.click(add)
    expect(api.addFixLoopRound).not.toHaveBeenCalled()
  })

  it('Add a round is off at the ceiling, and says why', () => {
    const many = [{ by: 'a@example.com', at: iso(-10), rounds: 9 }]
    render(<FixLoopStepCard workflowId="wf_loop" task={parked('fix_loop_exhausted', ROUND_1, many, 1)} steps={steps} onChanged={() => {}} />)
    const add = screen.getByRole('button', { name: 'Add a round' }) as HTMLButtonElement
    expect(add.disabled).toBe(true)
    expect(screen.getByText(/most any loop may run \(10\)/)).toBeTruthy()
  })

  it('Stop asks first, then posts outcome stop', async () => {
    api.endFixLoop.mockResolvedValue(ok({ fix_loop: { state: 'FAILED' } }))
    const changed = vi.fn()
    render(<FixLoopStepCard workflowId="wf_loop" task={parked()} steps={steps} onChanged={changed} />)
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    expect(api.endFixLoop).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Keep it parked' }))
    expect(screen.queryByRole('button', { name: 'Stop the loop' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    fireEvent.click(screen.getByRole('button', { name: 'Stop the loop' }))
    await waitFor(() => expect(api.endFixLoop).toHaveBeenCalledWith('wf_loop'))
    await waitFor(() => expect(changed).toHaveBeenCalled())
  })

  it('shows the server refusal in words and does not reload', async () => {
    api.addFixLoopRound.mockResolvedValue({
      status: 'error',
      error: { kind: 'conflict', httpStatus: 409, code: 'conflict', message: 'the fix loop is not parked for a person' },
    })
    const changed = vi.fn()
    render(<FixLoopStepCard workflowId="wf_loop" task={parked()} steps={steps} onChanged={changed} />)
    fireEvent.click(screen.getByRole('button', { name: 'Add a round' }))
    expect(await screen.findByText(/not parked for a person/)).toBeTruthy()
    expect(changed).not.toHaveBeenCalled()
  })
})

describe('the two API calls', () => {
  it('post to the routes FL-DRIVE serves, with the bodies it takes', async () => {
    vi.resetModules()
    vi.stubEnv('VITE_LIVE', '1')
    const real = await vi.importActual<typeof import('../api')>('../api')
    const calls: { url: string; init: RequestInit | undefined }[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push({ url: String(url), init })
        return new Response(JSON.stringify({ fix_loop: {} }), { status: 200, headers: { 'content-type': 'application/json' } })
      }),
    )
    for (const res of [await real.addFixLoopRound('wf_loop', 'a note'), await real.addFixLoopRound('wf_loop', null), await real.endFixLoop('wf_loop')]) {
      expect(res.status, JSON.stringify(res)).toBe('ok')
    }
    expect(calls.map((c) => [c.url.replace(/^.*(\/v1\/)/, '$1'), c.init?.method])).toEqual([
      ['/v1/workflows/wf_loop/fix-loop/rounds', 'POST'],
      ['/v1/workflows/wf_loop/fix-loop/rounds', 'POST'],
      ['/v1/workflows/wf_loop/fix-loop/end', 'POST'],
    ])
    expect(JSON.parse(String(calls[0]!.init?.body))).toEqual({ note: 'a note' })
    expect(JSON.parse(String(calls[1]!.init?.body))).toEqual({})
    expect(JSON.parse(String(calls[2]!.init?.body))).toEqual({ outcome: 'stop' })
  })
})

describe('StatePill: a FIX_ROUNDS_RUNNING park', () => {
  it('reads as fix rounds running, never as waiting for you, and carries no needs-a-person flag', () => {
    const { container } = render(<ParkPill reason="FIX_ROUNDS_RUNNING" />)
    expect(container.textContent).toContain('fix rounds running')
    expect(container.textContent).not.toMatch(/waiting for you/)
    expect(container.querySelector('[data-flag="needs-a-person"]')).toBeNull()
  })
})
