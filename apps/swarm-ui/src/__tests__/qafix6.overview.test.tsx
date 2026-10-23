/**
 * CONSOLE VISUAL QA RUN 6, LANE QAFIX6-OVERVIEW (part of #1680; run
 * wf_31aca00c977e4d37a935, the fixture build of main at d3cdbd62, 1440 and
 * 390 px, light and dark).
 *
 *   b. 6:overview-automate-012: at 390 Running now's rows had no separator,
 *      each title about 4px under the previous row's state line. The phone
 *      rows are ruled apart, as Recent failures' phone rows are.
 *   c. 6:overview-automate-013: Needs a look said a pool was paused while
 *      the Pools grid drew it as a plain empty bar. A paused pool's row
 *      carries Capacity › Pools' `paused` mark.
 *   d. 6:overview-automate-015: a parked row said 'next eligible 11:51 UTC'
 *      under a 12:00 clock. A time that has passed reads 'eligible since'.
 *   e. box 6343766999 (5b:wg-dark-D1): the failed-agents card's workflow is
 *      its first way out and drawn as the prominent one; 'Agents · FAILED →'
 *      steps down to the secondary ink.
 *   f. box 6343791868 (5b:ff-desktop-02): a workflow step's failure row
 *      carries its short task id, as a lone agent's name already does.
 *
 * LANE QAFIX6-OVERVIEW-2 (part of #1680), on BGD's decisions of 2026-10-23:
 *
 *   a. 6:overview-automate-011 (box 6343776209): at 390 the Pools list
 *      dropped every bar, a full pool's hatch with them. The phone draws
 *      them as 1440 does. This reverses BGD's Q3 of 2026-10-11 ("the bar
 *      stays hidden on a phone"); Q3's toned figure stays.
 *   e. box 6343766999: a click on the failed-agents card's empty space opens
 *      the failed workflow. The first named workflow's link carries the
 *      card's cover; 'Agents · FAILED →' is a plain link above it.
 *   f. box 6343791868: the step's short task id is inside the row's link,
 *      `<title> · <step> · <id>`, not on a line of its own.
 *
 * Each assertion names the mutation that turns it red.
 */
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import * as Overview from '../Overview'
import { RecentFailures, waitGroups } from '../OverviewRegions'
import type { Problem } from '../checks'
import type { Result } from '../fetch'
import type { Pool, TaskPage } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { task } from './runfixture'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const MIN = 60_000

const text = (el: Element | null | undefined): string => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

const hosts: HTMLElement[] = []
afterEach(() => {
  cleanup()
  for (const h of hosts.splice(0)) h.remove()
  document.body.innerHTML = ''
})

/** Markup parsed into a host the cascade can read. */
function fragment(html: string): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML = html
  document.body.appendChild(host)
  hosts.push(host)
  return host
}

function page(tasks: ReturnType<typeof task>[]): Result<TaskPage> {
  return { status: 'ok', data: { tasks, next_cursor: null } as unknown as TaskPage, fetchedAt: Date.now(), serverAt: new Date().toISOString() }
}

function pool(name: string, active: number, limit: number | null, enabled = true): Pool {
  return { name, hard_limit: limit, effective_limit: limit, active, enabled } as unknown as Pool
}

describe('a: the 390 Pools list draws its bars (6:overview-automate-011, box 6343776209)', () => {
  it('draws the full, lowered and paused pools’ tracks under the phone query', () => {
    const { container } = render(
      <div className="ov-pools">
        <Overview.PoolRow pool={pool('provider:anthropic:tenant:u-bogdan', 5, 5)} />
        <Overview.PoolRow pool={{ ...pool('global', 3, 8), hard_limit: 40 } as Pool} />
        <Overview.PoolRow pool={pool('tenant:eng', 0, 10, false)} />
      </div>,
    )
    const tracks = [...container.querySelectorAll('.ov-pl')].map((r) => r.querySelector('.ctl-util-track'))
    // MUTATION: drop `.ov-pools .ov-pl > .ctl-util-track` from overview.css's 560px block.
    expect(tracks.map((t) => (t === null ? 'no track' : painted(t, 'display', PHONE)))).toEqual(['flex', 'flex', 'flex'])
  })
})

