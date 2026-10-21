/**
 * VISUAL QA RUN 2, LANE QAFIX2-OVERVIEW (part of #1680; run
 * wf_b6e2441b419f423a84eb, 2026-10-20, the fixture build of main at 75148351).
 *
 *   overview-automate-010  At 1440 a Needs a look card cut its detail at
 *         "Newest: exit…" while the same card at 390 showed the whole
 *         failure. The desktop gives the detail the phone's four lines.
 *   FF-D3 Recent failures put a cancelled agent first: someone looking for
 *         the newest failure read the wrong row. Failed and dead-lettered
 *         rows come first, newest-ended first; cancelled ones follow them.
 *
 * Each assertion names the mutation that turns it red.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render } from '@testing-library/react'

import { CheckCard } from '../Overview'
import { RecentFailures, failuresOf } from '../OverviewRegions'
import type { Problem } from '../checks'
import type { Result } from '../fetch'
import type { TaskPage } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { task } from './runfixture'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const MIN = 60_000
const ago = (m: number): string => new Date(Date.now() - m * MIN).toISOString()

afterEach(() => {
  cleanup()
  document.body.innerHTML = ''
})

describe('overview-automate-010: at 1440 a check card’s detail is not cut before the failure', () => {
  const p: Problem = {
    severity: 'bad',
    n: 2,
    headline: '2 failed agents among the 26 most recent · newest 4m ago · wf_ccdd1422ab',
    detail: 'Terminal: nothing will retry them. 2 used every attempt. Newest: exit status 1: pytest collected 2 failures',
    href: '#work/tasks',
  }

  it('gives the detail the phone’s four lines on a desktop, beside an ids line or not', () => {
    const { container } = render(<CheckCard problem={p} />)
    const ids = container.querySelector('.ov-att-t small.ov-att-ids')!
    const detail = container.querySelector('.ov-att-t small.is-one')!
    expect(ids, 'the headline carries ids, so the card draws its ids line').not.toBeNull()
    expect(detail.textContent).toContain('Newest: exit status 1: pytest collected 2 failures')
    // At 1440 the detail read "… Newest: exit…".
    // MUTATION: put `.ov-att-t small.is-one { -webkit-line-clamp: 1 }` back in the desktop sheet.
    expect(painted(detail, '-webkit-line-clamp', WIDE)).toBe('4')
    // MUTATION: put the desktop `.ov-att-t small` clamp back to 2.
    const plain = render(<CheckCard problem={{ ...p, headline: '2 failed agents' }} />).container.querySelector('.ov-att-t small')!
    expect(plain.classList.contains('is-one')).toBe(false)
    expect(painted(plain, '-webkit-line-clamp', WIDE)).toBe('4')
    // The two widths agree, and the ids stay one cut line at both.
    // MUTATION: give the phone block a clamp of its own for the detail again.
    expect(painted(detail, '-webkit-line-clamp', PHONE)).toBe(painted(detail, '-webkit-line-clamp', WIDE))
    // MUTATION: drop the ids line's one-line clamp.
    for (const env of [WIDE, PHONE]) expect(painted(ids, '-webkit-line-clamp', env)).toBe('1')
    // MUTATION: drop the detail's `title`.
    expect(detail.getAttribute('title')).toBe(p.detail)
  })
})

describe('FF-D3: Recent failures leads with a failure, not a cancelled agent', () => {
  const rows = [
    task({ id: 'tsk_cancel_new', state: 'CANCELLED', completed_at: ago(1), updated_at: ago(1), last_error: null }),
    task({ id: 'tsk_failed_old', state: 'FAILED', completed_at: ago(30), updated_at: ago(30), last_error: 'exit status 1' }),
    task({ id: 'tsk_cancel_mid', state: 'CANCELLED', completed_at: ago(5), updated_at: ago(5), last_error: null }),
    task({ id: 'tsk_dead_mid', state: 'DEAD_LETTERED', completed_at: ago(10), updated_at: ago(10), last_error: 'retry budget' }),
    task({ id: 'tsk_failed_undated', state: 'FAILED', completed_at: null, updated_at: ago(2), last_error: 'boom' }),
  ]

  it('orders failed and dead-lettered newest-ended first, then the cancelled, then the undated', () => {
    const found = failuresOf(rows, Date.now())
    // MUTATION: sort the dated rows by end time alone -- tsk_cancel_new leads again.
    expect(found.rows.map((t) => t.id)).toEqual(['tsk_dead_mid', 'tsk_failed_old', 'tsk_cancel_new', 'tsk_cancel_mid', 'tsk_failed_undated'])
    // The head still counts the cancelled.
    // MUTATION: drop the cancelled clause from the note.
    expect(found.note).toContain('2 cancelled')
  })

  it('draws the newest failure as the card’s first row', () => {
    const page: Result<TaskPage> = {
      status: 'ok',
      data: { tasks: rows, next_cursor: null } as unknown as TaskPage,
      fetchedAt: Date.now(),
      serverAt: new Date().toISOString(),
    } as Result<TaskPage>
    const { container } = render(<RecentFailures tasks={page} />)
    const links = [...container.querySelectorAll<HTMLAnchorElement>('tbody th a.ov-name')]
    // MUTATION: list `failuresOf`'s dated rows by end time alone.
    expect(links[0]!.getAttribute('href')).toMatch(/tsk_dead_mid$/)
    // The cancelled agents are still listed, after the failures.
    // MUTATION: filter CANCELLED out of the card's rows.
    expect(links.map((a) => a.getAttribute('href')!.split('/').pop())).toContain('tsk_cancel_new')
  })
})
