// WHAT A CHECK'S PROBLEM MEANS IS ITS TITLE'S TOOLTIP (the owner's pick,
// 2026-11-05; the no-prose rule). Each check card ended its detail on a
// sentence explaining the problem -- the same words on every read. That
// sentence is the problem's `why` now: the card title's `title` and its
// accessible description, through a hidden node, and never drawn on the card.
//
// Each test names the mutation that turns it red.

import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { CheckCard } from '../Overview'
import { deriveChecks, type Check, type CheckInputs, type Problem } from '../checks'
import type { Result } from '../fetch'
import type { Account, AccountsPage, Stats } from '../types'

const NOW = Date.parse('2026-11-05T09:00:00Z')

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: NOW, serverAt: new Date(NOW).toISOString() }
}

const LOADING = { status: 'loading', since: 0 } as const

function inputs(over: Partial<CheckInputs>): CheckInputs {
  return {
    capacity: LOADING,
    tasks: LOADING,
    leases: LOADING,
    providers: LOADING,
    accounts: LOADING,
    stats: LOADING,
    workflows: LOADING,
    ...over,
  } as CheckInputs
}

const NEVER_POLLED: Account = {
  account_id: 'eng:never',
  owner_tenant: 'eng',
  label: 'never',
  provider: 'anthropic-subscription',
  state: 'AVAILABLE',
  reason: '',
  lend_to: [],
  assigned: 0,
  windows: {},
  observed_at: null,
  stale: false,
  unreadable_by: [],
  unreadable_now: [],
  last_assigned_at: null,
}

const PAUSED: Stats = {
  tenant_id: 'eng',
  tasks_by_state: {},
  dispatch_paused: true,
  limits: {},
  generated_at: '2026-11-05T09:00:00Z',
}

const NEVER_WHY = 'No reading has ever arrived, so their utilisation is unknown rather than zero and they cannot be counted as headroom.'
const PAUSED_WHY = 'Admission still runs and leases are still taken, but nothing is handed to a backend. Every agent submitted from now on waits.'

function found(checks: Check[], label: string): Problem {
  const c = checks.find((x) => x.label === label)
  expect(c?.status, `${label} found nothing`).toBe('found')
  return (c as Extract<Check, { status: 'found' }>).problems[0]!
}

const text = (n: Element | null | undefined): string => (n?.textContent ?? '').replace(/\s+/g, ' ').trim()

/** What the card draws: its text with the help nodes taken out, as the prose ratchet counts it. */
function drawn(el: Element): string {
  const clone = el.cloneNode(true) as Element
  for (const h of [...clone.querySelectorAll('[data-help-description]')]) h.remove()
  return text(clone)
}

afterEach(cleanup)

describe('a check’s explanation is its `why`, not its detail', () => {
  const checks = deriveChecks(
    inputs({
      accounts: ok<AccountsPage>({ accounts: [NEVER_POLLED], tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 }),
      stats: ok(PAUSED),
    }),
    NOW,
  )

  it('carries the sentence the detail used to end on as `why`', () => {
    const never = found(checks, 'Accounts')
    // MUTATION: put the explanation back after the account names in the detail.
    expect(never.detail).toBe('never')
    expect(never.why).toBe(NEVER_WHY)
    const paused = found(checks, 'Dispatch')
    // MUTATION: leave the explanation in `detail` and drop `why`.
    expect(paused.detail).toBe('')
    expect(paused.why).toBe(PAUSED_WHY)
  })

  it('draws `why` as the title’s tooltip on a one-link card, and not on the card', () => {
    const p = found(checks, 'Accounts')
    const { container } = render(<CheckCard problem={p} />)
    const card = container.querySelector('a.ov-att')!
    const title = card.querySelector('.ov-att-t b')!
    // MUTATION: keep the headline as the title's tooltip.
    expect(title.getAttribute('title')).toBe(NEVER_WHY)
    // The card is the one focus stop, so it carries the description a keyboard hears.
    // MUTATION: drop the link's `aria-describedby`.
    const said = document.getElementById(card.getAttribute('aria-describedby') ?? '')
    expect(text(said)).toBe(NEVER_WHY)
    expect(said!.hidden, 'the explanation is drawn on the surface').toBe(true)
    // MUTATION: draw `why` as a line of the card again.
    expect(drawn(card)).not.toContain('No reading has ever arrived')
    expect(drawn(card)).toContain('never')
  })

  it('draws no empty detail line when the detail was all explanation', () => {
    const p = found(checks, 'Dispatch')
    const { container } = render(<CheckCard problem={p} />)
    const card = container.querySelector('a.ov-att')!
    // MUTATION: render the detail's `<small>` whatever it holds.
    expect(card.querySelectorAll('.ov-att-t small')).toHaveLength(0)
    expect(card.getAttribute('aria-label')).toBe(`${p.headline}.`)
    expect(drawn(card)).not.toContain('Admission still runs')
    expect(card.querySelector('.ov-att-t b')!.getAttribute('title')).toBe(PAUSED_WHY)
  })

  it('makes the title its own keyboard stop on a card that is a box', () => {
    const p: Problem = {
      ...found(checks, 'Accounts'),
      headline: '1 workflow has not advanced for 20m · wf_box',
      workflows: [{ id: 'wf_box', href: '#work/workflows?wf=wf_box' }],
    }
    const { container } = render(<CheckCard problem={p} />)
    const box = container.querySelector('div.ov-att.is-split')!
    const stop = box.querySelector('.ov-att-why')!
    // MUTATION: draw the title as a bare `<b>` again.
    expect(stop.getAttribute('tabindex')).toBe('0')
    expect(stop.getAttribute('title')).toBe(NEVER_WHY)
    const said = document.getElementById(stop.getAttribute('aria-describedby') ?? '')
    expect(text(said)).toBe(NEVER_WHY)
    expect(said!.hidden).toBe(true)
    expect(drawn(box)).not.toContain('No reading has ever arrived')
  })

  it('leaves a problem with no explanation as it was', () => {
    const p: Problem = { severity: 'warn', n: 1, headline: 'One thing', detail: 'Facts only.', href: '#work/workflows' }
    const { container } = render(<CheckCard problem={p} />)
    const card = container.querySelector('a.ov-att')!
    expect(card.querySelector('b')!.getAttribute('title')).toBe('One thing')
    expect(card.hasAttribute('aria-describedby')).toBe(false)
    expect(card.getAttribute('aria-label')).toBe('One thing. Facts only.')
  })
})
