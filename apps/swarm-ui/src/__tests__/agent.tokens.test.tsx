// THE TOKENS TILE COUNTS EVERY KIND THE RUN USED (#322).
//
// The tile summed input + output, so task_9c0ade75fd0c4f1c9a6e -- which read
// 1.34M cached tokens -- showed 10,007 beside its cost. The owner's decision
// (2026-09-29, revising 2026-09-28): TOKENS ONLY, no dollars. The headline is
// the total of all four kinds; the caption gives each kind's count, as
// `in 52 · out 9,955 · cache read 1.34M · write 59.7k`. 5m and 1h cache writes
// are split when both are present. A kind no attempt reported is left out,
// never shown as 0. The counts come from `modelUsage`, not the top-level
// `usage` block, which can cover only the last result event (#323). A profile
// with no provider reads `no model call`; `not reported` is kept for a model
// profile whose usage is actually missing.
//
// MUTATIONS, one per block: sum `in + out` again; read the attempt documents
// when `modelUsage` is there; draw `write` unsplit, or split on one kind;
// print a kind no attempt reported as 0; say `not reported` for mock.

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

const { Run, tokenKinds } = await import('../AgentDetail')

const DONE = { state: 'SUCCEEDED', attempt_count: 1, started_at: at(1), completed_at: at(10) } as const

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

async function tokensTile(r: AgentRun): Promise<HTMLElement> {
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  const { container } = render(<Run run={r} />)
  await waitFor(() => expect(container.querySelector('.ctl-metrics')).not.toBeNull())
  const found = [...container.querySelectorAll<HTMLElement>('.ctl-metric')].find(
    (m) => m.querySelector('.ctl-metric-label')?.textContent?.startsWith('Tokens'),
  )
  expect(found, 'no Tokens tile').toBeTruthy()
  return found!
}

const value = (tile: HTMLElement) => tile.querySelector('.ctl-metric-value')?.textContent ?? ''

/** The usage block of task_9c0ade75fd0c4f1c9a6e, as the worker stores it. */
function summary(cacheCreation: Record<string, number> | null, models: Record<string, Record<string, number>>) {
  return {
    runner: {
      output: {
        structured_output: {
          // The top-level block covers the LAST result event only.
          usage: {
            input_tokens: 3,
            output_tokens: 500,
            cache_read_input_tokens: 1000,
            cache_creation_input_tokens: 0,
            ...(cacheCreation === null ? {} : { cache_creation: cacheCreation }),
          },
          modelUsage: models,
        },
      },
    },
  }
}

const OPUS = {
  'claude-opus-5-5': { inputTokens: 52, outputTokens: 9955, cacheReadInputTokens: 1_337_190, cacheCreationInputTokens: 59_715 },
}

describe('#322: the headline is all four kinds, the caption each one', () => {
  it('reads 1.41M for the run the issue measured, from modelUsage, not the last-event usage block', async () => {
    const t = task({ ...DONE, result_summary: summary({ ephemeral_1h_input_tokens: 59_715, ephemeral_5m_input_tokens: 0 }, OPUS) })
    const a = attempt(1, { input_tokens: 3, output_tokens: 500, cache_read_input_tokens: 1000, cache_creation_input_tokens: 0 })
    const tile = await tokensTile(run(t, [a]))
    expect(value(tile)).toBe('1.41M')
    expect(tile.textContent).toContain('in 52 · out 9,955 · cache read 1.34M · write 59.7k')
    expect(tile.textContent).not.toMatch(/\$/)
    expect(tile.textContent).not.toMatch(/in \+ out/)
  })

  it('sums every model modelUsage names', () => {
    const t = task({
      ...DONE,
      result_summary: summary(null, {
        ...OPUS,
        'claude-haiku-4-5': { inputTokens: 8, outputTokens: 45, cacheReadInputTokens: 10, cacheCreationInputTokens: 0 },
      }),
    })
    const k = tokenKinds(t, [attempt(1, { input_tokens: 3 })])
    expect(k).toMatchObject({ input: 60, output: 10_000, cacheRead: 1_337_200, cacheWrite: 59_715 })
  })

  it('sums the attempt documents, kind by kind, when there is no modelUsage', async () => {
    const tile = await tokensTile(
      run(task({ ...DONE, attempt_count: 2, completed_at: at(20) }), [
        attempt(1, { input_tokens: 100, output_tokens: 50, cache_read_input_tokens: 600_000, cache_creation_input_tokens: 10_000 }),
        attempt(2, { created_at: at(11), input_tokens: 100, output_tokens: 50, cache_read_input_tokens: 400_000, cache_creation_input_tokens: null }),
      ]),
    )
    expect(value(tile)).toBe('1.01M')
    expect(tile.textContent).toContain('in 200 · out 100 · cache read 1M · write 10k')
  })

  it('takes modelUsage for the attempt that wrote the summary and the documents for the others', () => {
    const t = task({ ...DONE, attempt_count: 2, result_summary: summary(null, OPUS) })
    const k = tokenKinds(t, [
      attempt(1, { input_tokens: 10, output_tokens: 20, cache_read_input_tokens: null, cache_creation_input_tokens: null }),
      attempt(2, { created_at: at(11), input_tokens: 3, output_tokens: 500, cache_read_input_tokens: 1000, cache_creation_input_tokens: 0 }),
    ])
    expect(k).toMatchObject({ input: 62, output: 9_975, cacheRead: 1_337_190, cacheWrite: 59_715 })
  })
})

