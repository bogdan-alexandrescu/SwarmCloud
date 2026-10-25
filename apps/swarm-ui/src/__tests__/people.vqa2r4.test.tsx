// ADMIN › PEOPLE, VISUAL QA 2026-10-16 (#1427, lane VQA2-R4).
//
// Seven findings from the re-run, each measured in headless Chromium against
// a People list with a 34-character workspace id, a five-team list, a job in
// flight and a loan request:
//   * V021  at 1440 the table was 1151px in a 1024px card ("Last a", Manage
//           behind the table's scroll): the id was the Workspace column's
//           min-content and a long address took 362px;
//   * V081  at 400 "Checking the / name is free" wrapped, its state beside the
//           first line;
//   * V159  the Manage drawer said aria-modal and trapped nothing: Tab walked
//           into the skip link, the menu and the rail behind it;
//   * V161  at 400 the same id ran past the phone record (`.ctl-scroll` 458/400);
//   * V185  the Ceiling head was the one DM Mono head;
//   * V188  at 400 a loan request's Lend sat under the select as a 48px stub
//           and Decline went to a third line;
//   * V189  the phone record said "Manage" beside a button saying "Manage".
//
// jsdom lays nothing out, so each case holds the RULE that made the measured
// difference, read through `painted` (the cascade with specificity and media),
// and the markup and focus behaviour the fix needs.
//
// Placeholders only; nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve } from './repofixture'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { tabStops } from '../focus'

const WAIT = { timeout: 4000 }
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 400 }

type Json = Record<string, unknown>

const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString()
const LONG_ID = 'w-' + '0123456789abcdef'.repeat(2)

function ws(state: string, id: string, over: Json = {}): Json {
  return {
    state,
    setup_url: '/setup#workspace',
    setup_command: '/sc:setup',
    workspace_id: id,
    request_id: `req-${id}`,
    requested_at: ago(30),
    requested_via: 'console',
    decision: null,
    limits: { max_active: 8, capacity_units: 8, quota_pods: 16, quota_cpu: 64 },
    steps: {},
    failure: null,
    ready_at: null,
    request_again_at: null,
    dispatch: null,
    run: null,
    retried_at: null,
    ...over,
  }
}

function person(email: string, workspace: Json, over: Json = {}): Json {
  return {
    email,
    teams: ['eng'],
    github: 'connected',
    workspace,
    claude_account: { source: 'none', own: 0, lent: 0, lent_by: [], provider_key: false },
    loan_request: null,
    first_seen: ago(600),
    last_seen: ago(1),
    last_submitted: null,
    last_active: ago(3),
    ...over,
  }
}

const GUS = person('gus@example.com', ws('applying', LONG_ID, { steps: { A1: { state: 'done' }, A2: { state: 'running' } } }), {
  teams: ['platform-infrastructure', 'eng-release-managers', 'security-oncall', 'data-science', 'mobile'],
})
const FAY = person('fay@example.com', ws('ready', 'w-fa1000'), { loan_request: { state: 'requested', requested_at: ago(10) } })
const BOB = person('bob@example.com', ws('ready', 'w-91c0de'))
const ROWS = [GUS, FAY, BOB]

const DOC: Json = {
  people: ROWS,
  count: ROWS.length,
  pending: 0,
  audit: [],
  lendable_accounts: [
    { account_id: 'acct-eng-1', owner_tenant: 'eng', label: 'engineering shared pool (primary seat)', state: 'AVAILABLE', lend_to: [] },
    { account_id: 'acct-ops-1', owner_tenant: 'ops', label: 'operations shared pool (on-call)', state: 'AVAILABLE', lend_to: [] },
  ],
  lendable_error: null,
  admins: [{ email: 'owner@example.com', role: 'owner', granted_by: 'config:PLATFORM_OWNER', granted_at: null }],
  admins_error: null,
}

function media(phone: boolean): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: phone && query.includes('560'),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }))
}

