/**
 * BROWSER QA U10b (owner, 2026-10-04; live console at 1440x900 and 390):
 * Overview.
 *
 *   D2   Recent failures was a 1532px table in a 1056px card: the row head
 *        inherited the head row's `nowrap`, so the whole error ran on one line
 *        and pushed the age and Open off the card. Fixed columns now, the error
 *        clamped to two lines with its whole text as the title, and the rows
 *        stacked on a phone; the card head wraps so "All recent" is never cut.
 *   D17  Headroom pool names were cut at 118px with no tooltip, and each bar
 *        ended where its figure began. Name and figure share a line, the bar
 *        takes the whole row under them, and the name is its own title.
 *   D18  Needs-a-look cards were 345px: the title was cut.
 *   D19  Running now and Waiting stretched to Headroom's height (~600px of
 *        blank); row separators changed colour at the row head.
 *   Links: every agent link names its own list, `/agents/<tab>/<id>`, never a
 *        legacy `#work/task/…` that a redirect sends to Live.
 *
 * MUTATIONS: put `nowrap` back on `.ov-tbl tbody th`, drop the error's title,
 * set `.ov-pl` back to three columns, `.ov-atts` back to `repeat(3, …)`,
 * `.ov-g21` back to `stretch`, or the href back to `#work/task/` -- each turns
 * a case red.
 */
