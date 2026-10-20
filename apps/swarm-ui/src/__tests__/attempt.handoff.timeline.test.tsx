// LB-V6 (part of #1030): HAND-OFF AND CONTROL EVENTS SIT UNDER THE ATTEMPT
// THEY HAPPENED IN.
//
// Visual QA pass 5 (2026-10-12, task_8be4d2c60e834faca479) measured
// `human handoff requested`, `human handoff resolved`, `ready`, `human control
// taken` and `human control returned` all filed under "Before any attempt",
// though they happened during attempts 1 and 2. The writers put the attempt in
// `detail` and left the event's own `attempt_id` null, and `grouped()` read
// only the top-level field.
//
// The writers carry `attempt_id` at the top level now (handoff.py, live.py);
// the events already stored do not, so the screen places them: the event's own
// attempt, else the attempt its detail names, else its generation, else the
// attempt it happened in by time.
//
// MUTATION: make `attemptOf` in AttemptTimeline.tsx return `e.attempt_id`
// alone. Every test but the first-attempt preface control fails.

import { describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { AttemptRow, TaskEvent } from '../types'
import { at, attempt, ev, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadAttempts: vi.fn(),
  loadAgentDetail: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AttemptTimelineScreen } = await import('../AttemptTimeline')

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

async function mount(attempts: AttemptRow[], events: TaskEvent[]): Promise<HTMLElement> {
  api.loadAttempts.mockResolvedValue(ok({ attempts }))
  api.loadAgentDetail.mockResolvedValue(
    ok({ task: task({ state: 'SUCCEEDED', started_at: at(1) }), events, eventsDetail: null }),
  )
  const { container } = render(<AttemptTimelineScreen taskId="tsk_charts" />)
  await waitFor(() => expect(container.querySelector('.ctl-card-foot')).not.toBeNull())
  return container as HTMLElement
}

/** Each card's events as stored types, keyed by its attempt id (`@preface` for the first). */
function cards(el: HTMLElement): Record<string, string[]> {
  const out: Record<string, string[]> = {}
  for (const card of Array.from(el.querySelectorAll<HTMLElement>('.att-card'))) {
    const fact = Array.from(card.querySelectorAll<HTMLElement>('.ctl-fact')).find(
      (li) => li.querySelector('b')?.textContent === 'attempt',
    )
    const key = fact?.querySelector('.mono')?.textContent ?? '@preface'
    out[key] = Array.from(card.querySelectorAll<HTMLElement>('.ev-type')).map((s) => s.dataset.kind ?? '')
  }
  return out
}

/** Two attempts: 1 runs 0-10 and parks for a person, 2 is leased at 20 and runs 21-40. */
const attempts = [
  attempt(2, { created_at: at(20), started_at: at(21), completed_at: at(40) }),
  attempt(1, { created_at: at(0), started_at: at(1), completed_at: at(10), exit_code: 75, error: 'HUMAN_REQUIRED' }),
]

describe('hand-off and control events on the Attempts tab', () => {
  it('files an event its writer attributed under that attempt (the new writers)', async () => {
    const requested = ev('human_handoff_requested', at(5), 'att_1')
    const el = await mount(attempts, [ev('submitted', at(-1), null), requested])
    expect(cards(el)).toMatchObject({ '@preface': ['submitted'], att_1: ['human_handoff_requested'] })
  })

  it('reads the attempt an old event names in its detail, wherever its time falls', async () => {
    // Stored before the writers carried the field: the attempt rode `detail`.
    // Its time is inside attempt 2's window; the detail is the better witness.
    const returned = ev('human_control_returned', at(25), null, { attempt_id: 'att_1', generation: 1 })
    const el = await mount(attempts, [ev('submitted', at(-1), null), returned])
    const got = cards(el)
    expect(got['@preface'], 'an attempt event filed before any attempt').toEqual(['submitted'])
    expect(got['att_1']).toEqual(['human_control_returned'])
    expect(got['att_2']).toEqual([])
  })

  it('reads the generation an event was written under when it names no attempt', async () => {
    const taken = { ...ev('human_control_taken', at(5), null), generation: 2 }
    const el = await mount(attempts, [ev('submitted', at(-1), null), taken])
    expect(cards(el)).toMatchObject({ '@preface': ['submitted'], att_2: ['human_control_taken'] })
  })

  it('places an event with no attempt at all by when it happened', async () => {
    const events = [
      ev('submitted', at(-1), null),
      ev('human_handoff_requested', at(5), null),
      // After attempt 1 ended and before attempt 2 was leased: the person's
      // `ready` answers attempt 1's park, so it sits under attempt 1.
      ev('ready', at(15), null, { reason: 'human_ready', park_reason: 'HUMAN_REQUIRED' }),
      ev('human_control_taken', at(30), null),
      ev('human_handoff_resolved', at(31), null),
    ]
    const el = await mount(attempts, events)
    const got = cards(el)
    expect(got['@preface'], 'the QA pass measured all four here').toEqual(['submitted'])
    expect(got['att_1']).toEqual(['human_handoff_requested', 'ready'])
    expect(got['att_2']).toEqual(['human_control_taken', 'human_handoff_resolved'])
  })

  it('says which events it placed by time, and only those', async () => {
    const events = [
      ev('submitted', at(-1), null),
      ev('started', at(1), 'att_1'),
      ev('human_handoff_requested', at(5), null),
    ]
    const el = await mount(attempts, events)
    const placed = Array.from(el.querySelectorAll<HTMLElement>('.ev-placed')).map(
      (s) => s.closest('li')?.querySelector<HTMLElement>('.ev-type')?.dataset.kind,
    )
    expect(placed).toEqual(['human_handoff_requested'])
  })

  it('keeps an event from before the first attempt before any attempt', async () => {
    // The control: the time fallback must not pull pre-admission events in.
    const el = await mount(attempts, [ev('submitted', at(-1), null), ev('admitted', plusSec(-1, 30), null)])
    expect(cards(el)['@preface']).toEqual(['submitted', 'admitted'])
  })
})

function plusSec(m: number, s: number): string {
  return new Date(Date.parse(at(m)) + s * 1000).toISOString()
}