async function mount(phone = false) {
  media(phone)
  serve((m, url) => (m === 'GET' && url === '/v1/admin/people' ? { status: 200, body: DOC } : null))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { PeopleScreen } = await import('../People')
  const utils = render(<PeopleScreen />)
  await screen.findByRole('heading', { name: 'People and roles', level: 1 }, WAIT)
  await waitFor(() => expect(document.querySelector('.pp-people tbody tr')).not.toBeNull(), WAIT)
  return utils
}

const rowOf = (email: string) => document.querySelector<HTMLElement>(`.pp-people tr[data-email="${email}"]`)!

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('a long workspace id is cut, not run past the card (V021, V161)', () => {
  it('caps the id at a length, ends it in an ellipsis and keeps it whole in its title, wide and on a phone', async () => {
    // MUTATION: drop the `.pp-ws code` cap, or write it as `min(100%, 16ch)`
    // (Chromium: 1136px of 1024 -- a percentage does not shrink a cell's min-content).
    for (const phone of [false, true]) {
      const { unmount } = await mount(phone)
      const id = rowOf('gus@example.com').querySelector<HTMLElement>('.pp-ws code.pp-id')!
      expect(id.textContent).toBe(LONG_ID)
      expect(id.getAttribute('title'), 'the cut id is not whole anywhere').toBe(LONG_ID)
      const env = phone ? PHONE : WIDE
      expect(painted(id, 'max-width', env)).toBe('16ch')
      expect(painted(id, 'display', env)).toBe('inline-block')
      expect(painted(id, 'overflow', env)).toBe('hidden')
      expect(painted(id, 'text-overflow', env)).toBe('ellipsis')
      // Still one token: never broken.
      expect(painted(id, 'white-space', env)).toBe('nowrap')
      unmount()
    }
  })

  it('caps an address at 15rem in the wide table, and keeps 18rem on the phone record', async () => {
    // MUTATION: the 18rem cap alone (362px of the 1024px card at 1440).
    let { unmount } = await mount(false)
    expect(painted(rowOf('gus@example.com').querySelector('.pp-who')!, 'max-width', WIDE)).toBe('15rem')
    unmount()
    ;({ unmount } = await mount(true))
    expect(painted(rowOf('gus@example.com').querySelector('.pp-who')!, 'max-width', PHONE)).toBe('18rem')
    unmount()
  })
})

describe('a job step reads on one line with its state (V081)', () => {
  it('gives the phone record\'s Workspace value the record\'s width, its key on the line above', async () => {
    // MUTATION: drop the Workspace override: the value is back in the ~166px column.
    await mount(true)
    const cell = rowOf('gus@example.com').querySelector<HTMLElement>('td[data-label="Workspace"]')!
    expect(painted(cell, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
    expect(painted(cell.querySelector('.pp-ws')!, 'grid-column', PHONE)).toBe('1')
    expect(painted(cell, 'content', PHONE, 'before'), 'the key is still said').toBe('attr(data-label)')
  })

  it('puts the state on the label\'s last line, and never lets a word run into it', async () => {
    // MUTATION: drop `align-self: last baseline` (the state beside "Checking the").
    await mount(false)
    const step = [...rowOf('gus@example.com').querySelectorAll<HTMLElement>('.ob-ws-steps li')].find((li) => li.textContent?.includes('Checking'))!
    expect(step.dataset.state).toBe('running')
    for (const env of [WIDE, PHONE]) {
      expect(painted(step.querySelector('.ob-state')!, 'align-self', env)).toBe('last baseline')
      expect(painted(step, 'grid-template-columns', env)).toBe('auto minmax(min-content, 1fr) auto')
    }
  })
})

describe('the Manage drawer holds focus (V159)', () => {
  it('makes the page behind inert, wraps Tab at both ends, and gives the page back on close', async () => {
    // MUTATION: drop the inert loop, or the `trapTab` call.
    const { container } = await mount(false)
    fireEvent.click(within(rowOf('bob@example.com')).getByRole('button', { name: 'Manage' }))
    const drawer = await screen.findByRole('dialog', { name: 'Manage bob@example.com' }, WAIT)
    const host = container.parentElement === document.body ? container : container.closest('body > *')!
    expect(host.hasAttribute('inert'), 'the page behind the drawer is reachable').toBe(true)
    expect(drawer.closest('[inert]'), 'the drawer itself is inert').toBeNull()
    const stops = tabStops(drawer)
    expect(stops.length).toBeGreaterThan(2)
    const first = stops[0]!
    const last = stops[stops.length - 1]!
    expect(first.textContent).toBe('Close')
    last.focus()
    fireEvent.keyDown(last, { key: 'Tab' })
    expect(document.activeElement, 'Tab off the last control left the drawer').toBe(first)
    fireEvent.keyDown(first, { key: 'Tab', shiftKey: true })
    expect(document.activeElement, 'Shift+Tab off Close left the drawer').toBe(last)
    // A Tab in the middle is the browser's: not swallowed.
    const middle = stops[1]!
    middle.focus()
    const ev = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })
    middle.dispatchEvent(ev)
    expect(ev.defaultPrevented, 'Tab inside the drawer was intercepted').toBe(false)
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Manage bob@example.com' })).toBeNull(), WAIT)
    expect(host.hasAttribute('inert'), 'the page stayed inert after the drawer closed').toBe(false)
    expect(document.querySelectorAll('[inert]').length).toBe(0)
  })
})

