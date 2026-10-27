// ADMIN › PEOPLE, WHICH ACCOUNT IS LENT (#1206, lane PEOPLE-LOAN-VIEW).
//
// Owner report 2026-10-13 05:10 PDT: after lending a Claude account the
// drawer said "Now using lent by eng" -- `lent_by` is the OWNER TENANTS, so
// it never said which account -- and the reclaim picker sat under "Account
// lent to them" as an empty "choose…" select, the account invisible until it
// was opened. And the picker was drawn huge: `.pp-loan select` was
// `width: 100%` inside a growing flex field, so it took the drawer's width.
//
// WHAT EACH CASE HOLDS:
//   * "Now using" names each lent account (id, then label) and the lending
//     tenant's display name; several are all listed; own / provider key are
//     as before;
//   * one lent account is text and a Reclaim button behind the confirm, no
//     select; several are a select;
//   * a lend re-renders the section with the account the re-read names;
//   * the picker select is a normal control: no width 100%, no flex grow,
//     at most 320px, one control high, at 1260px and at 390px.
//
// Placeholders only; nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WAIT = { timeout: 4000 }
const WIDE: CascadeEnv = { width: 1260 }
const PHONE: CascadeEnv = { width: 390 }

type Json = Record<string, unknown>

const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString()

function ws(state: string, id: string | null, over: Json = {}): Json {
  const base: Json = { state, setup_url: '/setup#workspace', setup_command: '/sc:setup' }
  if (id === null) return base
  return {
    ...base,
    workspace_id: id,
    request_id: `req-${id}`,
    requested_at: ago(5),
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
    last_active: ago(1),
    ...over,
  }
}

const ADMINS = [
  { email: 'owner@example.com', role: 'owner', granted_by: 'config:PLATFORM_OWNER', granted_at: null },
  { email: 'bob@example.com', role: 'admin', granted_by: 'owner@example.com', granted_at: ago(60) },
]

const ENG = { account_id: 'acct-eng-1', owner_tenant: 'eng', label: 'eng shared', state: 'AVAILABLE', lend_to: [] }
const OPS = { account_id: 'acct-ops-1', owner_tenant: 'ops', label: 'ops shared', state: 'AVAILABLE', lend_to: [] }

function people(rows: Json[], over: Json = {}): Json {
  return {
    people: rows,
    count: rows.length,
    pending: rows.filter((r) => (r.workspace as Json).state === 'requested').length,
    audit: [],
    lendable_accounts: [ENG, OPS],
    lendable_error: null,
    admins: ADMINS,
    admins_error: null,
    ...over,
  }
}

const BOB = person('bob@example.com', ws('ready', 'w-91c0de'), { claude_account: { source: 'own', own: 2, lent: 0, lent_by: [], provider_key: false } })