import { render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import * as Overview from '../Overview'
import { RecentFailures, agentPath } from '../OverviewRegions'
import type { Result } from '../fetch'
import type { Pool, TaskPage } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { task } from './runfixture'
import { textPx } from './tablefit'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const LONG_ERROR =
  'The agent exited with code 1 after the provider refused the request: rate limited on every account in the pool, and the retry budget for this attempt was spent before any account came back.'

function page(tasks: ReturnType<typeof task>[]): Result<TaskPage> {
  return { status: 'ok', data: { tasks, next_cursor: null } as unknown as TaskPage, fetchedAt: Date.now(), serverAt: new Date().toISOString() }
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('D2: Recent failures fits its card', () => {
  const now = new Date().toISOString()
  const rows = [
    task({ id: 'tsk_failed_one', state: 'FAILED', completed_at: now, updated_at: now, last_error: LONG_ERROR }),
    task({ id: 'tsk_cancelled_one', state: 'CANCELLED', completed_at: now, updated_at: now }),
  ]

  it('lays the table out fixed, with fixed age and Open columns and a free name column', () => {
    const { container } = render(<RecentFailures tasks={page(rows)} />)
    const table = container.querySelector<HTMLTableElement>('table.ov-tbl')!
    expect(painted(table, 'table-layout', WIDE)).toBe('fixed')
    const cols = [...table.querySelectorAll('col')]
    expect(cols.map((c) => c.className)).toEqual(['ov-fc-mark', 'ov-fc-name', 'ov-fc-age', 'ov-fc-open'])
    // Every column but the name's is a fixed px width; their sum leaves the name most of a 390px phone row.
    const fixed = cols.filter((c) => c.className !== 'ov-fc-name').map((c) => painted(c, 'width', WIDE))
    for (const w of fixed) expect(w).toMatch(/^\d+px$/)
    expect(painted(cols[1]!, 'width', WIDE)).toBeNull()
    expect(fixed.reduce((a, w) => a + Number.parseInt(w!, 10), 0)).toBeLessThan(200)
  })

  it('wraps the row head and clamps the error to two lines, whole in its title', () => {
    const { container } = render(
      <div className="ov-page">
        <RecentFailures tasks={page(rows)} />
      </div>,
    )
    const th = container.querySelector('tbody th')!
    expect(painted(th, 'white-space', WIDE) ?? 'normal', 'the row head inherits the head row’s nowrap').not.toBe('nowrap')
    const sub = th.querySelector<HTMLElement>('.ov-sub')!
    expect(sub.getAttribute('title')).toBe(sub.textContent)
    expect(painted(sub, '-webkit-line-clamp', WIDE)).toBe('2')
    expect(painted(sub, 'overflow', WIDE)).toBe('hidden')
    // The name is names.css's two-line clamp, whole in its title.
    const name = th.querySelector<HTMLElement>('.ov-name')!
    expect(painted(name, 'display', WIDE)).toBe('-webkit-box')
    expect(name.getAttribute('title')).toContain('tsk_failed_one')
    // The age and Open never wrap and never leave their column.
    for (const td of container.querySelectorAll('tbody td.is-num')) {
      expect(painted(td, 'white-space', WIDE)).toBe('nowrap')
    }
  })

  it('stacks each row on a phone and lets the card head wrap', () => {
    const { container } = render(<RecentFailures tasks={page(rows)} />)
    const tr = container.querySelector('tbody tr')!
    expect(painted(tr, 'display', PHONE)).toBe('grid')
    expect(painted(container.querySelector('table')!, 'display', PHONE)).toBe('block')
    // The head: title, note, and "All recent", which a nowrap row cut at 390.
    document.body.innerHTML = '<section class="ctl-card ov-card"><div class="ctl-card-head"><h2 class="ctl-card-title">Recent failures</h2><span class="ctl-card-note">last 24h</span></div></section>'
    expect(painted(document.querySelector('.ov-card .ctl-card-head')!, 'flex-wrap', PHONE)).toBe('wrap')
  })

  it('links each agent straight to the list it sits in', () => {
    const { container } = render(<RecentFailures tasks={page(rows)} />)
    const hrefs = [...container.querySelectorAll('a.ov-name')].map((a) => a.getAttribute('href'))
    expect(hrefs).toEqual(['/agents/recent/tsk_failed_one', '/agents/recent/tsk_cancelled_one'])
    for (const a of container.querySelectorAll('a')) expect(a.getAttribute('href')).not.toMatch(/^#/)
  })
})

describe('agent links name the tab the agent is in', () => {
  it('maps live, waiting and finished states to their own list', () => {
    expect(agentPath(task({ id: 'a', state: 'RUNNING' }))).toBe('/agents/live/a')
    expect(agentPath(task({ id: 'b', state: 'LEASED' }))).toBe('/agents/live/b')
    expect(agentPath(task({ id: 'c', state: 'PARKED' }))).toBe('/agents/waiting/c')
    expect(agentPath(task({ id: 'd', state: 'QUEUED' }))).toBe('/agents/waiting/d')
    expect(agentPath(task({ id: 'e', state: 'CANCELLED' }))).toBe('/agents/recent/e')
    expect(agentPath(task({ id: 'f/g', state: 'FAILED' }))).toBe('/agents/recent/f%2Fg')
  })

  it('draws a running row with path links, never a legacy hash', () => {
    const { container } = render(
      <table>
        <tbody>
          <Overview.RunningRow task={task({ id: 'tsk_r', state: 'RUNNING', workflow_id: 'wf_one', step_id: 'build' })} />
        </tbody>
      </table>,
    )
    expect(container.querySelector('a.ov-name')!.getAttribute('href')).toBe('/agents/live/tsk_r')
    expect(container.querySelector('a.ov-wf')!.getAttribute('href')).toBe('/workflows/wf_one')
  })
})

describe('D17: a Headroom pool row shows its whole name, and every bar ends at one edge', () => {
  const pool = { name: 'provider:anthropic-subscription', active: 3, effective_limit: 10, enabled: true } as unknown as Pool
  it('puts the name and figure on a line and the bar under them, full width', () => {
    const { container } = render(<Overview.PoolRow pool={pool} />)
    const row = container.querySelector<HTMLElement>('.ov-pl')!
    const name = row.querySelector<HTMLElement>('.ov-idc')!
    expect(name.getAttribute('title')).toBe('provider:anthropic-subscription')
    expect(painted(row, 'grid-template-columns', WIDE)).toBe('minmax(0, 1fr) auto')
    const track = row.querySelector<HTMLElement>('.ctl-util-track')!
    expect(painted(track, 'grid-column', WIDE)).toBe('1 / -1')
    // A Headroom card's content box is ~330px at 1440; the whole name fits on its line.
    expect(textPx(name.textContent!, name, WIDE)).toBeLessThan(330 - 40)
  })
})

describe('D18 / D19: the cards are wide enough, and a row never stretches a short card', () => {
  it('lays Needs a look out at 400px or more per card', () => {
    document.body.innerHTML = '<section class="ov-lead"><div class="ov-atts"></div></section>'
    const v = painted(document.querySelector('.ov-atts')!, 'grid-template-columns', WIDE)!
    const min = /minmax\(min\(100%, (\d+)px\)/.exec(v)
    expect(min, v).not.toBeNull()
    expect(Number(min![1])).toBeGreaterThanOrEqual(400)
  })

  it('aligns each card of a two-card row to its top', () => {
    document.body.innerHTML = '<div class="ov-g21"><section class="ctl-card ov-card"></section></div>'
    expect(painted(document.querySelector('.ov-g21')!, 'align-items', WIDE)).toBe('start')
  })

  it('draws one separator colour across a row, row head included', () => {
    const { container } = render(
      <table className="ov-tbl">
        <tbody>
          <Overview.RunningRow task={task({ id: 'tsk_r', state: 'RUNNING' })} />
        </tbody>
      </table>,
    )
    const th = container.querySelector('tbody th')!
    const td = container.querySelector('tbody td')!
    expect(painted(th, ['border-bottom'], WIDE)).toBe(painted(td, ['border-bottom'], WIDE))
  })
})
