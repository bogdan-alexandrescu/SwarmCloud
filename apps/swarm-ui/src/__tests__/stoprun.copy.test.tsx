// WHAT THE STOP CONTROL PROMISES WHEN NO WORKER HAS STARTED.
//
// F-8 in the wf_ebb3ab2d65664707a559 incident analysis. Every sentence
// `StopRun` put on screen for a task holding capacity said the same thing:
// the worker acts on the stop "at its next heartbeat". That is true of a
// worker that is running. It is not true of an attempt no worker has started.
// `started_at` is written on the DISPATCHED -> STARTING transition
// (`control.py:412`), so null means no worker has ever started this task and
// nothing has ever heartbeated for it. (A retry still waiting DISPATCHED is
// the same case with a stale `started_at`; see its own test below.) In the
// incident, five DISPATCHED tasks whose GKE pods never ran the
// worker lifecycle were stopped. They were promised a heartbeat that could not
// come, and they sat DISPATCHED for hours after the button was pressed.
//
// WHAT IS TRUE INSTEAD, and so what this file requires the copy to say:
//
//   * a worker that does start sees `cancel_requested` BEFORE it runs
//     anything, finishes the attempt as CANCELLED and exits
//     (`lifecycle.py:269-278`);
//   * if none starts, the reconciler reclaims the attempt once its dispatch
//     deadline passes. On GKE that depends on the reconciler being able to
//     read GKE at all (lane reconciler-gke). For that reason the copy names
//     the reconciler and gives NO time: no "next heartbeat", no minutes.
//
// FOUR PLACES CARRY THE PROMISE, and each is checked: the confirmation's
// facts, the confirmation's accessible name, the note after the request is
// recorded (its text and its accessible name), and the "stop requested" pill's
// title. Attributes are read as well as text. The long form of every claim in
// this component lives in an `aria-label`, so a text-only check would pass
// while a screen reader still read out the false sentence.
//
// THE LAST TEST IS A GUARD, NOT A REPRODUCTION. A task whose worker HAS
// started still gets the heartbeat promise, because there it is true. The easy
// wrong fix is deleting the sentence everywhere, and that test is what fails
// if someone does.

import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import type { Result } from '../fetch'
import type { CancelResult } from '../api'
import type { Task } from '../types'

const api = vi.hoisted(() => ({ cancelTask: vi.fn() }))
vi.mock('../api', () => api)

const { StopRun } = await import('../StopRun')

const THEN = '2026-09-24T03:55:07.000Z'

function task(over: Partial<Task> = {}): Task {
  return {
    id: 'task_2a417cb24edb4d2cb59e',
    tenant_id: 'eng',
    state: 'DISPATCHED',
    runner_profile: 'browser',
    resource_class: 'browser',
    provider: 'anthropic',
    priority: 0,
    created_at: THEN,
    updated_at: THEN,
    // THE CASE UNDER TEST: admitted, dispatched, never started.
    started_at: null,
    completed_at: null,
    submitted_by: 'ada@eng.test',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: null,
    step_id: null,
    depends_on: null,
    cancel_requested: false,
    repository_url: null,
    model: null,
    timeout_seconds: 600,
    next_eligible_at: null,
    metadata: null,
    repository_ref: null,
    input: {},
    last_error: null,
    result_summary: null,
    latest_checkpoint: null,
    current_generation: 1,
    current_lease_id: 'lease_c388c25e50c5421f840d',
    ...over,
  }
}

/** Everything the component tells anyone: its text, and every label and title. */
function said(root: HTMLElement): string {
  const parts = [root.textContent ?? '']
  for (const el of Array.from(root.querySelectorAll('[aria-label], [title]'))) {
    parts.push(el.getAttribute('aria-label') ?? '', el.getAttribute('title') ?? '')
  }
  return parts.join('\n')
}

/** The value of one keyed fact in the confirmation, found by its key. */
function fact(root: HTMLElement, key: string): string {
  const row = Array.from(root.querySelectorAll('li.ctl-fact')).find(
    (li) => li.querySelector('b')?.textContent === key,
  )
  expect(row, `the confirmation has no "${key}" fact`).toBeTruthy()
  const whole = row?.textContent ?? ''
  return whole.slice(key.length).trim()
}