describe('the Ceiling head is a head (V185)', () => {
  it('takes the heads\' sans, not the figures\' mono, and keeps its right alignment', async () => {
    // MUTATION: drop `.pp-table > thead > tr > th.n` (`table.pools .n` paints DM Mono).
    await mount(false)
    const heads = [...document.querySelectorAll<HTMLElement>('.pp-people thead th')]
    const ceiling = heads.find((th) => th.textContent === 'Ceiling')!
    expect(ceiling.classList.contains('n')).toBe(true)
    expect(painted(ceiling, 'font-family', WIDE)).toBe('var(--font)')
    expect(painted(ceiling, 'text-align', WIDE)).toBe('right')
    // The figures below it keep the mono.
    expect(painted(rowOf('bob@example.com').querySelector('td.n')!, 'font-family', WIDE)).toBe('var(--mono)')
  })
})

describe('a loan request decides on one row (V188)', () => {
  it('on a phone: the select takes the line, then Lend and Decline share the next, half each', async () => {
    // MUTATION: drop the loan block: `.pp-loan` is its own flex group again,
    // so Lend wraps under a 320px select and Decline goes to a third line.
    await mount(true)
    const req = document.querySelector<HTMLElement>('.pp-req[data-kind="loan"]')!
    const loan = req.querySelector<HTMLElement>('.pp-loan')!
    expect(painted(loan, 'display', PHONE)).toBe('contents')
    expect(painted(loan.querySelector('.c-field')!, 'flex', PHONE)).toBe('1 1 100%')
    expect(painted(loan.querySelector('select')!, 'width', PHONE)).toBe('100%')
    expect(painted(loan.querySelector('select')!, 'max-width', PHONE)).toBe('none')
    const lend = within(req).getByRole('button', { name: 'Lend' })
    const decline = within(req).getByRole('button', { name: 'Decline' })
    for (const b of [lend, decline]) {
      expect(painted(b, 'flex', PHONE), `${b.textContent} does not share the row`).toBe('1 1 0')
      expect(painted(b, 'min-height', PHONE)).toBe('44px')
    }
    // Wide, the picker stays the inline group the owner asked for (2026-10-13).
    expect(painted(loan, 'display', WIDE)).toBe('flex')
  })
})

describe('the phone record\'s Manage is the button alone (V189)', () => {
  it('draws no "Manage" key beside the Manage button, which spans the record', async () => {
    // MUTATION: drop the `td.pp-manage-cell::before` rule.
    await mount(true)
    const cell = rowOf('bob@example.com').querySelector<HTMLElement>('td.pp-manage-cell')!
    // The cell is still named for the record's markup (admin.people.test.tsx).
    expect(cell.dataset.label).toBe('Manage')
    expect(painted(cell, 'content', PHONE, 'before')).toBe('none')
    expect(painted(cell, 'display', PHONE)).toBe('block')
    const button = within(cell).getByRole('button', { name: 'Manage' })
    expect(painted(button, 'width', PHONE)).toBe('100%')
    expect(painted(button, 'min-height', PHONE)).toBe('44px')
  })
})
