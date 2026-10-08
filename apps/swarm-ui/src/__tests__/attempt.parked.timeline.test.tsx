// #163: A PARKED ATTEMPT READS AS PARKED, IN THE NEUTRAL TONE.
//
// The worker ends a parked attempt with `exit_code: 75` (ExitCode.PARKED) and
// the ParkReason in `error`. `outcome()` knew only exit 0 and "anything else",
// so a quota park drew `exit 75` in the failure tone and its reason in the red
// stderr block -- a park read as a crash.
//
// MUTATION: delete the `isParked` branch in `outcome()`. The chip reads
// `exit 75` with `is-bad`, and the first three tests fail. The fourth is the
// control: any other non-zero exit is a failure with or without the branch.
//
// The agent's Details tab (AgentDetail `Run`, rebuilt by #432) draws its own
// attempt card from `attemptOutcome`, which also knew only "non-zero is a
// failure". Both cards take the parked chip from one helper, `parkedOutcome`.
// MUTATION: drop the `isParked(a) ? parkedOutcome(a)` arm from AgentDetail's
// AttemptCard. The Details test fails with `exit 75`.

import { describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { AttemptRow, Task } from '../types'
import { at, attempt, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadAttempts: vi.fn(),
  loadAgentDetail: vi.fn(),
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AttemptTimelineScreen } = await import('../AttemptTimeline')
const { Run } = await import('../AgentDetail')

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

async function mount(t: Task, attempts: AttemptRow[]): Promise<HTMLElement> {
  api.loadAttempts.mockResolvedValue(ok({ attempts }))
  api.loadAgentDetail.mockResolvedValue(ok({ task: t, events: [], eventsDetail: null }))
  const { container } = render(<AttemptTimelineScreen taskId={t.id} />)
  await waitFor(() => expect(container.querySelector('.ctl-card-foot')).not.toBeNull())
  return container as HTMLElement
}

/** The outcome chip on the one attempt card. */
function chip(el: HTMLElement): HTMLElement {
  const c = el.querySelector<HTMLElement>('.att-card .ctl-card-title .sk-st')
  expect(c, 'the attempt card draws no outcome chip').not.toBeNull()
  return c!
}

describe('a parked attempt on the timeline', () => {
  const parked = attempt(1, { exit_code: 75, error: 'PROVIDER_QUOTA_EXHAUSTED', completed_at: at(10) })

  it('reads `parked · <reason>`, not `running` and not `exit 75`', async () => {
    const el = await mount(task({ state: 'PARKED', started_at: at(1) }), [parked])
    const c = chip(el)
    expect(c.textContent).toBe('parked · PROVIDER_QUOTA_EXHAUSTED')
    expect(c.textContent).not.toContain('running')
    expect(c.textContent).not.toContain('exit 75')
  })

  it('draws the attempt in the neutral tone, never the failure tone', async () => {
    const el = await mount(task({ state: 'PARKED', started_at: at(1) }), [parked])
    const c = chip(el)
    expect(c.getAttribute('data-tone') === 'bad', 'a park drawn as a failure').toBe(false)
    expect(c.getAttribute('data-tone') === 'info').toBe(true)
  })

  it('keeps the park reason out of the red stderr block and says it in words', async () => {
    const el = await mount(task({ state: 'PARKED', started_at: at(1) }), [parked])
    expect(el.querySelector('.att-card pre.err'), 'the park reason printed as a failure').toBeNull()
    expect(el.querySelector('.att-card .blocker-copy')?.textContent).toBe(
      'The provider quota is spent. Parked until it resets.',
    )
  })

  it('still draws any other non-zero exit as a failure, with its error', async () => {
    const failed = attempt(1, { exit_code: 1, error: 'Traceback: boom', completed_at: at(10) })
    const el = await mount(task({ state: 'FAILED', started_at: at(1) }), [failed])
    const c = chip(el)
    expect(c.textContent).toBe('exit 1')
    expect(c.getAttribute('data-tone') === 'bad').toBe(true)
    expect(el.querySelector('.att-card pre.err')?.textContent).toBe('Traceback: boom')
  })
})

// THE ISSUE'S ACCEPTANCE CASE: a task that ran 5 minutes, parked for 40 and
// was then cancelled. The task's own span, last start to cancel, is 45m; the
// parked attempt's end (exit 75, written by the worker's park) makes the
// inspector's attempt card time the run itself.
describe('a task cancelled while parked, in the Attempts view', () => {
  function ranFact(el: HTMLElement): string | undefined {
    const facts = Array.from(el.querySelectorAll<HTMLElement>('.att-card .ctl-fact'))
    const f = facts.find((li) => li.querySelector('b')?.textContent === 'ran')
    expect(f, 'the attempt card draws no `ran` fact').not.toBeUndefined()
    return f!.textContent?.replace(/^ran/, '')
  }

  it('times the run, not the last start to the cancel', async () => {
    const parked = attempt(1, {
      started_at: at(1),
      completed_at: at(6),
      exit_code: 75,
      error: 'PROVIDER_QUOTA_EXHAUSTED',
    })
    const el = await mount(task({ state: 'CANCELLED', started_at: at(1), completed_at: at(46) }), [parked])
    expect(ranFact(el)).toBe('5m 0s')
    expect(chip(el).textContent).toBe('parked · PROVIDER_QUOTA_EXHAUSTED')
  })

  it('still times a mid-run cancel to its end', async () => {
    const cancelled = attempt(1, { started_at: at(1), completed_at: at(46), exit_code: 130, error: null })
    const el = await mount(task({ state: 'CANCELLED', started_at: at(1), completed_at: at(46) }), [cancelled])
    expect(ranFact(el)).toBe('45m 0s')
  })
})

describe('a parked attempt on the Details tab', () => {
  async function details(t: Task, attempts: AttemptRow[]): Promise<HTMLElement> {
    api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    const { container } = render(
      <Run
        run={{
          task: t,
          events: [],
          eventsDetail: null,
          attempts,
          attemptsDetail: null,
          classes: {},
          classesDetail: null,
          classesRouteMissing: false,
        }}
      />,
    )
    await waitFor(() => expect(container.querySelector('.att-card')).not.toBeNull())
    return container as HTMLElement
  }

  it('reads `parked · <reason>` in the neutral tone, with no red error block', async () => {
    const parked = attempt(1, { exit_code: 75, error: 'PROVIDER_QUOTA_EXHAUSTED', completed_at: at(10) })
    const el = await details(task({ state: 'PARKED', started_at: at(1), last_error: null }), [parked])
    const c = chip(el)
    expect(c.textContent).toBe('parked · PROVIDER_QUOTA_EXHAUSTED')
    expect(c.getAttribute('data-tone') === 'bad', 'a park drawn as a failure').toBe(false)
    expect(c.getAttribute('data-tone') === 'info').toBe(true)
    expect(el.querySelector('.att-card pre.err'), 'the park reason printed as a failure').toBeNull()
  })

  it('still draws any other non-zero exit as a failure', async () => {
    const failed = attempt(1, { exit_code: 1, error: 'Traceback: boom', completed_at: at(10) })
    const el = await details(task({ state: 'FAILED', started_at: at(1), last_error: null }), [failed])
    const c = chip(el)
    expect(c.textContent).toBe('exit 1')
    expect(c.getAttribute('data-tone') === 'bad').toBe(true)
  })
})