describe('#322: a kind no attempt reported is left out, never 0', () => {
  it('draws no cache in the caption when no attempt reported one', async () => {
    const tile = await tokensTile(run(task(DONE), [attempt(1, { input_tokens: 100, output_tokens: 50 })]))
    expect(value(tile)).toBe('150')
    expect(tile.textContent).toContain('in 100 · out 50')
    expect(tile.textContent).not.toMatch(/cache|write/)
  })

  it('leaves out a kind modelUsage does not carry', () => {
    const t = task({ ...DONE, result_summary: summary(null, { m: { inputTokens: 5, outputTokens: 7 } }) })
    const k = tokenKinds(t, [attempt(1)])
    expect(k.cacheRead).toBeNull()
    expect(k.cacheWrite).toBeNull()
  })
})

describe('#322: 5m and 1h cache writes are split when both are present', () => {
  it('splits a write whose two parts are both present and add up to it', async () => {
    const models = { m: { inputTokens: 1, outputTokens: 2, cacheReadInputTokens: 3, cacheCreationInputTokens: 15_000 } }
    const t = task({ ...DONE, result_summary: summary({ ephemeral_5m_input_tokens: 5000, ephemeral_1h_input_tokens: 10_000 }, models) })
    const tile = await tokensTile(run(t, [attempt(1, { input_tokens: 1 })]))
    expect(tile.textContent).toContain('write 5m 5,000 · write 1h 10k')
  })

  it('keeps one write figure when only one duration was used', async () => {
    const t = task({ ...DONE, result_summary: summary({ ephemeral_5m_input_tokens: 0, ephemeral_1h_input_tokens: 59_715 }, OPUS) })
    const tile = await tokensTile(run(t, [attempt(1, { input_tokens: 3 })]))
    expect(tile.textContent).toContain('write 59.7k')
    expect(tile.textContent).not.toMatch(/write 5m|write 1h/)
  })

  it('does not split when the parts cover less than the write (the last result event only)', () => {
    const models = { m: { inputTokens: 1, outputTokens: 2, cacheReadInputTokens: 3, cacheCreationInputTokens: 150_000 } }
    const t = task({ ...DONE, result_summary: summary({ ephemeral_5m_input_tokens: 5000, ephemeral_1h_input_tokens: 10_000 }, models) })
    const k = tokenKinds(t, [attempt(1)])
    expect(k.cacheWrite).toBe(150_000)
    expect(k.write5m).toBeNull()
    expect(k.write1h).toBeNull()
  })
})

describe('#322: no model call is not a missing measurement', () => {
  it('reads "no model call" for a profile with no provider', async () => {
    const tile = await tokensTile(run(task({ ...DONE, runner_profile: 'mock', provider: null }), [attempt(1)]))
    expect(value(tile)).toBe('no model call')
    expect(tile.textContent).not.toMatch(/not reported/)
  })

  it('keeps "not reported" for a model profile whose usage is missing', async () => {
    const tile = await tokensTile(run(task(DONE), [attempt(1)]))
    expect(value(tile)).toBe('not reported')
  })
})

// THE SUMMARY IS THE NEWEST ATTEMPT'S ONLY ONCE THE TASK HAS FINISHED (the
// review of #322). `fail_retryably` writes the failed attempt's summary and
// sends the task back to READY, so a task on its second attempt still carries
// the first one's. Borrowing it for the newest attempt counted attempt 1 twice.
// MUTATION: drop the `resultIsNewest` / `completed_at` guard in `tokenKinds`.
describe('#322: modelUsage stands in only for the attempt that wrote it', () => {
  const FIRST = { input_tokens: 52, output_tokens: 9955, cache_read_input_tokens: 1_337_190, cache_creation_input_tokens: 59_715 }
  const NONE = { input_tokens: null, output_tokens: null, cache_read_input_tokens: null, cache_creation_input_tokens: null }

  it('counts attempt 1 once on a RUNNING retry that carries its summary', async () => {
    const t = task({ state: 'RUNNING', attempt_count: 2, started_at: at(11), completed_at: null, result_summary: summary(null, OPUS) })
    const attempts = [attempt(1, FIRST), attempt(2, { ...NONE, created_at: at(11), completed_at: null, exit_code: null })]
    const k = tokenKinds(t, attempts)
    expect(k).toMatchObject({ input: 52, output: 9955, cacheRead: 1_337_190, cacheWrite: 59_715, fromSummary: null })
    const tile = await tokensTile(run(t, attempts))
    expect(value(tile)).toBe('1.41M')
  })

  it('reads the documents for a finished task whose newest attempt ended without writing a summary', () => {
    const t = task({ state: 'FAILED', attempt_count: 2, completed_at: at(20), result_summary: summary(null, OPUS) })
    const k = tokenKinds(t, [attempt(1, FIRST), attempt(2, { ...NONE, created_at: at(11), completed_at: null, exit_code: null })])
    expect(k.input).toBe(52)
    expect(k.cacheRead).toBe(1_337_190)
    expect(k.fromSummary).toBeNull()
  })

  it('counts the attempt modelUsage covered as reported in the coverage foot', async () => {
    const t = task({ ...DONE, attempt_count: 2, completed_at: at(20), result_summary: summary(null, OPUS) })
    const tile = await tokensTile(
      run(t, [attempt(1, { input_tokens: 10, output_tokens: 20 }), attempt(2, { ...NONE, created_at: at(11), completed_at: at(20) })]),
    )
    expect(tile.textContent).not.toMatch(/attempts reported/)
  })
})
