// VISUAL QA PASS 2, cap2-07 (lane QA2-FIX-10, part of #2694): the window
// cells of an aged row (`overflow`, `retired-laptop` in the fixture console)
// were drawn at `opacity: 0.6`, which took their text to 2.72:1 in light and
// 3.66:1 in dark. The age is now said by a tag beside the name, in the
// account tags' existing look, and the cells keep their full contrast.
//
// BREAK IT: put `opacity: 0.6` back on `.acct-rank-row.is-stale .acct-fig >
// .acct-win` or `.acct-card.is-stale .acct-card-w2` -- the cascade case
// fails; drop the `is-aged` tag from `Tags` -- the render cases fail.

import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import type { PoolAccount } from '../accountsPool'
import { AccountsTable } from '../AccountsTable'
import { build, painted } from './marks'

const H = 3_600_000
const D = 24 * H
const NOW = Date.parse('2026-10-29T09:04:00Z')
const iso = (t: number) => new Date(t).toISOString()

function acct(label: string, over: Partial<PoolAccount> = {}): PoolAccount {
  return {
    account_id: `u-bogdan:${label}`,
    owner_tenant: 'u-bogdan',
    label,
    provider: 'anthropic',
    state: 'AVAILABLE',
    reason: '',
    lend_to: [],
    assigned: 0,
    windows: {
      five_hour: { utilization: 0.4, resets_at: iso(NOW + 2 * H), reset: false },
      seven_day: { utilization: 0.5, resets_at: iso(NOW + 3 * D), reset: false },
    },
    observed_at: iso(NOW - 5 * 60_000),
    stale: false,
    unreadable_by: [],
    unreadable_now: [],
    last_assigned_at: iso(NOW - H),
    max_agents: 6,
    can_take_new_agent: true,
    blocked_by: [],
    next_eligible_at: null,
    organisation: null,
    ...over,
  }
}

const aged = () => acct('overflow', { stale: true, observed_at: iso(NOW - 52 * 60_000) })

const hosts: HTMLElement[] = []
afterEach(() => {
  cleanup()
  for (const h of hosts.splice(0)) h.remove()
})

describe('an aged row keeps its text at full contrast (cap2-07)', () => {
  it('draws no opacity on an aged row’s window cells, in the table or on a card', () => {
    // MUTATION: `.acct-rank-row.is-stale .acct-fig > .acct-win { opacity: 0.6 }` -> fails.
    const cell = build('tr.acct-rank-row.is-stale > td.acct-fig > div.acct-win', hosts)
    expect(painted(cell, 'opacity', { width: 1440 })).toBeNull()
    // MUTATION: `.acct-card.is-stale .acct-card-w2 { opacity: 0.6 }` -> fails.
    const card = build('article.acct-card.is-stale > div.acct-card-w2', hosts)
    expect(painted(card, 'opacity', { width: 390 })).toBeNull()
  })

  it('marks the aged row with a tag beside its name, and a fresh row with none', () => {
    const { container } = render(
      <AccountsTable accounts={[aged(), acct('primary')]} scope="u-bogdan" now={NOW} chosen={null} phone={false} onChoose={() => {}} />,
    )
    const rows = [...container.querySelectorAll<HTMLElement>('tr.acct-rank-row')]
    const row = (label: string) => rows.find((r) => r.textContent?.includes(`u-bogdan:${label}`))!
    // MUTATION: drop the aged tag from `Tags` -> fails.
    const tag = row('overflow').querySelector('.acct-tags .acct-tag.is-aged')
    expect(tag?.textContent).toBe('aged reading')
    expect(row('primary').querySelector('.acct-tag.is-aged')).toBeNull()
  })

  it('marks the aged card the same way on a phone', () => {
    const { container } = render(
      <AccountsTable accounts={[aged()]} scope="u-bogdan" now={NOW} chosen={null} phone onChoose={() => {}} />,
    )
    expect(container.querySelector('.acct-card .acct-tag.is-aged')?.textContent).toBe('aged reading')
  })
})