describe('b: Running now’s phone rows are ruled apart (6:overview-automate-012)', () => {
  it('draws a rule between rows under the phone query, and none under the last', () => {
    const host = fragment('<ul class="ov-prun"><li>one</li><li>two</li></ul>')
    const [first, last] = [...host.querySelectorAll('li')]
    // MUTATION: drop the `.ov-prun li` separator from overview.css's 560px block.
    expect(painted(first!, ['border-bottom', 'border-bottom-width'], PHONE)).toBe('1px solid var(--line-soft)')
    expect(painted(last!, ['border-bottom', 'border-bottom-width'], PHONE)).toBe('0')
  })
})

describe('c: a paused pool is marked in the Pools grid (6:overview-automate-013)', () => {
  it('carries the paused mark on the paused pool’s row and on no other', () => {
    const { container } = render(
      <div className="ov-pools">
        <Overview.PoolRow pool={pool('tenant:eng', 0, 10, false)} />
        <Overview.PoolRow pool={pool('global', 3, 10)} />
      </div>,
    )
    const rows = [...container.querySelectorAll('.ov-pl')]
    const marked = rows.map((r) => [...r.querySelectorAll('[data-tone="paused"]')].map(text))
    // MUTATION: draw the paused pool's row as any other, with no `paused` mark.
    expect(marked).toEqual([['paused'], []])
  })
})

describe('d: an eligibility time that has passed reads as past (6:overview-automate-015)', () => {
  it('says "eligible since" for a parked group whose time is before now, "next eligible" for one still ahead', () => {
    const now = Date.parse('2026-10-19T12:00:00Z')
    const past = waitGroups([task({ id: 'tsk_p1', state: 'PARKED', park_reason: 'QUOTA_EXHAUSTED', next_eligible_at: '2026-10-19T11:51:00Z' })], now)
    const ahead = waitGroups([task({ id: 'tsk_p2', state: 'PARKED', park_reason: 'QUOTA_EXHAUSTED', next_eligible_at: '2026-10-19T12:30:00Z' })], now)
    // MUTATION: say 'next eligible' whatever the clock reads.
    expect(past[0]!.detail).toContain('eligible since 11:51 UTC')
    expect(ahead[0]!.detail).toContain('next eligible 12:30 UTC')
  })
})

describe('e: the failed-agents card leads to the failed workflow (box 6343766999)', () => {
  const WF = 'wf_5e5ad3b6f7da4299a839'
  const p: Problem = {
    severity: 'bad',
    n: 2,
    headline: `2 failed agents among the 26 most recent · newest 4m ago · from ${WF}`,
    detail: 'Terminal: nothing will retry them. Newest: exit status 1',
    href: '#work/running/recent/failed',
    linkLabel: 'Agents · FAILED',
    workflows: [{ id: WF, href: `#work/workflows?wf=${WF}` }],
  }

  it('opens the workflow first and draws it as the prominent link, the list as the secondary one', () => {
    const { container } = render(<Overview.CheckCard problem={p} />)
    const links = [...container.querySelectorAll<HTMLAnchorElement>('.ov-att-acts a')]
    expect(links[0]!.getAttribute('href')).toBe(Overview.toPath(p.workflows![0]!.href))
    const list = links.find((a) => text(a).startsWith('Agents · FAILED'))!
    // MUTATION: draw the workflow link at the list link's weight, or the list link in the accent.
    expect([painted(links[0]!, 'font-weight', WIDE), painted(list, 'font-weight', WIDE), painted(list, 'color', WIDE)]).toEqual([
      '600',
      '400',
      'var(--text-dim)',
    ])
  })
})

