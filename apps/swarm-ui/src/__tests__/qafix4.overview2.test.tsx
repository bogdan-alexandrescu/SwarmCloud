/**
 * OVERVIEW'S RECENT FAILURES: THE FAILURES ALONE, THE CANCELLED ONE TAP AWAY,
 * AND THE TITLE AS THE ROW'S ONLY LINK (lane QAFIX4-OVERVIEW-2, part of #1680).
 *
 *   c02  "Fold the cancelled under '2 cancelled ▸'" (owner, 2026-10-22, box
 *        comment 6298220142; run 2 FF-D3, run 4 flow-find-failure-desktop-D1).
 *        The table lists FAILED and DEAD_LETTERED alone, newest first; under
 *        it a disclosure built like 'N more' reads 'N cancelled' and opens the
 *        cancelled rows in the same row design, newest first.
 *   c03  "The title is the only link" (owner, 2026-10-22, box comment
 *        6314121113; run 3 ff-003, run 4 flow-find-failure-desktop-D2 and the
 *        'Open' half of flow-find-failure-phone-390-P2). The row-end 'Open' and
 *        its column are gone; the row ends in its age, and the title takes a
 *        44px touch target on a phone.
 *
 * Each assertion names the one change that turns it red.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render } from '@testing-library/react'

import type { Result } from '../fetch'
import type { TaskPage } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { task } from './runfixture'
import { RecentFailures, agentPath } from '../OverviewRegions'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const MIN = 60_000
const ago = (m: number): string => new Date(Date.now() - m * MIN).toISOString()
const text = (n: Element | null | undefined): string => (n?.textContent ?? '').replace(/\s+/g, ' ').trim()
const page = (tasks: ReturnType<typeof task>[]): Result<TaskPage> =>
  ({ status: 'ok', data: { tasks, next_page_token: null, tenant_id: 'eng' } as unknown as TaskPage, fetchedAt: Date.now() }) as Result<TaskPage>
const ids = (table: Element | null): string[] =>
  [...(table?.querySelectorAll('tbody th a.ov-name') ?? [])].map((a) => a.getAttribute('href')!.split('/').pop()!)

afterEach(cleanup)

const MIXED = [
  task({ id: 'tsk_cancel_new', state: 'CANCELLED', completed_at: ago(1), updated_at: ago(1) }),
  task({ id: 'tsk_failed_old', state: 'FAILED', completed_at: ago(30), updated_at: ago(30), last_error: 'exit status 1' }),
  task({ id: 'tsk_cancel_old', state: 'CANCELLED', completed_at: ago(40), updated_at: ago(40) }),
  task({ id: 'tsk_dead_mid', state: 'DEAD_LETTERED', completed_at: ago(10), updated_at: ago(10) }),
  task({ id: 'tsk_failed_new', state: 'FAILED', completed_at: ago(2), updated_at: ago(2), last_error: 'boom' }),
]

describe('c02: the failures alone, the cancelled under "N cancelled"', () => {
  it('lists only the failures, newest first, and folds the cancelled, newest first', () => {
    const { container } = render(<RecentFailures tasks={page(MIXED)} />)
    const tables = [...container.querySelectorAll('table.ov-fails')]
    // MUTATION: list the cancelled in the first table again (one mixed list).
    expect(ids(tables[0]!)).toEqual(['tsk_failed_new', 'tsk_dead_mid', 'tsk_failed_old'])
    const fold = container.querySelector<HTMLDetailsElement>('details.ov-more.ov-failures-cancelled')
    // MUTATION: drop the cancelled disclosure.
    expect(fold, 'no "N cancelled" disclosure').not.toBeNull()
    // MUTATION: render the disclosure `open`.
    expect(fold!.open).toBe(false)
    // MUTATION: count the failures (or every row) in the summary.
    expect(text(fold!.querySelector('summary'))).toBe('2 cancelled')
    // MUTATION: sort the cancelled oldest first.
    expect(ids(fold!.querySelector('table.ov-fails'))).toEqual(['tsk_cancel_new', 'tsk_cancel_old'])
    // A disclosure's table says what it holds (a11y-keyboard-003).
    // MUTATION: name the cancelled table "Recent failures, the rest".
    expect(fold!.querySelector('table')!.getAttribute('aria-label')).toBe('Recent failures, cancelled')
    // MUTATION: name the first table anything but the card's title.
    expect(tables[0]!.getAttribute('aria-label')).toBe('Recent failures')
  })

  it('keeps the failures’ own "N more" past six rows, ahead of the cancelled', () => {
    const failed = Array.from({ length: 8 }, (_, i) => task({ id: `tsk_f${i}`, state: 'FAILED', completed_at: ago(i + 1), updated_at: ago(i + 1) }))
    const cancelled = task({ id: 'tsk_c', state: 'CANCELLED', completed_at: ago(0.5), updated_at: ago(0.5) })
    const { container } = render(<RecentFailures tasks={page([cancelled, ...failed])} />)
    const more = container.querySelector('details.ov-failures-more')!
    // MUTATION: count the cancelled into the failures' "N more".
    expect(text(more.querySelector('summary'))).toBe('2 more')
    expect(ids(more.querySelector('table'))).toEqual(['tsk_f6', 'tsk_f7'])
    const folds = [...container.querySelectorAll('details.ov-more')]
    // MUTATION: draw the cancelled disclosure before the failures' "N more".
    expect(folds.map((d) => d.className)).toEqual(['ov-more ov-failures-more', 'ov-more ov-failures-cancelled'])
    expect(text(folds[1]!.querySelector('summary'))).toBe('1 cancelled')
  })

  it('still says a step was cancelled because its parent failed', () => {
    const parent = task({ id: 'tsk_checkpoints', state: 'FAILED', step_id: 'checkpoints', workflow_id: 'wf_n23', completed_at: ago(5), updated_at: ago(5) })
    const child = task({ id: 'tsk_review', state: 'CANCELLED', step_id: 'review', workflow_id: 'wf_n23', depends_on: ['tsk_checkpoints'], completed_at: ago(4), updated_at: ago(4) })
    const { container } = render(<RecentFailures tasks={page([child, parent])} />)
    const row = container.querySelector('details.ov-failures-cancelled tbody tr')!
    // MUTATION: drop `taskById` from the cancelled table's `whyAgent` call.
    expect(text(row.querySelector('.ov-why'))).toBe('Cancelled: checkpoints failed.')
  })

  it('says there are no failures and still offers the cancelled', () => {
    const only = [task({ id: 'tsk_c1', state: 'CANCELLED', completed_at: ago(3), updated_at: ago(3) })]
    const { container } = render(<RecentFailures tasks={page(only)} />)
    // MUTATION: draw the cancelled row in the main table when nothing failed.
    for (const t of container.querySelectorAll('table.ov-fails')) expect(t.closest('details')).not.toBeNull()
    // MUTATION: drop the no-failures words when a cancelled row is read.
    expect(text(container)).toMatch(/no failed or dead-lettered agent/i)
    const fold = container.querySelector('details.ov-failures-cancelled')!
    // MUTATION: return the empty state alone, without the disclosure.
    expect(fold, 'no "N cancelled" under the empty state').not.toBeNull()
    expect(text(fold.querySelector('summary'))).toBe('1 cancelled')
    expect(ids(fold.querySelector('table'))).toEqual(['tsk_c1'])
  })

  it('keeps today’s empty state with neither', () => {
    const { container } = render(<RecentFailures tasks={page([task({ id: 'tsk_ok', state: 'SUCCEEDED', completed_at: ago(1) })])} />)
    // MUTATION: draw an empty "0 cancelled" disclosure.
    expect(container.querySelector('details')).toBeNull()
    // MUTATION: change the real-zero heading.
    expect(text(container)).toMatch(/None among the 1 newest/)
  })
})

describe('c03: the title is the row’s only link', () => {
  it('draws no Open link or column, and one link per row, its title, to the agent', () => {
    const { container } = render(<RecentFailures tasks={page(MIXED)} />)
    // MUTATION: put the row-end `.ov-open` link back.
    expect(container.querySelector('.ov-open')).toBeNull()
    // MUTATION: put `<col className="ov-fc-open">` back.
    expect(container.querySelector('col.ov-fc-open')).toBeNull()
    const rows = [...container.querySelectorAll('table.ov-fails tbody tr')]
    expect(rows.length).toBe(5)
    for (const r of rows) {
      const links = [...r.querySelectorAll('a')]
      // MUTATION: add any second link to the row.
      expect(links.length).toBe(1)
      expect(links[0]!.classList.contains('ov-name')).toBe(true)
      const id = r.querySelector('a.ov-name')!.getAttribute('title')!.split(' · ').pop()!
      expect(links[0]!.getAttribute('href')).toBe(agentPath(MIXED.find((t) => t.id === id)!))
      // MUTATION: end the row in anything but its age.
      expect(r.lastElementChild!.matches('td.is-num')).toBe(true)
      expect(r.lastElementChild!.querySelector('time')).not.toBeNull()
    }
    // MUTATION: drop the title's underline.
    expect(painted(rows[0]!.querySelector('.ov-name')!, 'text-decoration-line', WIDE)).toBe('underline')
  })

  it('gives the title a touch target at least 44px tall on a phone', () => {
    const { container } = render(
      <div className="ov-page">
        <RecentFailures tasks={page(MIXED)} />
      </div>,
    )
    const name = container.querySelector('.ov-fails .ov-name')!
    const px = (v: string | null): number => (v !== null && /^\d+(\.\d+)?px$/.test(v) ? Number.parseFloat(v) : 0)
    // MUTATION: drop `min-height: 44px` from `.ov-fails .ov-name` in the 560px block.
    expect(Math.max(px(painted(name, 'min-height', PHONE)), px(painted(name, 'block-size', PHONE)))).toBeGreaterThanOrEqual(44)
  })

  it('lets the name take the freed width: only the mark and age columns are fixed', () => {
    const { container } = render(<RecentFailures tasks={page(MIXED)} />)
    const cols = [...container.querySelector('table.ov-fails')!.querySelectorAll('col')]
    // MUTATION: keep a fourth (Open) column.
    expect(cols.map((c) => c.className)).toEqual(['ov-fc-mark', 'ov-fc-name', 'ov-fc-age'])
    // MUTATION: give the name column a fixed width.
    expect(painted(cols[1]!, 'width', WIDE)).toBeNull()
    // On a phone the row head and the age share the row: the age sits under
    // the name, and nothing is laid out for a third cell.
    const tr = container.querySelector('table.ov-fails tbody tr')!
    const age = tr.lastElementChild!
    // MUTATION: leave `.ov-fails td.is-num:last-child { grid-column: 3 }`, which pushes the age into a narrow right cell.
    expect(painted(age, 'grid-column', PHONE)).toBe('2')
  })
})
