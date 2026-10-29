// VISUAL QA PASS 1, LANE QA1-FIX-03 (part of #2596): the Accounts forecast
// and the verdict, in AccountsVerdict.tsx.
//
//   capacity-20  one never-polled account blanked the whole forecast to a
//                lone `Goes dark —`, with no `Back` tile under a title that
//                promises one
//   capacity-21  internal names in the reader's copy: `claudeswitch's
//                subscriptions`, `This broker does not ...`

import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { PoolAccount } from '../accountsPool'
import { Forecast, Verdict } from '../AccountsVerdict'

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
      // Four days into its week at 70%: it reaches its floor before it resets.
      seven_day: { utilization: 0.7, resets_at: iso(NOW + 3 * D), reset: false },
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

const tiles = (c: HTMLElement) =>
  [...c.querySelectorAll('.acct-fc-tile')].map((t) => ({
    k: t.querySelector('.acct-fc-k')?.textContent ?? '',
    v: t.querySelector('.acct-fc-v')?.textContent ?? '',
  }))

describe('capacity-20: an account with no 7-day reading is left out and named, not allowed to blank the forecast', () => {
  it('forecasts from the measured accounts and draws the Back tile', () => {
    const accounts = [acct('home-personal'), acct('never-polled', { observed_at: null, windows: {} })]
    const { container } = render(<Forecast accounts={accounts} scope="eng" now={NOW} />)
    const drawn = tiles(container)
    // MUTATION: return to the lone `Goes dark —` whenever `unmeasured` is not
    // empty: the dark tile reads `—` and there is no Back tile.
    const dark = drawn.find((t) => t.k === 'Goes dark')
    expect(dark?.v, 'the dark tile is blank').not.toBe('—')
    expect(drawn.map((t) => t.k)).toContain('Back')
    // The account left out is named, whole, under the tiles.
    const left = container.querySelector('.acct-fc-left') as HTMLElement | null
    expect(left, 'nothing says which account was left out').not.toBeNull()
    expect(left!.textContent).toContain('never-polled has no 7-day reading to take a pace from')
    expect([...left!.querySelectorAll('.acct-nm')].map((n) => n.textContent)).toEqual(['never-polled'])
    // The pace line counts only the measured one.
    expect(container.querySelector('.acct-fc-sub')?.textContent).toContain('together 1 subscription')
  })

  it('draws both tiles as dashes, with the reason, when no account has a reading', () => {
    const accounts = [acct('never-polled', { observed_at: null, windows: {} })]
    const { container } = render(<Forecast accounts={accounts} scope="eng" now={NOW} />)
    // MUTATION: drop the Back tile from the no-reading case.
    expect(tiles(container)).toEqual([
      { k: 'Goes dark', v: '—' },
      { k: 'Back', v: '—' },
    ])
    expect(container.querySelector('.acct-fc-left'), 'the reason is said twice').toBeNull()
  })

  it('says nothing is left out when every account has a reading', () => {
    const { container } = render(<Forecast accounts={[acct('home-personal')]} scope="eng" now={NOW} />)
    expect(container.querySelector('.acct-fc-left')).toBeNull()
  })
})

describe('capacity-21: the forecast and the verdict speak in the reader’s words', () => {
  it('calls the laptop tool’s subscriptions laptop sessions', () => {
    const { container } = render(<Forecast accounts={[acct('home-personal')]} scope="eng" now={NOW} />)
    // MUTATION: restore `these are claudeswitch's subscriptions too`.
    expect(container.textContent ?? '').not.toMatch(/claudeswitch/i)
    expect(container.querySelector('.acct-fc-note')?.textContent).toContain('Laptop sessions spend the same windows')
  })

  it('never calls the deployment a broker', () => {
    // A deployment that serves no per-account answer.
    const bare = acct('home-personal')
    delete bare.can_take_new_agent
    const { container } = render(
      <Verdict accounts={[bare]} scope="eng" shortfall={0} now={NOW} waiting={{ kind: 'loading' }} />,
    )
    const verdict = container.querySelector('.acct-verdict') as HTMLElement
    // MUTATION: restore `This broker does not say which accounts can take a new agent.`
    expect(verdict.textContent).toContain('This deployment does not say which accounts can take a new agent.')
    const said = [...verdict.querySelectorAll('[title]')].map((e) => e.getAttribute('title') ?? '')
    expect(`${verdict.textContent} ${said.join(' ')}`).not.toMatch(/broker|can_take_new_agent/)
  })
})
