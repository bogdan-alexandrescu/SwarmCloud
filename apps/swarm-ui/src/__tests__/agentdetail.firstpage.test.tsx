// AGENT DETAILS › PROGRESS: THE OLDEST ROW AND THE PAGING NOTE (part of #3015).
//
// Each event row shows the gap since the event before it on the page. The
// oldest row on the page has no event before it, so it showed no time at all;
// it now shows its own age, as `EventWhen` does on the Attempts tab. The note
// under the list says it is `the first page of events`, so a short list is not
// read as the whole history. Unit tests, no credentials.

import { render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AgentRun } from '../api'
import type { Task } from '../types'
import { attempt, ev, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadTaskInputOnce: vi.fn(),
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { Run, ageText } = await import('../AgentDetail')
const { clearTaskPrefill } = await import('../submitPrefill')

const CLASSES = { standard: { name: 'standard', cpu: 4, memory_gib: 8, disk_gib: 4, units: 1 } }

beforeEach(() => {
  api.loadTaskInputOnce.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
})

afterEach(() => clearTaskPrefill())

/** A RUNNING task with `n` events, one a minute, the newest a minute ago. */
function running(n: number): AgentRun {
  const minAgo = (m: number) => new Date(Date.now() - m * 60_000).toISOString()
  const started = minAgo(n + 1)
  const t: Task = task({ id: 'tsk_running0', state: 'RUNNING', started_at: started, completed_at: null, current_lease_id: 'lse_1' })
  const events = Array.from({ length: n }, (_, i) => ev(i === 0 ? 'lease_acquired' : 'heartbeat', minAgo(n - i), 'att_1'))
  return {
    task: t,
    events,
    eventsDetail: null,
    attempts: [attempt(1, { created_at: started, started_at: started, completed_at: null, exit_code: null })],
    attemptsDetail: null,
    classes: CLASSES,
    classesDetail: null,
    classesRouteMissing: false,
  } as AgentRun
}

async function rows(n: number): Promise<{ first: HTMLElement[]; all: HTMLElement[]; card: HTMLElement }> {
  const { container } = render(<Run run={running(n)} />)
  const card = await waitFor(() => {
    const c = container.querySelector<HTMLElement>('.dt-progress')
    expect(c?.querySelector('.dt-evs')).toBeTruthy()
    return c!
  })
  const first = [...card.querySelectorAll<HTMLElement>('ol.dt-evs:not(.dt-evs-all) > li')]
  const all = [...card.querySelectorAll<HTMLElement>('ol.dt-evs-all > li')]
  return { first, all, card }
}

const when = (li: HTMLElement) => li.querySelector<HTMLElement>('[data-when]')

describe('Progress: the oldest event on the page shows its age', () => {
  it('with 3 events, the oldest row shows an age and the other two a gap', async () => {
    const { first, all } = await rows(3)
    expect(first).toHaveLength(3)
    expect(all).toHaveLength(0)
    // MUTATION: draw nothing when there is no predecessor -- the oldest row has no time.
    expect(when(first[2]!)?.dataset.when).toBe('age')
    expect(when(first[2]!)?.textContent).toMatch(/ago/)
    for (const li of first.slice(0, 2)) {
      expect(when(li)?.dataset.when).toBe('gap')
      expect(when(li)?.textContent?.trim()).toMatch(/^\+/)
    }
  })

  it('with 7 events, the last row in `All 7 events` shows the age', async () => {
    const { first, all } = await rows(7)
    expect(first).toHaveLength(5)
    expect(all).toHaveLength(2)
    // The fifth row has a predecessor in the disclosure, so it keeps its gap.
    for (const li of first) expect(when(li)?.dataset.when).toBe('gap')
    expect(when(all[0]!)?.dataset.when).toBe('gap')
    expect(when(all[1]!)?.dataset.when).toBe('age')
    expect(when(all[1]!)?.textContent).toMatch(/ago/)
  })

  it('`ageText` is the age `timeAgo` writes', () => {
    const now = Date.parse('2026-11-12T12:00:00Z')
    expect(ageText('2026-11-12T11:55:00Z', now)).toBe('5m ago')
  })
})

describe('Progress: the paging note says it is the first page of events', () => {
  it('says `Newest first · the first page of events`', async () => {
    const { card } = await rows(3)
    const note = [...card.querySelectorAll('.dt-note')].find((n) => (n.textContent ?? '').includes('the first page of events'))
    // MUTATION: put `first page only` back.
    expect(note, 'the note does not say it is the first page of events').toBeTruthy()
    expect(note!.textContent).toBe('Newest first · the first page of events')
    expect(note!.getAttribute('aria-label')).toMatch(/^One page, as served\./)
  })
})
