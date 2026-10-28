// #2222, ACCOUNTS-A-LIST: THE PHONE FRAMES (mock-up A, P1, P2, P4, P5).
//
// Rendered at 390 on 2026-10-25, two things the phone did differently from
// the mock-up:
//
//   * the cards dropped both windows' bars and their floor ticks
//     (`.acct-floor { display: none }` under 560px), so a phone could not see
//     the 15% / 5% rule the desktop draws -- mock-up A's cards draw the same
//     window cell as the table;
//   * the verdict broke an account name at its hyphen (`five-` / `hour-floor`),
//     which reads as two accounts; the table already keeps names whole.
//
// BREAK IT: hide `.acct-floor` in the phone block again, or drop `acct-nm`
// from a name in the verdict, the duplicate banner or the forecast.

import { render } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

import type { PoolAccount } from '../accountsPool'
import { AccountsTable } from '../AccountsTable'
import { DuplicateBanner, Forecast, Verdict } from '../AccountsVerdict'

const H = 3_600_000
const D = 24 * H
const NOW = Date.parse('2026-10-24T09:04:00Z')
const iso = (t: number) => new Date(t).toISOString()

function acct(label: string, over: Partial<PoolAccount> = {}): PoolAccount {
  return {
    account_id: `eng:${label}`,
    owner_tenant: 'eng',
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

const css = readFileSync(join(__dirname, '..', 'styles', 'accounts-ranked.css'), 'utf8')

/** The rules inside the phone block (`@media (max-width: 560px)`), braces balanced. */
function phoneBlock(): string {
  const at = css.indexOf('@media (max-width: 560px)')
  expect(at).toBeGreaterThanOrEqual(0)
  const open = css.indexOf('{', at)
  let depth = 0
  for (let i = open; i < css.length; i += 1) {
    if (css[i] === '{') depth += 1
    if (css[i] === '}') depth -= 1
    if (depth === 0) return css.slice(open + 1, i)
  }
  throw new Error('the phone block never closes')
}

describe('the phone cards draw each window against its floor', () => {
  it('keeps the bar and its floor tick on a phone, as mock-up A’s cards do', () => {
    expect(phoneBlock()).not.toMatch(/\.acct-floor\s*\{[^}]*display:\s*none/)
    // styles.css's phone block hides every `.ctl-util-track`; the card's is the exception that draws the floor.
    expect(phoneBlock()).toMatch(/\.acct-card-w2 \.acct-floor > \.ctl-util-track\s*\{[^}]*display:\s*flex/)
    const { container } = render(
      <AccountsTable accounts={[acct('primary')]} scope="eng" now={NOW} chosen={null} phone onChoose={() => {}} />,
    )
    const card = container.querySelector('.acct-card') as HTMLElement
    const ticks = [...card.querySelectorAll<HTMLElement>('.acct-card-w2 .acct-floor > s')].map((s) => s.style.left)
    expect(ticks).toEqual(['85%', '95%'])
  })
})

describe('an account name is never broken at its hyphen', () => {
  // THE VERDICT JUDGES A GONE CLOCK AGAINST ITS OWN `now` PROP (capacity-03,
  // QA1B-02 and QA1C-FOLLOWUPS): a reset 40 minutes after NOW is long past on
  // the wall clock, and still named, because `Verdict` hands `verdictOf` the
  // board's clock. No fake Date is held for it.
  it('holds every name in the verdict whole', () => {
    expect(css).toMatch(/\.acct-nm\s*\{[^}]*white-space:\s*nowrap/)
    const now = NOW
    const accounts = [
      acct('never-polled'),
      acct('weekly-burn'),
      acct('five-hour-floor', {
        can_take_new_agent: false,
        blocked_by: [{ window: 'five_hour', used: 0.92, floor: 0.15, resets_at: iso(now + 40 * 60_000) }],
        next_eligible_at: iso(now + 40 * 60_000),
      }),
    ]
    const { container } = render(
      <Verdict accounts={accounts} scope="eng" shortfall={0} now={now} waiting={{ kind: 'loading' }} />,
    )
    const names = [...container.querySelectorAll('.acct-nm')].map((n) => n.textContent)
    expect(names).toEqual(expect.arrayContaining(['never-polled', 'weekly-burn', 'five-hour-floor']))
  })

  it('holds the next account whole when none can take one', () => {
    const accounts = [
      acct('five-hour-floor', {
        can_take_new_agent: false,
        blocked_by: [{ window: 'five_hour', used: 0.92, floor: 0.15, resets_at: iso(NOW + 40 * 60_000) }],
        next_eligible_at: iso(NOW + 40 * 60_000),
      }),
    ]
    const { container } = render(
      <Verdict accounts={accounts} scope="eng" shortfall={0} now={NOW} waiting={{ kind: 'loading' }} />,
    )
    const head = container.querySelector('.acct-verdict-head') as HTMLElement
    expect([...head.querySelectorAll('.acct-nm')].map((n) => n.textContent)).toEqual(['five-hour-floor'])
  })

  it('holds both names of a pair whole in the duplicate banner', () => {
    const accounts = [
      acct('home-personal'),
      acct('team-copy', {
        can_take_new_agent: false,
        blocked_by: [{ duplicate_of: 'eng:home-personal', reason: 'registered on the same organisation as eng:home-personal' }],
      }),
    ]
    const { container } = render(<DuplicateBanner accounts={accounts} scope="eng" onOpen={() => {}} />)
    const head = container.querySelector('.acct-dup-head') as HTMLElement
    expect(head.textContent).toBe('team-copy and home-personal are one subscription')
    expect([...head.querySelectorAll('.acct-nm')].map((n) => n.textContent)).toEqual(['team-copy', 'home-personal'])
  })

  it('holds the unmeasured names whole in the forecast', () => {
    const accounts = [acct('never-polled', { observed_at: null, windows: {} })]
    const { container } = render(<Forecast accounts={accounts} scope="eng" now={NOW} />)
    const tile = container.querySelector('.acct-fc-tile') as HTMLElement
    expect(tile.textContent).toContain('never-polled has no 7-day reading to take a pace from')
    expect([...tile.querySelectorAll('.acct-nm')].map((n) => n.textContent)).toEqual(['never-polled'])
  })
})

describe('the phone’s cards keep names and ids whole', () => {
  it('holds each folded name whole in the “more” line', () => {
    const accounts = ['a-one', 'b-two', 'c-three', 'd-four', 'e-five', 'f-six'].map((l) => acct(l))
    const { container } = render(
      <AccountsTable accounts={accounts} scope="eng" now={NOW} chosen={null} phone onChoose={() => {}} />,
    )
    const more = container.querySelector('.acct-card-more') as HTMLElement
    expect(more.textContent).toContain('2 more: e-five, f-six.')
    expect([...more.querySelectorAll('.acct-nm')].map((n) => n.textContent)).toEqual(['e-five', 'f-six'])
  })

  it('moves a card’s account id to its own line before it breaks it', () => {
    expect(css).toMatch(/\.acct-card-meta \.raw\s*\{[^}]*display:\s*inline-block/)
  })
})

describe('a duplicate tag keeps the other account’s name whole', () => {
  it('holds the name in “same subscription as …” whole, on the table and the cards', () => {
    const accounts = [
      acct('primary', { duplicate_of: 'eng:team-copy' }),
      acct('team-copy', {
        can_take_new_agent: false,
        blocked_by: [{ duplicate_of: 'eng:primary', reason: 'registered on the same organisation as eng:primary' }],
        duplicate_of: 'eng:primary',
      }),
    ]
    for (const phone of [false, true]) {
      const { container, unmount } = render(
        <AccountsTable accounts={accounts} scope="eng" now={NOW} chosen={null} phone={phone} onChoose={() => {}} />,
      )
      const tags = [...container.querySelectorAll<HTMLElement>('.acct-tag.is-dup')]
      expect(tags.map((t) => t.textContent).sort()).toEqual(['same subscription as primary', 'same subscription as team-copy'])
      expect(tags.map((t) => t.querySelector('.acct-nm')?.textContent).sort()).toEqual(['primary', 'team-copy'])
      unmount()
    }
  })
})
