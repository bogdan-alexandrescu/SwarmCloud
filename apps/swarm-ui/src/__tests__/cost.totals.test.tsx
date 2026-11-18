// THE COST CELLS SHOW WHAT THE WHOLE TASK COST (lane review P1, 2026-10-05).
//
// Measured: `swarm result` and the follow outcome reported only the final
// attempt -- UR1's implement step served $0.51 while its two attempts cost
// $9.64. The API now serves `cost_usd_total`, `cost_incomplete`, `attempts`
// and the last attempt's `last_attempt_cost_usd` on each task. Agent Details'
// Cost cell and the Workflows cost columns show the TOTAL, with the last
// attempt as secondary text; a missing attempt cost reads `at least`, never a
// silent total that looks whole. One attempt reads as it always did.
//
// MUTATIONS: drop `at least`; show the last attempt's figure as the headline;
// prefer the result summary over the served total on the board.

import { describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import type { AgentRun } from '../api'
import type { AttemptRow, Task } from '../types'
import { at, attempt, ev, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadWorkflow: vi.fn(),
  loadArtifactContent: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { Run } = await import('../AgentDetail')
const { stepCostOf, totalCostCell, workflowSpend } = await import('../dag')

const DONE = { state: 'SUCCEEDED', attempt_count: 2, started_at: at(30), completed_at: at(40) } as const

function run(t: Task, attempts: AttemptRow[]): AgentRun {
  return {
    task: t,
    events: [ev('submitted', at(-1), null)],
    eventsDetail: null,
    attempts,
    attemptsDetail: null,
    classes: { standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 4, units: 1 } },
    classesDetail: null,
    classesRouteMissing: false,
  }
}

async function costCell(r: AgentRun): Promise<{ value: string; sub: string }> {
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  const { container } = render(<Run run={r} />)
  await waitFor(() => expect(container.querySelector('.dt-strip')).not.toBeNull())
  const found = [...container.querySelectorAll<HTMLElement>('.dt-sc')].find(
    (m) => m.querySelector('.dt-sc-l')?.firstChild?.textContent === 'Cost',
  )
  expect(found, 'no Cost cell').toBeTruthy()
  return {
    value: found!.querySelector('.dt-sc-v')?.textContent ?? '',
    sub: found!.querySelector('.dt-sc-s')?.textContent ?? '',
  }
}

const twoAttempts = (first: number | null, last: number | null): AttemptRow[] => [
  attempt(1, { created_at: at(0), started_at: at(1), completed_at: at(20), cost_usd: first }),
  attempt(2, { created_at: at(29), started_at: at(30), completed_at: at(40), cost_usd: last }),
]

describe('Agent Details: the Cost cell is the total, the last attempt beside it', () => {
  it('totals both attempts and names the last one as secondary text', async () => {
    const cell = await costCell(run(task({ ...DONE }), twoAttempts(8.13, 1.51)))
    expect(cell.value).toBe('$9.6400')
    expect(cell.sub).toContain('last attempt $1.5100')
  })

  it('says at least when an ended attempt recorded no cost', async () => {
    const cell = await costCell(run(task({ ...DONE }), twoAttempts(null, 1.51)))
    expect(cell.value).toBe('at least $1.5100')
    expect(cell.sub).toContain('cost 1 of 2 reported')
  })

  it('reads a single attempt exactly as before', async () => {
    const cell = await costCell(
      run(task({ ...DONE, attempt_count: 1 }), [attempt(1, { cost_usd: 1.51 })]),
    )
    expect(cell.value).toBe('$1.5100')
    expect(cell.sub).not.toContain('last attempt')
    expect(cell.value).not.toContain('at least')
  })
})

// The step's result summary describes only the attempt that finished.
const lastResult = { runner: { usage: { total_cost_usd: 1.51 } } }

describe('Workflows: a step without attempt telemetry costs the served total', () => {
  it('prefers the API total over the last attempt result', () => {
    const t = task({
      state: 'SUCCEEDED',
      result_summary: lastResult,
      attempts: 2,
      cost_usd_total: 9.64,
      cost_incomplete: false,
      last_attempt_cost_usd: 1.51,
    })
    expect(stepCostOf(t, undefined)).toEqual({ usd: 9.64, from: 'total' })

    const cell = totalCostCell(t)
    expect(cell).toMatchObject({ kind: 'measured', text: '$9.64' })
    expect(cell?.note).toContain('last attempt $1.51')
  })

  it('marks a floor as at least', () => {
    const t = task({
      state: 'SUCCEEDED',
      result_summary: lastResult,
      attempts: 2,
      cost_usd_total: 1.51,
      cost_incomplete: true,
      last_attempt_cost_usd: 1.51,
    })
    expect(totalCostCell(t)).toMatchObject({ kind: 'measured', text: 'at least $1.51' })
  })

  it('sums the served totals into the row total', () => {
    const t = task({ id: 'tsk_a', state: 'SUCCEEDED', result_summary: lastResult, attempts: 2, cost_usd_total: 9.64 })
    const step = { step_id: 'a', task_id: 'tsk_a', runner_profile: 'claude-code' } as never
    const spend = workflowSpend([step], new Map([['tsk_a', t]]))
    expect(spend.usd).toBe(9.64)
    expect(spend.fromResult).toBe(0)
  })

  it('falls back to the result for an API that serves no total, as before', () => {
    const t = task({ state: 'SUCCEEDED', result_summary: lastResult })
    expect(stepCostOf(t, undefined)).toEqual({ usd: 1.51, from: 'result' })
    expect(totalCostCell(t)).toBeNull()
  })
})
