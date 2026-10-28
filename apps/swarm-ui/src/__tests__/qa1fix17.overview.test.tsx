/**
 * QA1-FIX-17 (visual QA pass 1, part of #2596): Overview's regions.
 *
 *   overview-agents-01  Needs a look said "2 failed agents among the 27 most
 *        recent" while Recent failures, over the same 27, said "None among the
 *        27 newest": the check counts every failure on a page that is every
 *        agent, at any age, and the card windowed to the last 24 hours. The
 *        card takes the check's window (`failureWindow`), and its note names
 *        it ("of the 27 newest"), so both count the same rows. With more
 *        behind the page and a FAILED state read, both stay on 24 hours.
 *
 *   overview-agents-07  Needs a look's first row drew the two-line "2 leases
 *        past TTL" card as tall as the card beside it: the grid stretched it.
 *        The cards size to their content (`align-items: start`).
 *
 * Fixtures from `runfixture`; nothing here is token-shaped.
 */
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import type { Result } from '../fetch'
import { RecentFailures, failureWindow, failuresOf, type OverviewTaskPage } from '../OverviewRegions'
import type { Task } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { task } from './runfixture'

const MIN = 60_000
const ago = (m: number): string => new Date(Date.now() - m * MIN).toISOString()
const text = (n: Element | null | undefined): string => (n?.textContent ?? '').replace(/\s+/g, ' ').trim()

function ok(data: OverviewTaskPage): Result<OverviewTaskPage> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: new Date().toISOString() } as Result<OverviewTaskPage>
}

afterEach(cleanup)

const TWO_DAYS = 2 * 24 * 60

function rowsOf(): Task[] {
  return [
    task({ id: 'tsk_ok_1', state: 'SUCCEEDED', completed_at: ago(30), updated_at: ago(30) }),
    task({ id: 'tsk_fail_a', state: 'FAILED', completed_at: ago(TWO_DAYS), updated_at: ago(TWO_DAYS), last_error: 'exit status 1' }),
    task({ id: 'tsk_fail_b', state: 'FAILED', completed_at: ago(TWO_DAYS + 10), updated_at: ago(TWO_DAYS + 10), last_error: 'exit status 2' }),
    task({ id: 'tsk_run_1', state: 'RUNNING', completed_at: null, updated_at: ago(1) }),
  ]
}

describe('overview-agents-01: Recent failures counts the failures Needs a look counts', () => {
  it('lists every failure on a page that is every agent, at any age, and names that window', () => {
    const rows = rowsOf()
    const data = { tasks: rows, next_page_token: null } as unknown as OverviewTaskPage
    expect(failureWindow(data)).toEqual({ newest: 4 })
    const { container } = render(<RecentFailures tasks={ok(data)} />)
    const links = [...container.querySelectorAll<HTMLAnchorElement>('tbody th a.ov-name')].map((a) => a.getAttribute('href')!.split('/').pop())
    // MUTATION: keep the 24-hour window on a whole page -> "None among the 4 newest", no rows.
    expect(links).toEqual(['tsk_fail_a', 'tsk_fail_b'])
    expect(text(container)).not.toMatch(/None among/)
    // The head's note names the same window, not "last 24h".
    // MUTATION: leave the note's first clause at 'last 24h' -> fails.
    expect(failuresOf(rows, Date.now(), undefined, failureWindow(data)).note).toBe('of the 4 newest · 2 failed · 0 dead-lettered')
  })

  it('keeps the last 24 hours when the page has more behind it and the FAILED state read landed', () => {
    const rows = rowsOf()
    const data = {
      tasks: rows,
      next_page_token: 'more',
      byState: [
        { state: 'FAILED', tasks: rows.filter((t) => t.state === 'FAILED'), complete: true },
        { state: 'DEAD_LETTERED', tasks: [], complete: true },
        { state: 'CANCELLED', tasks: [], complete: true },
      ],
    } as unknown as OverviewTaskPage
    // MUTATION: return the page window always -> the old failures are listed.
    expect(failureWindow(data)).toEqual({ newest: null })
    const { container } = render(<RecentFailures tasks={ok(data)} />)
    expect(container.querySelectorAll('tbody th a.ov-name')).toHaveLength(0)
    expect(failuresOf(rows, Date.now(), undefined, failureWindow(data)).note).toMatch(/^last 24h · 0 failed/)
  })
})

describe('overview-agents-07: a Needs a look card is as tall as its own content', () => {
  it('does not stretch a short card to its neighbour, at any width', () => {
    document.body.innerHTML = '<section class="ov-lead"><div class="ov-atts"></div></section>'
    const grid = document.querySelector('.ov-atts')!
    for (const env of [{ width: 1440 }, { width: 390 }] as CascadeEnv[]) {
      // MUTATION: drop `align-items: start` from `.ov-atts` -> the default stretch is back.
      expect(painted(grid, 'align-items', env)).toBe('start')
    }
  })
})
