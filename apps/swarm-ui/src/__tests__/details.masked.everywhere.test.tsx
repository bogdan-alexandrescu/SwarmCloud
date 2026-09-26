// DETAILS AFTER THE OWNER'S DECISIONS OF 2026-09-26 (#184, #222).
//
//   * TASK METADATA MASKED TOO ("mask it everywhere"). Details drew
//     `task.metadata` raw, between the masked prompt and the masked rest of
//     the input: `TaskCreate.metadata` is caller-supplied, and a token in it
//     was drawn in clear. It now draws only the `/input` copy's `metadata`
//     block, with its own `masked N`, and never `task.metadata` in its place.
//   * LOG LOCATIONS MOVE. Details' Output listed `result_summary.logs` -- the
//     stdout and stderr object locations with `copy gsutil`. "Details holds
//     no log rows"; the locations sit beside their logs in Artifacts › Logs.
//   * #222 (a): the card foot's "reading these cards" index still listed
//     "CPU is never sampled", directly under the two CPU rows it contradicts.
//
// MUTATIONS: draw `task.metadata` again, or fall back to it when the copy
// failed or does not carry the block; drop the metadata block's count; put the
// `result_summary.logs` rows back in Output; list `cpu-not-sampled` again.

import { describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import type { AgentRun } from '../api'
import type { Task } from '../types'
import { attempt, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadTaskInputOnce: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { Run } = await import('../AgentDetail')

const WAIT = { timeout: 5000 }

/** Shaped like values `redaction.RULES` masks. Not real credentials. */
const META_SECRET = 'correct-horse-battery-staple-8812'
const LOG_URI = 'gs://swarm-artifacts/tenants/acme/tasks/tsk_charts/attempts/att_1/logs/stdout.log'

function inputCopy(over: Record<string, unknown> = {}) {
  return {
    task_id: 'tsk_charts',
    tenant_id: 'acme',
    read_at: new Date().toISOString(),
    prompt_key: 'string',
    prompt: { text: 'Audit the capacity code.', redaction_count: 0 },
    rest: null,
    full: { text: '{\n  "prompt": "Audit the capacity code."\n}', redaction_count: 0 },
    metadata: {
      value: {
        dispatch: { strategy: 'collect', carrier: 'checkpoints' },
        ci_token: '********',
        unit: 'fanout-3',
      },
      redaction_count: 1,
      platform_keys: ['dispatch'],
    },
    redacted: false,
    redaction_count: 0,
    redaction: { applied_at_read_time: true, rules: 11 },
    ...over,
  }
}

let served: unknown = null

function agentRun(t: Partial<Task> = {}, input: unknown = { status: 'ok', data: inputCopy(), fetchedAt: Date.now() }): AgentRun {
  served = input
  const run = {
    task: task({
      state: 'SUCCEEDED',
      attempt_count: 1,
      input: { prompt: 'Audit the capacity code.' },
      // The RAW document, as an API before the change would serve it: the
      // screen must never draw this.
      metadata: { dispatch: { strategy: 'collect', carrier: 'checkpoints' }, ci_token: META_SECRET, unit: 'fanout-3' },
      result_summary: {
        artifacts: [],
        logs: { stdout: LOG_URI, stderr: LOG_URI.replace('stdout', 'stderr') },
        git: { base: '0a09be9f3c1d2e4b5a6978' },
      },
      ...t,
    }),
    events: [],
    eventsDetail: null,
    attempts: [attempt(1)],
    attemptsDetail: null,
    classes: { standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 4, units: 1 } },
    classesDetail: null,
    classesRouteMissing: false,
  }
  return run as unknown as AgentRun
}

async function mount(run: AgentRun): Promise<HTMLElement> {
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskInputOnce.mockImplementation(async () => served)
  const { container } = render(<Run run={run} />)
  await waitFor(() => expect(container.querySelector('.ctl-metrics')).not.toBeNull(), WAIT)
  await waitFor(() => expect(section(container as HTMLElement, 'Input')?.querySelector('.ctl-mark.is-pending') ?? null).toBeNull(), WAIT)
  return container as HTMLElement
}

function section(root: HTMLElement, title: string): HTMLElement | null {
  return [...root.querySelectorAll<HTMLElement>('section')].find((s) => s.querySelector('h2')?.textContent === title) ?? null
}

/** The sub-block of Input headed `metadata`. */
function metadataBlock(root: HTMLElement): HTMLElement {
  const input = section(root, 'Input')
  expect(input, 'no Input section').not.toBeNull()
  const eyebrow = [...input!.querySelectorAll('.ctl-eyebrow')].find((e) => e.textContent?.trim() === 'metadata')
  expect(eyebrow, 'no metadata block in Input').toBeTruthy()
  return eyebrow!.closest('.section') as HTMLElement
}

describe('Details draws the task’s metadata as the API masked it', () => {
  it('draws the masked copy with its count, and never the raw document', async () => {
    const root = await mount(agentRun())
    expect(root.textContent, 'the raw metadata reached the screen').not.toContain(META_SECRET)
    const block = metadataBlock(root)
    const rows = Object.fromEntries(
      [...block.querySelectorAll('tbody tr')].map((tr) => [
        (tr.querySelector('th')?.firstChild?.textContent ?? '').trim(),
        tr.querySelector('td')?.textContent ?? '',
      ]),
    )
    expect(rows['ci_token']).toBe('********')
    expect(rows['unit']).toBe('fanout-3')
    const count = block.querySelector('.art-masked')
    expect(count, 'no masked count beside the metadata').not.toBeNull()
    expect(count!.textContent).toBe('1')
    expect(count!.classList.contains('is-warn'), 'a count above zero is drawn as nothing to see').toBe(true)
  })

  it('says which rows the platform wrote and were served as stored', async () => {
    const root = await mount(agentRun())
    const block = metadataBlock(root)
    const dispatchRow = [...block.querySelectorAll('tbody tr')].find((tr) => tr.querySelector('th')?.textContent?.startsWith('dispatch'))
    expect(dispatchRow, 'the platform key is not drawn').toBeTruthy()
    expect(dispatchRow!.querySelector('th')!.textContent).toMatch(/platform/)
    const tokenRow = [...block.querySelectorAll('tbody tr')].find((tr) => tr.querySelector('th')?.textContent?.startsWith('ci_token'))
    expect(tokenRow!.querySelector('th')!.textContent, 'a caller key is called the platform’s').not.toMatch(/platform/)
  })

  it('never falls back to the raw metadata when the copy could not be read', async () => {
    const failed = { status: 'error', error: { kind: 'upstream_degraded', httpStatus: 503, code: 'upstream_unavailable', message: 'Busy.' } }
    const root = await mount(agentRun({}, failed))
    expect(root.textContent).not.toContain(META_SECRET)
    expect(metadataBlock(root).querySelector('.ctl-mark.is-unread'), 'a failed read is not marked as one').not.toBeNull()
    expect(metadataBlock(root).querySelector('table'), 'a table was drawn with no copy to draw it from').toBeNull()
  })

  it('says an API that serves no masked metadata does not, rather than drawing the raw one', async () => {
    const copy = inputCopy()
    delete (copy as Record<string, unknown>)['metadata']
    const root = await mount(agentRun({}, { status: 'ok', data: copy, fetchedAt: Date.now() }))
    expect(root.textContent).not.toContain(META_SECRET)
    const block = metadataBlock(root)
    expect(block.querySelector('table')).toBeNull()
    expect(block.textContent).toMatch(/not served by this API/)
    expect(block.querySelector('.ctl-mark.is-absent')).not.toBeNull()
  })
})

describe('Details holds no log rows', () => {
  it('lists no log object location and copies no gsutil line for one in Output', async () => {
    const root = await mount(agentRun())
    const output = section(root, 'Output')
    expect(output, 'no Output section').not.toBeNull()
    expect(output!.textContent, 'Output still lists a log location').not.toContain(LOG_URI)
    const eyebrows = [...output!.querySelectorAll('.ctl-eyebrow')].map((e) => e.textContent?.trim())
    expect(eyebrows, 'Output still has a logs block').not.toContain('logs')
    expect(
      [...output!.querySelectorAll('button')].some((b) => b.textContent === 'copy gsutil' && (b.closest('.ctl-fact')?.textContent ?? '').includes('logs/')),
      'a log location’s copy action is still in Output',
    ).toBe(false)
  })
})

describe('the card foot’s index no longer says CPU is never sampled (#222 a)', () => {
  it('lists what the CPU rows are, and not the retired claim', async () => {
    const root = await mount(agentRun())
    const legend = root.querySelector('.att-legend')
    expect(legend, 'no reading-these-cards index').not.toBeNull()
    expect(legend!.textContent, 'the index contradicts the CPU rows above it').not.toMatch(/never sampled/i)
    expect(legend!.textContent).toMatch(/CPU is peak and mean cores/)
  })
})
