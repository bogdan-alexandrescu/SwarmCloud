// LB-V4 (part of #1030): A PARKED HAND-OFF SAYS SO ONCE, AND THE TASK PAGE'S
// COST AGREES WITH THE GRAPH.
//
// U1, measured in visual QA pass 5 (2026-10-12): a PARKED(HUMAN_REQUIRED) task
// said it three times on the Details tab -- the yellow hand-off banner, a bar
// "waiting for you to sign in · eligible <time>", and a bar "last scheduler
// pass HUMAN_REQUIRED" with a why sentence repeating it. The banner is the one
// statement; "parked since <time>" is folded into it (`HandoffParkedSince`),
// and the other two stay for every other park reason, where they are the only
// explanation.
//
// B8, measured the same day: the workflow graph drew "cost $0.712 · tokens
// 1.06M" for a claude-code-browser step while its Cost card said "no model
// call · this profile reports no spend". `reportsSpend` named claude-code and
// codex only; the graph gates on no profile and reads what was reported.
//
// MUTATIONS: drop the HUMAN_REQUIRED guard in `Alerts` or `Why`; drop
// `claude-code-browser` from `SPEND_PROFILES`; drop the "anything reported"
// override; drop the served-total fallback.

import { describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import type { AgentRun } from '../api'
import type { AttemptRow, Task } from '../types'
import { REASON_COPY } from '../types'
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

const { Run, HandoffParkedSince, parkedSince } = await import('../AgentDetail')

function run(t: Task, attempts: AttemptRow[]): AgentRun {
  return {
    task: t,
    events: [ev('submitted', at(-1), null)],
    eventsDetail: null,
    attempts,
    attemptsDetail: null,
    classes: {
      standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 4, units: 1 },
      browser: { name: 'browser', cpu: 8, memory_gib: 16, disk_gib: 8, units: 2 },
    },
    classesDetail: null,
    classesRouteMissing: false,
  }
}

async function draw(r: AgentRun): Promise<HTMLElement> {
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  const { container } = render(<Run run={r} />)
  await waitFor(() => expect(container.querySelector('.dt-strip')).not.toBeNull())
  return container
}

function costCell(c: HTMLElement): HTMLElement {
  const found = [...c.querySelectorAll<HTMLElement>('.dt-sc')].find(
    (m) => m.querySelector('.dt-sc-l')?.firstChild?.textContent === 'Cost',
  )
  expect(found, 'no Cost cell').toBeTruthy()
  return found!
}

/** The figure and its sub-line, without the help card's prose. */
const shown = (cell: HTMLElement) =>
  `${cell.querySelector('.dt-sc-v')?.textContent ?? ''} ${cell.querySelector('.dt-sc-s')?.textContent ?? ''}`

const PARKED_AT = '2026-10-12T12:43:51Z'

function parked(reason: string, over: Partial<Task> = {}): Task {
  return task({
    state: 'PARKED',
    park_reason: reason as Task['park_reason'],
    runner_profile: 'claude-code-browser',
    resource_class: 'browser',
    started_at: at(1),
    completed_at: null,
    next_eligible_at: PARKED_AT,
    blocked_by: [{ reason }],
    ...over,
  })
}

describe('U1: a parked hand-off says so once, in the banner', () => {
  it('draws no park bar, no scheduler-pass bar and no why sentence for HUMAN_REQUIRED', async () => {
    const c = await draw(run(parked('HUMAN_REQUIRED'), [attempt(1)]))
    const top = c.querySelector('.dt-top')!
    expect(top.textContent).not.toContain('waiting for you')
    expect(top.textContent).not.toContain('last scheduler pass')
    expect(top.textContent).not.toContain('eligible')
    expect(top.textContent).not.toContain(REASON_COPY.HUMAN_REQUIRED)
    expect(c.querySelector('[data-blocked-by]')).toBeNull()
  })

  it('keeps the bars and the sentence for every other park reason', async () => {
    const c = await draw(
      run(parked('PROVIDER_QUOTA_EXHAUSTED', { runner_profile: 'claude-code', resource_class: 'standard' }), [attempt(1)]),
    )
    const top = c.querySelector('.dt-top')!
    expect(top.textContent).toContain('provider quota')
    expect(top.textContent).toContain('eligible')
    expect(top.textContent).toContain('last scheduler pass')
    expect(top.textContent).toContain(REASON_COPY.PROVIDER_QUOTA_EXHAUSTED)
  })

  it('folds "parked since <time>" into the banner, from the park instant', () => {
    const t = parked('HUMAN_REQUIRED')
    expect(parkedSince(t)).toBe(Date.parse(PARKED_AT))
    const { container } = render(<HandoffParkedSince task={t} />)
    expect(container.textContent).toBe(`parked since ${new Date(PARKED_AT).toLocaleString()}`)
  })

  it('falls back to updated_at when the park wrote no instant', () => {
    const t = parked('HUMAN_REQUIRED', { next_eligible_at: null })
    expect(parkedSince(t)).toBe(Date.parse(t.updated_at))
  })

  it('says nothing for a task that is not a parked hand-off', () => {
    for (const t of [parked('PROVIDER_QUOTA_EXHAUSTED'), task({ state: 'RUNNING' })]) {
      expect(parkedSince(t)).toBeNull()
      const { container } = render(<HandoffParkedSince task={t} />)
      expect(container.textContent).toBe('')
    }
  })
})

describe('B8: the Cost card reads what the graph reads', () => {
  const DONE = { state: 'SUCCEEDED', attempt_count: 1, started_at: at(1), completed_at: at(10) } as const
  const SPENT = { cost_usd: 0.712, input_tokens: 40, output_tokens: 8000, cache_read_input_tokens: 1_000_000, cache_creation_input_tokens: 52_000 }

  it('reads $0.712 and 1.06M tokens for a claude-code-browser attempt that reported them', async () => {
    const cell = costCell(await draw(run(task({ ...DONE, runner_profile: 'claude-code-browser' }), [attempt(1, SPENT)])))
    expect(cell.querySelector('.dt-sc-v')?.textContent).toBe('$0.7120')
    expect(shown(cell)).toContain('1.06M tokens')
    expect(shown(cell)).not.toContain('no model call')
  })

  it('says "not reported", not "no model call", for a claude-code-browser attempt that reported nothing', async () => {
    const cell = costCell(await draw(run(task({ ...DONE, runner_profile: 'claude-code-browser' }), [attempt(1)])))
    expect(shown(cell)).not.toContain('no model call')
    expect(cell.querySelector('.dt-sc-v')?.textContent).toContain('not reported')
  })

  it('reads the figure whatever the profile, once one was reported', async () => {
    const cell = costCell(await draw(run(task({ ...DONE, runner_profile: 'some-new-agent' }), [attempt(1, SPENT)])))
    expect(cell.querySelector('.dt-sc-v')?.textContent).toBe('$0.7120')
  })

  it("reads the API's served total, as the graph does, when the attempt documents carry no cost", async () => {
    const t = task({ ...DONE, runner_profile: 'claude-code-browser', cost_usd_total: 0.712, attempts: 1, attempts_read: 'ok' })
    const cell = costCell(await draw(run(t, [attempt(1)])))
    expect(cell.querySelector('.dt-sc-v')?.textContent).toBe('$0.7120')
  })

  it('keeps "no model call" for a profile with no model and nothing reported', async () => {
    const cell = costCell(await draw(run(task({ ...DONE, runner_profile: 'mock', provider: null }), [attempt(1)])))
    expect(cell.querySelector('.dt-sc-v')?.textContent).toBe('no model call')
  })
})
