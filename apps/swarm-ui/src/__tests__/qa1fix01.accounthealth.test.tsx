// QA1-FIX-01 (part of #2596, console visual QA pass 1): CAPACITY › ACCOUNT HEALTH.
//
// Drawn from the development pool (`loadAccountHealthBoard` in the fixture
// build), the board the sweep read. WHAT EACH CASE HOLDS:
//   * an account the broker holds back for a reason of its own (paused,
//     skipped here, one subscription twice) says so beside its health, in
//     the words Accounts uses, so the two screens do not contradict each
//     other about it (capacity-04);
//   * the Erroring tile says when an account with provider errors in the
//     window is counted under a worse status, so its 0 is not read against
//     the table's Provider errors column (capacity-05);
//   * the re-auth banner's "since" is the open incident's, the one the phone's
//     card prints (capacity-06);
//   * the phone's Incidents/Accounts switch keeps focus on the chosen button,
//     and its arrow keys move between them (capacity-09);
//   * a Last event message is drawn whole, wrapping in its column, never cut
//     with an ellipsis a reader cannot open (chrome-C26).

import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { cleanup, fireEvent, render, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { AccountHealthBody } from '../AccountHealthPage'
import { loadAccountHealthBoard, type AccountHealthBoard } from '../api'
import { poolIncidents, when, type HealthAccount, type HealthStatus } from '../accountHealthModel'

afterEach(cleanup)

async function fixtureBoard(): Promise<AccountHealthBoard> {
  const r = await loadAccountHealthBoard()
  if (r.status !== 'ok' || r.data.health.kind !== 'ok') throw new Error('the fixture board did not load')
  return r.data
}

const row = (c: HTMLElement, id: string) => c.querySelector(`tr[data-account="${id}"]`) as HTMLElement

describe('capacity-04: an account the broker holds back says so beside its health', () => {
  it('draws weekly-burn as paused, shared as skipped here and team-copy as one subscription twice', async () => {
    const b = await fixtureBoard()
    const { container } = render(<AccountHealthBody board={b} now={b.readAt} phone={false} />)
    // MUTATION: drop the broker's own reason (BrokerNote) from StatusCellView.
    expect(row(container, 'u-bogdan:weekly-burn').querySelector('[data-broker]')?.textContent).toMatch(/^Paused: held back for the release run on Monday/)
    expect(row(container, 'eng:shared').querySelector('[data-broker]')?.textContent).toMatch(/^Skipped here/)
    expect(row(container, 'u-bogdan:team-copy').querySelector('[data-broker]')?.textContent).toMatch(/^One subscription twice/)
    // An account the broker hands out draws no such line.
    expect(row(container, 'u-bogdan:primary').querySelector('[data-broker]')).toBeNull()
  })

  it('says it on the phone’s account card too', async () => {
    const b = await fixtureBoard()
    const { container } = render(<AccountHealthBody board={b} now={b.readAt} phone />)
    fireEvent.click(within(container.querySelector('.ah-switch') as HTMLElement).getByRole('button', { name: 'Accounts' }))
    const card = container.querySelector('a.ah-acard[data-account="u-bogdan:weekly-burn"]') as HTMLElement
    expect(card.querySelector('[data-broker]')?.textContent).toMatch(/^Paused/)
  })
})

describe('capacity-05: the Erroring tile and the Provider errors column agree', () => {
  it('names the accounts with provider errors that a worse status outranks', async () => {
    const b = await fixtureBoard()
    const { container } = render(<AccountHealthBody board={b} now={b.readAt} phone={false} />)
    const tile = container.querySelector('[data-tile="erroring"]') as HTMLElement
    // MUTATION: drop the "also with provider errors" line under the Erroring tile.
    const also = tile.querySelector('[data-outranked]') as HTMLElement
    expect(also).not.toBeNull()
    expect(also.getAttribute('data-outranked')).toBe('1')
    expect(also.textContent).toContain('overflow')
    expect(also.textContent).toContain('Limited')
  })
})

describe('capacity-06: one "since" for the re-auth incident', () => {
  it('prints the open incident’s start in the banner, the time the phone’s card prints', async () => {
    const b = await fixtureBoard()
    if (b.health.kind !== 'ok') throw new Error('no health')
    const accounts = b.page.accounts as HealthAccount[]
    const status = new Map(accounts.map((a) => [a.account_id, a.health_status as HealthStatus | null]))
    const { open } = poolIncidents(b.health.pool, (id) => status.get(id) ?? null, b.readAt)
    const x = open.find((i) => i.account_id === 'u-bogdan:retired-laptop' && i.family === 'cred')!
    const since = `since ${when(x.from, b.readAt)}`

    const desk = render(<AccountHealthBody board={b} now={b.readAt} phone={false} />)
    const banner = desk.container.querySelector('[data-state="reauth"][data-account="u-bogdan:retired-laptop"] b') as HTMLElement
    // MUTATION: take the banner's time from the newest auth or reauth event again.
    expect(banner.textContent).toBe(`retired-laptop needs re-auth ${since}`)
    desk.unmount()

    const phone = render(<AccountHealthBody board={b} now={b.readAt} phone />)
    expect(phone.container.textContent).toContain(since)
  })
})

describe('capacity-09: the phone switch keeps focus', () => {
  it('leaves focus on the chosen button after either choice, and moves with the arrow keys', async () => {
    const b = await fixtureBoard()
    const { container } = render(<AccountHealthBody board={b} now={b.readAt} phone />)
    const sw = container.querySelector('.ah-switch') as HTMLElement
    const accounts = within(sw).getByRole('button', { name: 'Accounts' })
    accounts.focus()
    fireEvent.click(accounts)
    expect(document.activeElement?.textContent).toBe('Accounts')
    expect(document.activeElement?.getAttribute('aria-pressed')).toBe('true')
    // MUTATION: drop the switch's onKeyDown (arrow keys do nothing).
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: 'ArrowLeft' })
    expect(document.activeElement?.textContent).toMatch(/^Incidents/)
    expect(document.activeElement?.getAttribute('aria-pressed')).toBe('true')
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: 'ArrowRight' })
    expect(document.activeElement?.textContent).toBe('Accounts')
    expect(container.querySelector('[data-tile="erroring"]')).not.toBeNull()
  })
})

