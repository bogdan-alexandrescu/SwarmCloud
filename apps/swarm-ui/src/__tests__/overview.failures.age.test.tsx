/**
 * RECENT FAILURES SAY WHEN, AND WAITING, AND WHY ENDS WHERE ITS ROWS DO (#503).
 *
 * The 2026-10-02 audit measured Recent failures rows with no age and no
 * "Open" link, where O1's rows end "25m ago · Open", and "Waiting, and why"
 * stretched to Headroom's height with ~550px of it empty.
 *
 *   * Each row ends in its relative age, inside a `<time>` whose `dateTime`
 *     is the recorded end and whose title is that instant in full -- the age
 *     is what a reader scans for, the instant is what they match to a log.
 *   * Each row's title is its one link, to the agent under the tab its
 *     state sits in (`agentPath`); the row-end Open went by the owner's
 *     decision c03 (2026-10-22), so the row ends in its age.
 *   * The Waiting card has no height of its own at any width, and nothing
 *     around it stretches it: its column and the grid align to the top.
 *
 * MUTATIONS: print the age bare again (no `<time>` or no title); point the
 * title at `#work/task/<id>` or put Open back; put `align-items: stretch` back on
 * `.ov-cols > .ov-col`, or give `.ov-card` a `min-height`.
 */
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import STYLES from '../styles.css?raw'
import OVERVIEW_CSS from '../styles/overview.css?raw'
import type { Result } from '../fetch'
import { RecentFailures, agentPath } from '../OverviewRegions'
import type { TaskPage } from '../types'
import { cascade } from './cssgate'
import { task } from './runfixture'

const SHEET = `${STYLES}\n${OVERVIEW_CSS}`
const won = (el: Element, prop: string, width: number) => cascade(SHEET, el, prop, { width }).winner?.value ?? null

const MIN = 60_000
const ago = (m: number): string => new Date(Date.now() - m * MIN).toISOString()
const text = (n: Element | null | undefined): string => (n?.textContent ?? '').replace(/\s+/g, ' ').trim()

function page(tasks: TaskPage['tasks']): Result<TaskPage> {
  return { status: 'ok', data: { tasks, tenant_id: 'eng', next_page_token: null }, fetchedAt: Date.now() }
}

afterEach(() => {
  cleanup()
  document.body.innerHTML = ''
})

describe('Recent failures rows', () => {
  it('end in a relative age with the absolute end time in its tooltip', () => {
    const ended = ago(25)
    const { container } = render(
      <RecentFailures tasks={page([task({ id: 'tsk_fail_age', state: 'FAILED', updated_at: ago(2), completed_at: ended })])} />,
    )
    const row = container.querySelector('tbody tr')!
    const at = row.querySelector('td.is-num time')!
    expect(at, 'the age is not a <time>').not.toBeNull()
    expect(text(at)).toBe('25m ago')
    expect(at.getAttribute('dateTime')).toBe(ended)
    // The full instant, not the age again and not the last write.
    expect(at.getAttribute('title')).toContain(new Date(ended).toISOString())
    expect(at.getAttribute('title')).toMatch(/^ended /)
  })

  // The row-end Open went by the owner's decision c03, 2026-10-22: the
  // title is the only link, and the row ends in its age.
  it('link to the agent by the title alone, and end in the age', () => {
    const t = task({ id: 'tsk_fail_open', state: 'DEAD_LETTERED', workflow_id: 'wf_docs', step_id: 'review', completed_at: ago(58) })
    const { container } = render(<RecentFailures tasks={page([t])} />)
    const row = container.querySelector('tbody tr')!
    // MUTATION: put the row-end `.ov-open` link back.
    expect(row.querySelector('a.ov-open')).toBeNull()
    expect(row.querySelectorAll('a').length).toBe(1)
    const name = row.querySelector<HTMLAnchorElement>('a.ov-name')!
    // MUTATION: point the title at `#work/task/<id>`, or anywhere but `agentPath`.
    expect(name.getAttribute('href')).toBe(agentPath(t))
    expect(name.getAttribute('href')).toBe('/agents/recent/tsk_fail_open')
    // MUTATION: put any cell after the age.
    expect(row.lastElementChild!.querySelector('time')).not.toBeNull()
  })

  it('say a missing end is missing, never a time read off the last write', () => {
    const { container } = render(
      <RecentFailures tasks={page([task({ id: 'tsk_fail_undated', state: 'CANCELLED', updated_at: ago(1), completed_at: null })])} />,
    )
    const cell = container.querySelector('tbody tr td.is-num')!
    expect(cell.querySelector('time')).toBeNull()
    expect(text(cell)).not.toMatch(/ago/)
  })
})

describe('the Waiting, and why card', () => {
  function grid(): HTMLElement {
    document.body.innerHTML =
      '<div class="ov-g21 ov-cols">' +
      '<div class="ov-col"><section class="ctl-card ov-card ov-running"></section><section class="ctl-card ov-card ov-waiting"></section></div>' +
      '<div class="ov-col"><section class="ctl-card ov-card ov-spend"></section><section class="ctl-card ov-card ov-headroom"></section></div>' +
      '<section class="ctl-card ov-card ov-failures"></section></div>'
    return document.querySelector<HTMLElement>('.ov-waiting')!
  }

  it('is sized to its content at every width: no height, no stretch', () => {
    const waiting = grid()
    for (const width of [390, 900, 1440]) {
      for (const prop of ['height', 'min-height', 'flex-grow', 'align-self']) {
        expect(won(waiting, prop, width), `${prop} at ${width}`).toBeNull()
      }
    }
    // Beside Headroom from 1100px: the column and the grid align to the top,
    // so a short card is not stretched to the tall one beside it.
    const col = waiting.parentElement!
    expect(won(col, 'align-items', 1440)).toBe('start')
    expect(won(col, 'align-content', 1440)).toBe('start')
    expect(won(col.parentElement!, 'align-items', 1440)).toBe('start')
  })
})
