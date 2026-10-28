// LANE QA1-FIX-08 (console visual QA pass 1, 2026-10-28; part of #2596): the
// Attempts tab's two findings.
//
//   overview-agents-41  `8 events` was printed three times on one pane: the
//                       head's `1 attempt · 8 events`, the toolbar's `8 events
//                       · first page`, and the one card's `8 events · Cloud
//                       Run`. The count is said once: by the card when one
//                       card holds every event, by the toolbar when the page
//                       is shared out between cards, never in the head.
//   overview-agents-42  an open attempt read `ran 9d ago · open` and `heartbeat
//                       9d ago` beside a head saying 8d 21h: `timeAgo` rounds
//                       213 h to 9 days, `spanText` floors it to `8d 21h`.

import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AgentDetail } from '../api'
import type { Result } from '../fetch'
import { at, attempt, ev, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({ loadAttempts: vi.fn(), loadAgentDetail: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api, USE_FIXTURES: false }))

const { AttemptTimelineScreen } = await import('../AttemptTimeline')

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })
const flat = (el: Element | null | undefined) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

afterEach(() => {
  vi.clearAllMocks()
  document.body.innerHTML = ''
})

/** Every text node on the pane that says `<n> event(s)`, counted. */
function eventCounts(): number {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
  let n = 0
  for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
    n += (node.textContent ?? '').match(/\b\d+ events?\b/g)?.length ?? 0
  }
  return n
}

describe('overview-agents-41: the event count is said once', () => {
  it('one attempt holding every event: the card says `2 events`, nothing else does', async () => {
    api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1)] }))
    api.loadAgentDetail.mockResolvedValue(
      ok({ task: runTask({ state: 'SUCCEEDED' }), events: [ev('running', at(1), 'att_1'), ev('succeeded', at(10), 'att_1')] } as unknown as AgentDetail),
    )
    render(<AttemptTimelineScreen taskId="tsk_charts" />)
    await waitFor(() => expect(document.querySelector('.att-card')).not.toBeNull())
    // MUTATION: put `pluralise(t.events.length, 'event')` back in the summary -- two.
    // MUTATION: keep the toolbar's count when one card holds the page -- two.
    expect(eventCounts()).toBe(1)
    expect(flat(document.querySelector('.att-card .ctl-card-note'))).toBe('2 events · Cloud Run')
    // The head counts the attempts, once.
    expect(screen.getByText('1 attempt')).toBeTruthy()
    // The toolbar keeps the paging caveat, and its accessible name the figure.
    const cap = document.querySelector('.att-ev-cap')!
    expect(flat(cap)).toBe('first page')
    expect(cap.getAttribute('aria-label')).toMatch(/^2 events, the first page/)
  })

  it('a page shared between cards keeps its one total on the toolbar (control)', async () => {
    api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1)] }))
    api.loadAgentDetail.mockResolvedValue(
      ok({
        task: runTask({ state: 'SUCCEEDED' }),
        events: [ev('submitted', at(-1), null), ev('running', at(1), 'att_1'), ev('succeeded', at(10), 'att_1')],
      } as unknown as AgentDetail),
    )
    render(<AttemptTimelineScreen taskId="tsk_charts" />)
    await waitFor(() => expect(document.querySelector('.att-ev-cap')).not.toBeNull())
    expect(flat(document.querySelector('.att-ev-cap'))).toBe('3 events, 1 before any attempt · first page')
    expect(screen.getByText('1 attempt')).toBeTruthy()
    expect(screen.queryByText(/1 attempt · /)).toBeNull()
  })
})

describe('overview-agents-42: one instant, one formatter', () => {
  it('an open attempt 8d 21h old reads `8d 21h · open` and `heartbeat 8d 21h ago`, never `9d`', async () => {
    const started = new Date(Date.now() - ((8 * 24 + 21) * 3600 + 600) * 1000).toISOString()
    const open = attempt(1, { started_at: started, completed_at: null, exit_code: null })
    api.loadAttempts.mockResolvedValue(ok({ attempts: [open] }))
    api.loadAgentDetail.mockResolvedValue(
      ok({
        task: runTask({ state: 'RUNNING', started_at: started, completed_at: null }),
        events: [ev('running', started, 'att_1'), ev('heartbeat', started, 'att_1', { peak_rss_bytes: 1024 ** 3 })],
      } as unknown as AgentDetail),
    )
    render(<AttemptTimelineScreen taskId="tsk_charts" />)
    await waitFor(() => expect(document.querySelector('.att-card .ctl-facts')).not.toBeNull())
    const facts = [...document.querySelectorAll('.att-card .ctl-fact')].map(flat)
    // MUTATION: `${timeAgo(a.started_at)} · open` in `ran()` -- `ran9d ago · open`.
    expect(facts).toContain('ran8d 21h · open')
    // MUTATION: `heartbeat ${timeAgo(reading.at)}` -- `heartbeat 9d ago`.
    expect(facts.find((f) => f.startsWith('peak memory'))).toMatch(/heartbeat 8d 21h ago$/)
    expect(facts.join(' | ')).not.toMatch(/9d/)
  })
})