function openConfirmation(t: Task): HTMLElement {
  const { container } = render(<StopRun task={t} what="agent" reload={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: 'stop' }))
  const dialog = container.querySelector<HTMLElement>('.stop-confirm')
  expect(dialog, 'pressing stop did not open the confirmation').toBeTruthy()
  return dialog as HTMLElement
}

describe('a stop on an attempt no worker has started (started_at null)', () => {
  it('the confirmation promises no heartbeat, names the reconciler and claims no time', () => {
    const dialog = openConfirmation(task())

    expect(said(dialog)).not.toMatch(/heartbeat/i)
    expect(said(dialog)).toMatch(/reconciler/)

    const effect = fact(dialog, 'takes effect')
    expect(effect).toMatch(/reconciler/)
    expect(effect).not.toMatch(/\d|second|minute|hour/)
    expect(effect).toMatch(/not now/)
  })

  it('the confirmation does not claim a checkpoint of work nobody did', () => {
    const dialog = openConfirmation(task())

    expect(fact(dialog, 'work so far')).not.toMatch(/kept|checkpoint/)
    expect(dialog.querySelector('.stop-facts')?.getAttribute('aria-label') ?? '').not.toMatch(
      /work so far is kept|takes a checkpoint/,
    )
  })

  it('the note after the request is recorded does not say an agent is running', async () => {
    const ok: Result<CancelResult> = {
      status: 'ok',
      data: { released_immediately: false },
      fetchedAt: Date.now(),
    }
    api.cancelTask.mockResolvedValue(ok)
    const reload = vi.fn()
    const { container } = render(<StopRun task={task()} what="agent" reload={reload} />)

    fireEvent.click(screen.getByRole('button', { name: 'stop' }))
    fireEvent.click(screen.getByRole('button', { name: 'stop agent' }))

    const note = await screen.findByText('Stop requested.')
    expect(reload).toHaveBeenCalledTimes(1)
    expect(note).toBeTruthy()
    const all = said(container)
    expect(all).not.toMatch(/heartbeat/i)
    expect(all).not.toMatch(/still running|still spending/i)
    expect(all).toMatch(/reconciler/)
  })

  it('the "stop requested" pill does not promise a heartbeat either', () => {
    const { container } = render(
      <StopRun task={task({ cancel_requested: true })} what="agent" reload={vi.fn()} />,
    )
    const pill = container.querySelector('.stop-pending')
    expect(pill, 'a recorded stop on a live task renders no pill').toBeTruthy()
    expect(pill?.textContent).toBe('stop requested')
    const title = pill?.getAttribute('title') ?? ''
    expect(title).not.toMatch(/heartbeat/i)
    expect(title).toMatch(/reconciler/)
  })
})

describe('a retry waiting for its worker (DISPATCHED, started_at left by an earlier attempt)', () => {
  // `started_at` is the TASK's field. Its only writer is the DISPATCHED ->
  // STARTING transition (`control.py:412`), and nothing clears it on a retry,
  // so a second attempt waiting DISPATCHED still carries the first attempt's
  // value. Reading `started_at` alone would promise this attempt a heartbeat
  // from a worker that has not started either.
  it('is still an attempt no worker has started', () => {
    const dialog = openConfirmation(task({ started_at: THEN, attempt_count: 2 }))

    expect(said(dialog)).not.toMatch(/heartbeat/i)
    expect(fact(dialog, 'takes effect')).toMatch(/reconciler/)
  })
})

describe('a stop on an attempt whose worker has started', () => {
  // A GUARD: green before this change and after it. The heartbeat promise is
  // TRUE here, and deleting it everywhere is the easy wrong fix.
  it('still says the worker acts at its next heartbeat and keeps the work', () => {
    const dialog = openConfirmation(task({ state: 'RUNNING', started_at: THEN }))

    expect(fact(dialog, 'takes effect')).toMatch(/next heartbeat/)
    expect(fact(dialog, 'work so far')).toMatch(/kept/)
  })
})
