// #101: THE FULL TIMELINE LIVES ON ATTEMPTS, AND IT READS AS A SEQUENCE.
//
// Every event printed `timeAgo(e.at)` -- "3m ago", "3m ago", "3m ago" down a
// retried run -- and its whole detail as JSON. What a reader asks of a timeline
// is how long each step took, so each event now shows its GAP from the one
// before it (`+3m 09s`), the absolute time in its `title` and on focus, and the
// detail behind a closed `<details>`. The paging qualifiers stay, and the
// terminal-event check Details had moves here with the full list.
//
// MUTATION: print `timeAgo(e.at)` again; open the `<details>`; drop the
// endMissing mark from the toolbar.

import { describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { AttemptRow, Task, TaskEvent } from '../types'
import { at, attempt, ev, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadAttempts: vi.fn(),
  loadAgentDetail: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AttemptTimelineScreen, gapText } = await import('../AttemptTimeline')

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

const plus = (m: number, s: number) => new Date(Date.parse(at(m)) + s * 1000).toISOString()

async function mount(t: Task, attempts: AttemptRow[], events: TaskEvent[]): Promise<HTMLElement> {
  api.loadAttempts.mockResolvedValue(ok({ attempts }))
  api.loadAgentDetail.mockResolvedValue(ok({ task: t, events, eventsDetail: null }))
  const { container } = render(<AttemptTimelineScreen taskId={t.id} />)
  await waitFor(() => expect(container.querySelector('.ctl-card-foot')).not.toBeNull())
  return container as HTMLElement
}

describe('gapText', () => {
  it('pads the seconds under a minute and keeps a sub-second gap measured', () => {
    expect(gapText(189_000)).toBe('+3m 09s')
    expect(gapText(12_000)).toBe('+12s')
    expect(gapText(450)).toBe('+450ms')
    expect(gapText(0)).toBe('+0s')
    expect(gapText(2 * 3600_000 + 5 * 60_000)).toBe('+2h 05m')
  })
})

describe('the Attempts pane timeline', () => {
  const events = [
    ev('submitted', at(-1), null),
    ev('started', plus(-1, 189), 'att_1', { execution: 'exec-1' }),
    ev('heartbeat', plus(-1, 201), 'att_1', { source: 'reconciler' }),
  ]

  it('shows each event’s gap from the one before it, across attempt groups', async () => {
    const el = await mount(task({ state: 'RUNNING', started_at: at(1) }), [attempt(1, { completed_at: null })], events)
    const gaps = [...el.querySelectorAll('ol.timeline time.ev-at')].map((t) => t.textContent)
    expect(gaps[1]).toContain('+3m 09s')
    expect(gaps[2]).toContain('+12s')
  })

  it('puts the absolute time in the title and on focus', async () => {
    const el = await mount(task({ state: 'RUNNING', started_at: at(1) }), [attempt(1, { completed_at: null })], events)
    const t = el.querySelectorAll<HTMLElement>('ol.timeline time.ev-at')[1]!
    expect(t.getAttribute('title')).toBe('2026-09-22 10:02:09 UTC')
    expect(t.getAttribute('dateTime')).toBe(events[1]!.at)
    expect(t.tabIndex).toBe(0)
    expect(t.querySelector('.ev-abs')?.textContent).toBe('2026-09-22 10:02:09 UTC')
  })

  it('puts the detail JSON behind a closed <details>', async () => {
    const el = await mount(task({ state: 'RUNNING', started_at: at(1) }), [attempt(1, { completed_at: null })], events)
    const boxes = [...el.querySelectorAll<HTMLDetailsElement>('ol.timeline details.ev-more')]
    expect(boxes).toHaveLength(2)
    for (const b of boxes) {
      expect(b.open).toBe(false)
      expect(b.querySelector('summary')?.textContent).toBe('detail')
      expect(b.querySelector('pre.ev-detail')).not.toBeNull()
    }
    expect(el.querySelector('ol.timeline .ev-badge')?.textContent).toBe('reconciler')
  })

  it('keeps the proof that a finished task’s ending is off the page', async () => {
    const el = await mount(
      task({ state: 'SUCCEEDED', started_at: at(1), completed_at: at(10) }),
      [attempt(1)],
      events,
    )
    const marks = [...el.querySelectorAll('.ctl-toolbar .ctl-mark.is-partial')].filter((m) =>
      /terminal event/i.test(m.getAttribute('aria-label') ?? ''),
    )
    expect(marks).toHaveLength(1)
    expect(marks[0]!.getAttribute('aria-label')).toMatch(/does not follow the page token/)
  })
})
