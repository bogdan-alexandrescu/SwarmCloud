// A HELD MERGE WAIT'S "NEXT CHECK" (the owner, 2026-11-10): while the
// scheduler holds a merge step's CI wait on its wake tick's fresh reading,
// the API serves no `next_check_at` and the tick's last check as
// `detail.held_last_check_at`. The panel puts that time behind an (i), never
// as visible prose, and never says "not recorded" for it; any other wait
// keeps its next-check time.
//
// MUTATIONS, each named on the assertion it breaks.

import { describe, expect, it } from 'vitest'
import { fireEvent, render } from '@testing-library/react'

import type { Waiting } from '../types'
import { clockTime } from '../types'
import { WaitPanel } from '../WaitPanel'
import { at, task } from './runfixture'

const NOW = Date.parse(at(120))

function waiting(over: Partial<Waiting>): Waiting {
  return {
    reason: 'CI_PENDING',
    summary: 'CI on PR #41: 1 checks pending (ci-gate); read 10:59Z',
    since: at(60),
    next_check_at: null,
    resumes_when: 'its pull request checks settle',
    detail: { pending: ['ci-gate'], failed: [], last_checked_at: at(119), held_last_check_at: at(119) },
    history: [],
    history_read: true,
    read_at: at(120),
    ...over,
  }
}

function nextCheck(w: Waiting): HTMLElement {
  const t = task({ id: 'task_held', state: 'PARKED', park_reason: 'CI_PENDING', next_eligible_at: at(95), waiting: w })
  const { container } = render(<WaitPanel task={t} now={NOW} />)
  const row = container.querySelector<HTMLElement>('[data-fact="next-check"] dd')
  expect(row, 'no next-check row').not.toBeNull()
  return row!
}

describe('a held merge wait', () => {
  it('shows its last check behind an (i), with no visible time and no "not recorded"', () => {
    const dd = nextCheck(waiting({}))
    const tip = dd.querySelector<HTMLElement>('[role="tooltip"]')
    // MUTATION: draw next_check_at (null) as "not recorded", or the time as text.
    expect(tip, 'no (i)').not.toBeNull()
    expect(tip!.textContent).toContain(clockTime(at(119), NOW)!.title)
    // MUTATION: showing the tip open, or printing the time beside the (i).
    expect(tip!.hidden).toBe(true)
    expect(dd.textContent!.replace(tip!.textContent!, '').trim()).toBe('')
    expect(dd.textContent).not.toContain('not recorded')
    fireEvent.mouseEnter(dd.querySelector('.info-tip')!)
    expect(tip!.hidden).toBe(false)
  })

  it('an ordinary CI wait keeps its next-check time', () => {
    const dd = nextCheck(waiting({ next_check_at: at(130), detail: { pending: ['ci-gate'], held_last_check_at: null } }))
    // MUTATION: hiding every CI wait's next check.
    expect(dd.textContent).toBe(clockTime(at(130), NOW)!.text)
    expect(dd.querySelector('[role="tooltip"]')).toBeNull()
  })
})
