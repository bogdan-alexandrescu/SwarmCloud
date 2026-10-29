// LANE QA2-FIX-13 (part of #2694, the console visual QA pass 2 epic): the
// Attempts tab's findings, read on the fixture console's failed workflow step
// `task_fan_3` and the agents beside it.
//
// WHAT EACH CASE HOLDS:
//   * ovag2-07: an attempt that ended on a bare error code (`BACKEND_REJECTED`)
//     says it in words; the code stays in the title, for a search;
//   * overview-agents-41: the page's event count is said once -- on the
//     toolbar, beside its `first page` -- not again in the head's summary nor
//     on the one card that holds every event on the page;
//   * overview-agents-43: an attempt with no model recorded shows the model
//     the agent asked for, labelled `requested`, rather than `not recorded`
//     beside an Inputs pane that names it.

import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AgentDetail } from '../api'
import type { Result } from '../fetch'
import type { AttemptRow, Task, TaskEvent } from '../types'
import { at, attempt, ev, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadAttempts: vi.fn(),
  loadAgentDetail: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AttemptTimelineScreen } = await import('../AttemptTimeline')

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })
const flat = (el: Element | null | undefined) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

async function mount(t: Task, attempts: AttemptRow[], events: TaskEvent[]) {
  api.loadAttempts.mockResolvedValue(ok({ attempts }))
  api.loadAgentDetail.mockResolvedValue(ok({ task: t, events, eventsDetail: null } as unknown as AgentDetail))
  render(<AttemptTimelineScreen taskId={t.id} />)
  await waitFor(() => expect(document.querySelector('.att-card')).not.toBeNull())
}

function fact(card: Element, key: string): HTMLElement {
  const f = [...card.querySelectorAll<HTMLElement>('.ctl-fact')].find((li) => li.querySelector('b')?.textContent === key)
  expect(f, `no "${key}" fact`).toBeTruthy()
  return f!
}

afterEach(() => {
  vi.clearAllMocks()
})

describe('ovag2-07: an attempt that ended on a bare error code', () => {
  it('says what happened in words, and keeps the code in its title', async () => {
    const t = runTask({ state: 'FAILED', model: 'claude-opus-5', started_at: at(1), completed_at: at(10) })
    const rejected = attempt(1, { started_at: null, completed_at: at(1), exit_code: null, execution_name: null, error: 'BACKEND_REJECTED' })
    await mount(t, [rejected], [ev('failed', at(1), 'att_1')])
    const card = document.querySelector('.att-card')!
    // MUTATION: print `a.error` as it came -> the card ends in `BACKEND_REJECTED`.
    expect(flat(card)).toContain('The backend refused to start it.')
    expect(flat(card)).not.toContain('BACKEND_REJECTED')
    expect(card.querySelector('[title="BACKEND_REJECTED"]')).not.toBeNull()
  })

  it('still prints a free-text error whole, in the error box (control)', async () => {
    const t = runTask({ state: 'FAILED', started_at: at(1), completed_at: at(10) })
    await mount(t, [attempt(1, { exit_code: 2, error: 'exit status 2: pytest was interrupted' })], [ev('failed', at(10), 'att_1')])
    expect(flat(document.querySelector('.att-card pre.err'))).toBe('exit status 2: pytest was interrupted')
  })
})

describe('overview-agents-41: the event count is said once', () => {
  it('prints `9 events` on the toolbar only, not in the summary nor on the card holding all nine', async () => {
    const t = runTask({ state: 'SUCCEEDED', started_at: at(1), completed_at: at(10) })
    const events = Array.from({ length: 9 }, (_, i) => ev(i === 8 ? 'succeeded' : 'heartbeat', at(1 + i), 'att_1'))
    await mount(t, [attempt(1)], events)
    await waitFor(() => expect(flat(document.querySelector('.att-ev-cap'))).toBe('9 events · first page'))
    // MUTATION: put the event count back in the head's summary -> `1 attempt · 9 events`.
    // MUTATION: draw `pluralise(g.events.length, 'event')` on a card holding the whole page.
    expect(document.body.textContent!.match(/9 events/g)).toHaveLength(1)
    expect(screen.getByText('1 attempt')).toBeTruthy()
    expect(flat(document.querySelector('.att-card .ctl-card-note'))).toBe('Cloud Run')
  })

  it('keeps each card’s own count when the page’s events are split between attempts (control)', async () => {
    const t = runTask({ state: 'SUCCEEDED', started_at: at(1), completed_at: at(20) })
    const events = [ev('failed', at(5), 'att_1'), ev('running', at(11), 'att_2'), ev('succeeded', at(19), 'att_2')]
    await mount(t, [attempt(1, { exit_code: 1 }), attempt(2, { created_at: at(10), started_at: at(11), completed_at: at(19) })], events)
    await waitFor(() => expect(document.querySelectorAll('.att-card')).toHaveLength(2))
    const notes = [...document.querySelectorAll('.att-card .ctl-card-note')].map((n) => flat(n))
    expect(notes).toEqual(['2 events · Cloud Run', '1 event · Cloud Run'])
  })
})

describe('overview-agents-43: an attempt with no model recorded', () => {
  it('shows the model the agent asked for, labelled `requested`', async () => {
    const t = runTask({ state: 'SUCCEEDED', model: 'claude-opus-5', started_at: at(1), completed_at: at(10) })
    await mount(t, [attempt(1, { model: null })], [ev('succeeded', at(10), 'att_1')])
    const model = fact(document.querySelector('.att-card')!, 'model')
    // MUTATION: draw `AttemptModel` whatever the task asked for -> `not recorded`.
    expect(flat(model)).toMatch(/^model\s*claude-opus-5\s*requested/)
    expect(flat(model)).not.toContain('not recorded')
    expect(model.querySelector('[title]')?.getAttribute('title')).toMatch(/asked for/)
  })

  it('shows the recorded model when the attempt wrote one, with no `requested` (control)', async () => {
    const t = runTask({ state: 'SUCCEEDED', model: 'claude-opus-5', started_at: at(1), completed_at: at(10) })
    await mount(t, [attempt(1, { model: 'claude-opus-5' })], [ev('succeeded', at(10), 'att_1')])
    const model = fact(document.querySelector('.att-card')!, 'model')
    expect(flat(model)).toMatch(/^model\s*claude-opus-5/)
    expect(flat(model)).not.toContain('requested')
  })

  it('says `not recorded` when the agent asked for no model either (control)', async () => {
    const t = runTask({ state: 'SUCCEEDED', model: null, started_at: at(1), completed_at: at(10) })
    await mount(t, [attempt(1, { model: null })], [ev('succeeded', at(10), 'att_1')])
    expect(flat(fact(document.querySelector('.att-card')!, 'model'))).toContain('not recorded')
  })
})