describe('chrome-C26: a Last event message can be read whole', () => {
  it('draws the whole message, wrapping inside its held column, with no one-line cut', async () => {
    const b = await fixtureBoard()
    const { container } = render(<AccountHealthBody board={b} now={b.readAt} phone={false} />)
    const msg = row(container, 'u-bogdan:retired-laptop').querySelector<HTMLElement>('.ah-msg')!
    // MUTATION: put the is-one-line class (and its ellipsis rule) back on the message.
    expect(msg.classList.contains('is-one-line')).toBe(false)
    expect(msg.textContent).toBe('not assigned: the account needs re-auth')
    const css = readFileSync(join(__dirname, '..', 'styles', 'account-health.css'), 'utf8')
    expect(css).not.toMatch(/\.ah-msg[^{]*\{[^}]*text-overflow:\s*ellipsis/)
    // The column is still held to its width, so a long message wraps rather than widening the table.
    expect(css).toMatch(/\.ah-last-td \.ah-last\s*\{[^}]*max-width:\s*280px/)
  })
})

describe('capacity-04 review: the broker note wraps inside the Status column', () => {
  it('draws the note in a class of its own that wraps and is held to a width', async () => {
    const b = await fixtureBoard()
    const { container } = render(<AccountHealthBody board={b} now={b.readAt} phone={false} />)
    const notes = container.querySelectorAll<HTMLElement>('tbody [data-broker]')
    expect(notes.length).toBeGreaterThan(0)
    // MUTATION: draw the note as .ah-st-until again (display: block alone): it
    // inherits the cell's nowrap, the Status column grows to 573 px and the
    // table runs 329 px past its card at 1440, off "Sign in again ›".
    notes.forEach((n) => expect(n.classList.contains('ah-broker')).toBe(true))
    const css = readFileSync(join(__dirname, '..', 'styles', 'account-health.css'), 'utf8')
    const rule = css.match(/\.ah-broker\s*\{([^}]*)\}/)?.[1] ?? ''
    expect(rule).toMatch(/white-space:\s*normal/)
    expect(rule).toMatch(/max-width:\s*\d+px/)
  })
})