describe('e: a click on the card’s empty space opens the failed workflow (box 6343766999)', () => {
  const WF = 'wf_5e5ad3b6f7da4299a839'
  const WF2 = 'wf_0b1c2d3e4f5a6b7c8d9e'
  const p: Problem = {
    severity: 'bad',
    n: 3,
    headline: `3 failed agents among the 26 most recent · newest 4m ago · 2 in workflow ${WF}, 1 in ${WF2}`,
    detail: 'Terminal: nothing will retry them. Newest: exit status 1',
    href: '#work/running/recent/failed',
    linkLabel: 'Agents · FAILED',
    workflows: [
      { id: WF, href: `#work/workflows?wf=${WF}` },
      { id: WF2, href: `#work/workflows?wf=${WF2}` },
    ],
  }
  /** A link whose `::after` is drawn absolutely over the card: what a click on its empty space lands on. */
  const covers = (a: Element): boolean =>
    painted(a, 'content', WIDE, 'after') !== null && painted(a, 'content', WIDE, 'after') !== 'none' && painted(a, 'position', WIDE, 'after') === 'absolute'

  it('anchors the card’s one cover on the first workflow’s link, every other link above it', () => {
    const { container } = render(<Overview.CheckCard problem={p} />)
    const card = container.querySelector('.ov-att.is-split')!
    // The cover is positioned against the card.
    expect(painted(card, 'position', WIDE)).toBe('relative')
    const links = [...card.querySelectorAll('a')]
    // MUTATION: leave the cover on 'Agents · FAILED →', or give it to every workflow link.
    expect(links.filter(covers).map((a) => a.getAttribute('href'))).toEqual([Overview.toPath(p.workflows![0]!.href)])
    // MUTATION: draw any other link under the cover (no `position: relative; z-index: 1`).
    for (const a of links.filter((x) => !covers(x))) {
      expect([a.getAttribute('href'), painted(a, 'position', WIDE), painted(a, 'z-index', WIDE)]).toEqual([a.getAttribute('href'), 'relative', '1'])
    }
    // 'Agents · FAILED →' is still on the card, as a plain link to the list.
    const list = links.find((a) => text(a).startsWith('Agents · FAILED'))!
    expect(list.getAttribute('href')).toBe(Overview.toPath(p.href))
  })
})

describe('f: every failure row carries a task id (box 6343791868)', () => {
  it('names a workflow step’s row by its short task id, as a lone agent’s name already is', () => {
    const ended = new Date(Date.now() - 4 * MIN).toISOString()
    const rows = [
      task({ id: 'tsk_8e33a3de11', state: 'FAILED', completed_at: ended, updated_at: ended }),
      task({ id: 'tsk_c4f9b2a0aa', state: 'FAILED', completed_at: ended, updated_at: ended, workflow_id: 'wf_one', step_id: 'checkpoints' }),
    ]
    const { container } = render(<RecentFailures tasks={page(rows)} />)
    const heads = [...container.querySelectorAll('#ov-failures tbody th, table.ov-fails tbody th')].map(text)
    // MUTATION: drop the `.ov-tid` line from a step's row.
    expect(heads.map((h) => /8e33a3de|c4f9b2a0/.exec(h)?.[0] ?? `no id in "${h}"`).sort()).toEqual(['8e33a3de', 'c4f9b2a0'])
  })

  it('ends a workflow step’s row link in its short task id, with no separate id line', () => {
    const ended = new Date(Date.now() - 4 * MIN).toISOString()
    const rows = [task({ id: 'tsk_c4f9b2a0aa', state: 'FAILED', completed_at: ended, updated_at: ended, workflow_id: 'wf_one', step_id: 'checkpoints' })]
    const { container } = render(<RecentFailures tasks={page(rows)} />)
    const link = container.querySelector('table.ov-fails tbody th a.ov-name')
    // An untitled step names its workflow (OV-16), then its id, inside the link.
    // MUTATION: draw the link as `rowLabel` alone and the id outside it.
    expect(text(link)).toBe('checkpoints · wf_one · c4f9b2a0')
    // MUTATION: keep the separate 'task <id>' line under the name.
    expect(container.querySelectorAll('table.ov-fails .ov-tid')).toHaveLength(0)
  })
})