async function mount(body: Json, writes: Record<string, (body: unknown) => { status: number; body: unknown }> = {}) {
  const calls = serve((m, url, b) => {
    if (m === 'GET' && url === '/v1/admin/people') return { status: 200, body }
    const key = `${m} ${url}`
    if (writes[key] !== undefined) return writes[key]!(b)
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { PeopleScreen } = await import('../People')
  const utils = render(<PeopleScreen />)
  await screen.findByRole('heading', { name: 'People and roles', level: 1 }, WAIT)
  await waitFor(() => expect(document.querySelector('.pp-people tbody tr')).not.toBeNull(), WAIT)
  return { ...utils, calls }
}

const rowOf = (email: string) => document.querySelector<HTMLElement>(`.pp-people tr[data-email="${email}"]`)!
const writesOf = (calls: { method: string; url: string; body: unknown }[]) => calls.filter((c) => c.method !== 'GET')

async function manage(email: string): Promise<HTMLElement> {
  fireEvent.click(within(rowOf(email)).getByRole('button', { name: 'Manage' }))
  return screen.findByRole('dialog', { name: `Manage ${email}` }, WAIT)
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

const LENT_ENG = { account_id: 'acct-eng-1', label: 'eng shared', owner_tenant: 'eng', owner_name: 'Engineering', state: 'AVAILABLE' }
const LENT_OPS = { account_id: 'acct-ops-1', label: 'ops shared', owner_tenant: 'ops', owner_name: 'Operations', state: 'AVAILABLE' }

function lentTo(email: string, id: string, accounts: Json[]): Json {
  const owners = [...new Set(accounts.map((a) => a.owner_tenant as string))].sort()
  return person(email, ws('ready', id), {
    claude_account: { source: 'lent', own: 0, lent: accounts.length, lent_by: owners, lent_accounts: accounts, provider_key: false },
  })
}

const sectionOf = (drawer: HTMLElement) => drawer.querySelector<HTMLElement>('[data-section="claude"]')!

describe('the Claude account section names the account they are using', () => {
  it('names the lent account first and the lending tenant by its display name', async () => {
    await mount(people([lentTo('hal@example.com', 'w-4a1000', [LENT_ENG])]))
    const section = sectionOf(await manage('hal@example.com'))
    // MUTATION: claudeText back to `lent by ${c.lent_by.join(', ')}`.
    expect(visible(section)).toContain('Now using acct-eng-1 (eng shared), lent by Engineering')
    expect(visible(rowOf('hal@example.com'))).toContain('acct-eng-1 (eng shared), lent by Engineering')
  })

  it('lists every lent account, each with its lender', async () => {
    await mount(people([lentTo('hal@example.com', 'w-4a1000', [LENT_ENG, LENT_OPS])]))
    const section = sectionOf(await manage('hal@example.com'))
    // The grid's one line, the accounts behind it (rendered review of PR 2464).
    expect(visible(section.querySelector('.pp-loan-sum'))).toBe('2 accounts · lent by Engineering, Operations')
    expect([...section.querySelectorAll('.pp-loan-list > li')].map((li) => visible(li))).toEqual([
      'acct-eng-1 (eng shared), lent by Engineering',
      'acct-ops-1 (ops shared), lent by Operations',
    ])
  })

  it('says an account without a label by its id alone, and own and provider key as before', async () => {
    const bare = { ...LENT_ENG, label: null, owner_name: null }
    const key = person('kay@example.com', ws('ready', 'w-0c1000'), { claude_account: { source: 'provider_key', own: 0, lent: 0, lent_by: [], provider_key: true } })
    await mount(people([lentTo('hal@example.com', 'w-4a1000', [bare]), BOB, key]))
    expect(visible(rowOf('hal@example.com'))).toContain('acct-eng-1, lent by eng')
    expect(visible(rowOf('bob@example.com'))).toContain('own (2)')
    expect(visible(rowOf('kay@example.com'))).toContain('provider key')
  })
})

describe('one lent account is text and Reclaim; several are a select', () => {
  it('shows the one lent account as text with Reclaim, no select, and reclaims it behind the confirm', async () => {
    const { calls } = await mount(people([lentTo('hal@example.com', 'w-4a1000', [LENT_ENG])]), {
      'PUT /v1/admin/people/w-4a1000/loan': () => ({ status: 200, body: { workspace_id: 'w-4a1000', account_id: 'acct-eng-1', owner_tenant: 'eng', lent: false, changed: true } }),
    })
    const section = sectionOf(await manage('hal@example.com'))
    // MUTATION: render the select for one account too.
    expect(section.querySelector('select')).toBeNull()
    expect(section.querySelector('[data-testid="pp-loan-one"]')?.textContent).toContain('acct-eng-1 (eng shared)')
    fireEvent.click(within(section).getByRole('button', { name: 'Reclaim' }))
    const confirm = await screen.findByRole('dialog', { name: 'Reclaim this account' }, WAIT)
    expect(writesOf(calls)).toHaveLength(0)
    expect(visible(confirm)).toContain('Reclaim eng shared (eng) from hal@example.com?')
    fireEvent.click(within(confirm).getByRole('button', { name: 'Reclaim' }))
    await waitFor(() => expect(writesOf(calls)).toHaveLength(1), WAIT)
    expect(writesOf(calls)[0]!.body).toEqual({ account_id: 'acct-eng-1', lend: false })
  })

  it('reclaims by account, not by owner: another account the same owner holds is not offered', async () => {
    const second = { account_id: 'acct-eng-2', owner_tenant: 'eng', label: 'eng second', state: 'AVAILABLE', lend_to: [] }
    await mount(people([lentTo('hal@example.com', 'w-4a1000', [LENT_ENG])], { lendable_accounts: [ENG, second, OPS] }))
    const section = sectionOf(await manage('hal@example.com'))
    // MUTATION: lentToRow by owner tenant alone -> two eng accounts -> a select.
    expect(section.querySelector('select')).toBeNull()
    expect(section.querySelector('[data-testid="pp-loan-one"]')?.textContent).toContain('acct-eng-1')
  })

  it('offers a select when several accounts are lent to them', async () => {
    await mount(people([lentTo('hal@example.com', 'w-4a1000', [LENT_ENG, LENT_OPS])]))
    const section = sectionOf(await manage('hal@example.com'))
    const select = within(section).getByLabelText('Account to reclaim from hal@example.com')
    expect([...select.querySelectorAll('option')].map((o) => o.getAttribute('value'))).toEqual(['', 'acct-eng-1', 'acct-ops-1'])
    expect(section.querySelector('[data-testid="pp-loan-one"]')).toBeNull()
  })
})

describe('after a lend the section names the new account without a reload', () => {
  it('re-reads and re-renders the drawer with the lent account and Reclaim', async () => {
    let lent = false
    const before = person('hal@example.com', ws('ready', 'w-4a1000'))
    const after = lentTo('hal@example.com', 'w-4a1000', [LENT_ENG])
    const calls = serve((m, url) => {
      if (m === 'GET' && url === '/v1/admin/people') return { status: 200, body: people([lent ? after : before]) }
      if (m === 'PUT' && url === '/v1/admin/people/w-4a1000/loan') {
        lent = true
        return { status: 200, body: { workspace_id: 'w-4a1000', account_id: 'acct-eng-1', owner_tenant: 'eng', lent: true, changed: true } }
      }
      return null
    })
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { PeopleScreen } = await import('../People')
    render(<PeopleScreen />)
    await waitFor(() => expect(rowOf('hal@example.com')).not.toBeNull(), WAIT)
    const drawer = await manage('hal@example.com')
    fireEvent.change(within(sectionOf(drawer)).getByLabelText('Account to lend to hal@example.com'), { target: { value: 'acct-eng-1' } })
    fireEvent.click(within(sectionOf(drawer)).getByRole('button', { name: 'Lend' }))
    const confirm = await screen.findByRole('dialog', { name: 'Lend this account' }, WAIT)
    fireEvent.click(within(confirm).getByRole('button', { name: 'Lend' }))
    await waitFor(() => expect(visible(sectionOf(screen.getByRole('dialog', { name: 'Manage hal@example.com' })))).toContain('Now using acct-eng-1 (eng shared), lent by Engineering'), WAIT)
    const section = sectionOf(screen.getByRole('dialog', { name: 'Manage hal@example.com' }))
    expect(section.querySelector('[data-testid="pp-loan-one"]')?.textContent).toContain('acct-eng-1')
    expect(within(section).getByRole('button', { name: 'Reclaim' })).toBeTruthy()
    expect(calls.filter((c) => c.method === 'GET' && c.url === '/v1/admin/people').length).toBeGreaterThan(1)
  })
})

describe('the account picker is a normal-sized control', () => {
  it('has no width 100% and no flex grow, at most 320px wide and one control high, wide and on a phone', async () => {
    await mount(people([BOB]))
    const section = sectionOf(await manage('bob@example.com'))
    const select = within(section).getByLabelText('Account to lend to bob@example.com')
    const field = select.closest('.c-field')!
    const loan = select.closest('.pp-loan')!
    for (const env of [WIDE, PHONE]) {
      // MUTATION: `.pp-loan select { width: 100% }` or `.pp-loan > .c-field { flex: 1 1 9rem }` back.
      expect(painted(select, 'width', env) ?? 'auto', `select width at ${env.width}`).toBe('auto')
      expect(painted(select, 'max-width', env), `select max-width at ${env.width}`).toBe('320px')
      // The console's control height (32px), the one Admin › Schedules' tenant select has.
      expect(painted(select, 'height', env), `select height at ${env.width}`).toBe('var(--h-ctl)')
      expect(painted(select, 'white-space', env), `select wraps at ${env.width}`).toBe('nowrap')
      for (const el of [field, loan]) {
        const flex = painted(el, 'flex', env) ?? '0 1 auto'
        expect(flex.split(/\s+/)[0], `${el.className} grows at ${env.width}`).toBe('0')
      }
      expect(painted(loan, 'justify-self', env), `.pp-loan stretches across the section at ${env.width}`).toBe('start')
    }
  })
})
