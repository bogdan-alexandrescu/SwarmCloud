// RUNNING NOW'S COST SO FAR IS THE TASK'S TOTAL (owner decision 2026-10-05,
// P1 follow-up).
//
// `GET /v1/tasks` -- the Overview's `view=summary` poll among its callers --
// now serves on every row the totals P1 put on the task page: `cost_usd_total`
// over every attempt, `cost_incomplete`, `attempts` and the last attempt's
// `last_attempt_cost_usd`, summed by the API from one batched attempts read.
// So the column that was a dash "because no task route serves it" shows the
// total, with the last attempt's figure beside it once there is more than one,
// and `at least` when an attempt has not reported (a running attempt reports
// at exit). A row from an API that serves no total keeps its dash and its
// reason; an attempt read that failed says so, never `$0`.
//
// MUTATIONS: keep the dash for a served total; drop `at least`; show the last
// attempt's figure as the headline; draw a failed read or a null total as $0.

import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render } from '@testing-library/react'

import type { Task } from '../types'
import { task } from './runfixture'

const { RunningRow } = await import('../Overview')

afterEach(cleanup)

const text = (n: Element | null | undefined): string => (n?.textContent ?? '').replace(/\s+/g, ' ').trim()

function costCell(t: Task): HTMLElement {
  const { container } = render(
    <table>
      <tbody>
        <RunningRow task={t} />
      </tbody>
    </table>,
  )
  return container.querySelector<HTMLElement>('tr td:last-child')!
}

const RUNNING = { state: 'RUNNING', attempt_count: 2, started_at: new Date(Date.now() - 60_000).toISOString() } as const

describe('Running now: Cost so far reads the served total', () => {
  it('shows the total over every attempt, the last attempt beside it', () => {
    const cell = costCell(
      task({ ...RUNNING, attempts: 2, cost_usd_total: 9.64, cost_incomplete: false, last_attempt_cost_usd: 1.51, attempts_read: 'ok' }),
    )
    expect(text(cell.querySelector('.ov-cost'))).toBe('$9.64')
    expect(text(cell.querySelector('.ov-sub'))).toBe('last attempt $1.51')
    expect(cell.querySelector('[title]')?.getAttribute('title') ?? '').toMatch(/Summed by the API over 2 attempts/)
  })

  it('says at least while an attempt has not reported its cost', () => {
    const cell = costCell(
      task({ ...RUNNING, attempts: 2, cost_usd_total: 8.13, cost_incomplete: true, last_attempt_cost_usd: null, attempts_read: 'ok' }),
    )
    expect(text(cell.querySelector('.ov-cost'))).toBe('at least $8.13')
    expect(text(cell.querySelector('.ov-sub'))).toBe('last attempt not reported')
  })

  it('reads a single attempt without a last-attempt line', () => {
    const cell = costCell(
      task({ ...RUNNING, attempt_count: 1, attempts: 1, cost_usd_total: 0.5, cost_incomplete: false, last_attempt_cost_usd: 0.5, attempts_read: 'ok' }),
    )
    expect(text(cell)).toBe('$0.50')
    expect(cell.querySelector('.ov-sub')).toBeNull()
  })

  it('draws a dash, not $0, when no attempt has reported yet', () => {
    const cell = costCell(
      task({ ...RUNNING, attempt_count: 1, attempts: 1, cost_usd_total: null, cost_incomplete: true, last_attempt_cost_usd: null, attempts_read: 'ok' }),
    )
    expect(text(cell)).toBe('—')
    expect(cell.querySelector('[title]')?.getAttribute('title') ?? '').toMatch(/not reported yet/)
  })

  it('draws a dash naming the failed read, never a cost', () => {
    const cell = costCell(
      task({ ...RUNNING, attempts: null, cost_usd_total: null, cost_incomplete: null, attempts_read: 'failed' }),
    )
    expect(text(cell)).toBe('—')
    expect(cell.querySelector('[title]')?.getAttribute('title') ?? '').toMatch(/could not be read/)
  })

  it('keeps the dash and its reason for an API that serves no total', () => {
    const cell = costCell(task({ ...RUNNING }))
    expect(text(cell)).toBe('—')
    expect(cell.querySelector('[title]')?.getAttribute('title') ?? '').toMatch(/not served/)
  })
})
