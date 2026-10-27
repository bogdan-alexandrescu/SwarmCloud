// ADMIN-PEOPLE-FIX2 (part of #2391): the render review of PR 2464 (Admin ›
// People and roles, task_7103d39876044a5d93f9) found two majors and three
// minors. Each is held here:
//
//   * the Manage drawer's heading read "…" at 390 for a long name: an
//     inline-flex PersonName in a nowrap, ellipsis h2 beside the 1px hidden
//     "Manage " is atomic, so 1px of overflow elided all of it;
//   * the drawer's Claude account was the semicolon paragraph, "lent by
//     Engineering" seven times: it is the grid's one line now;
//   * an account id broke at its hyphen ("ai-" over "eng-4") where a phone
//     draws it, the drawer;
//   * Recent admin actions read "7d ago to 7d ago";
//   * the fixture build serves what the review needed a scratch copy for.
//
// The grid's 1440 fit is held in admin.people.vq052.test.tsx, beside the rule
// it replaced. Placeholders only; nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WAIT = { timeout: 4000 }
const PHONE: CascadeEnv = { width: 390 }

type Json = Record<string, unknown>

const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString()

function person(email: string, over: Json = {}): Json {
  return {
    email,
    display_name: null,
    teams: [],
    github: 'connected',
    workspace: {
      state: 'ready',
      setup_url: '/setup#workspace',
      setup_command: '/sc:setup',
      workspace_id: 'w-0a1b2c',
      request_id: 'req-w-0a1b2c',
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
    },
    claude_account: { source: 'none', own: 0, lent: 0, lent_by: [], provider_key: false },
    loan_request: null,
    first_seen: ago(600),
    last_seen: ago(1),
    last_submitted: null,
    last_active: ago(1),
    roles: { eng: 'member' },
    ...over,
  }
}

const lent = (n: number) => ({ account_id: `ai-eng-${n}`, label: `ai-eng-${n}`, owner_tenant: 'eng', owner_name: 'Engineering', state: 'AVAILABLE' })

function borrower(email: string, n: number, over: Json = {}): Json {
  const accounts = Array.from({ length: n }, (_, i) => lent(i + 1))
  return person(email, { claude_account: { source: 'lent', own: 0, lent: n, lent_by: ['eng'], lent_accounts: accounts, provider_key: false }, ...over })
}

function platform(rows: Json[], over: Json = {}): Json {
  return {
    scope: 'platform',
    workspaces: [{ tenant_id: 'eng', group: 'eng@example.com', display_name: 'Engineering', default_role: 'member', unavailable: false }],
    workspaces_error: null,
    people: rows,
    count: rows.length,
    pending: 0,
    audit: [],
    lendable_accounts: [],
    lendable_error: null,
    admins: [],
    admins_error: null,
    ...over,
  }
}

async function mount(body: Json) {
  serve((m, url) => (m === 'GET' && url === '/v1/admin/people' ? { status: 200, body } : null))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { PeopleScreen } = await import('../People')
  render(<PeopleScreen />)
  await screen.findByRole('heading', { name: 'People and roles', level: 1 }, WAIT)
  await waitFor(() => expect(document.querySelector('.pp-people tbody tr')).not.toBeNull(), WAIT)
}

const rowOf = (email: string) => document.querySelector<HTMLElement>(`.pp-people tr[data-email="${email}"]`)!

async function manage(email: string): Promise<HTMLElement> {
  fireEvent.click(within(rowOf(email)).getByRole('button', { name: 'Manage' }))
  await waitFor(() => expect(document.querySelector('.pp-drawer')).not.toBeNull(), WAIT)
  return document.querySelector<HTMLElement>('.pp-drawer')!
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  document.cookie = 'swarm_qa_persona=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/'
})

describe('the Manage drawer heading reads the name at 390', () => {
  it('lays the heading out as a row whose name box shrinks and wraps, instead of one atomic line cut to "…"', async () => {
    await mount(platform([borrower('bogdan@example.com', 7, { display_name: 'Bogdan Alexandrescu' })]))
    const drawer = await manage('bogdan@example.com')
    const h2 = drawer.querySelector<HTMLElement>('.pp-drawer-h h2')!
    const box = h2.querySelector<HTMLElement>('.person-name')!
    expect(box.querySelector('.person-name-name')?.textContent).toBe('Bogdan Alexandrescu')
    // MUTATION: put back `white-space: nowrap; text-overflow: ellipsis` on a
    // block h2 -> the 1px "Manage " plus the 100%-wide box overflow by 1px and
    // the atomic box is elided whole.
    expect(painted(h2, 'display', PHONE)).toBe('flex')
    expect(painted(h2, 'text-overflow', PHONE)).toBeNull()
    expect(painted(h2, 'white-space', PHONE)).toBeNull()
    // MUTATION: leave the box one line -> PersonName shrinks the name first and cuts it.
    expect(painted(box, 'flex-wrap', PHONE)).toBe('wrap')
    // Only the address may be cut, with an ellipsis, on a line of its own.
    const email = box.querySelector<HTMLElement>('.person-name-email')!
    expect(painted(email, 'max-width', PHONE)).toBe('100%')
    expect(painted(email, 'overflow', PHONE)).toBe('hidden')
    expect(painted(email, 'text-overflow', PHONE)).toBe('ellipsis')
  })

  it('keeps a short heading as it was: Carol · carol@example.com', async () => {
    await mount(platform([person('carol@example.com', { display_name: 'Carol' })]))
    const drawer = await manage('carol@example.com')
    expect(visible(drawer.querySelector('.pp-drawer-h h2 .person-name'))).toBe('Carol · carol@example.com')
  })
})

describe("the Manage drawer's Claude account is the grid's one line", () => {
  it('says seven loans from one lender as a count and the lender once, the accounts folded', async () => {
    await mount(platform([borrower('bogdan@example.com', 7)]))
    const section = (await manage('bogdan@example.com')).querySelector<HTMLElement>('[data-section="claude"]')!
    // MUTATION: put back `Now using {claudeText(...)}` -> "lent by Engineering" seven times.
    expect(visible(section.querySelector('.pp-now .pp-loan-sum'))).toBe('7 accounts · lent by Engineering')
    expect(visible(section).match(/lent by Engineering/g)).toHaveLength(1)
    expect(section.querySelector('.pp-now details')?.hasAttribute('open')).toBe(false)
    // A disclosure is not phrasing content: it sits in no <p>.
    expect(section.querySelector('p details')).toBeNull()
  })

  it('keeps one loan as its sentence', async () => {
    await mount(platform([borrower('kay@example.com', 1)]))
    const section = (await manage('kay@example.com')).querySelector<HTMLElement>('[data-section="claude"]')!
    expect(visible(section.querySelector('.pp-now'))).toBe('Now using ai-eng-1, lent by Engineering')
  })
})

describe('an account id is one token where a phone draws it', () => {
  it('holds ai-eng-4 whole in the drawer, in the summary\'s fold and in a single loan\'s sentence', async () => {
    await mount(platform([borrower('bogdan@example.com', 7), borrower('kay@example.com', 1)]))
    let drawer = await manage('bogdan@example.com')
    const id = [...drawer.querySelectorAll<HTMLElement>('.pp-loan-list .pp-nobr')].find((s) => s.textContent === 'ai-eng-4')
    // MUTATION: print the id as bare text -> "ai-" over "eng-4" at 390.
    expect(id).toBeDefined()
    expect(painted(id!, 'white-space', PHONE)).toBe('nowrap')
    fireEvent.click(within(drawer).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(document.querySelector('.pp-drawer')).toBeNull(), WAIT)
    drawer = await manage('kay@example.com')
    const one = [...drawer.querySelectorAll<HTMLElement>('.pp-now .pp-nobr')].map((s) => s.textContent)
    expect(one).toEqual(['ai-eng-1', 'Engineering'])
  })
})

describe('Recent admin actions says one age once', () => {
  const entry = (minutes: number): Json => ({ action: 'qa_target_credential_set', target_workspace_id: 'eng', by: 'bob@example.com', at: ago(minutes), detail: {} })

  it('reads "7d ago", not "7d ago to 7d ago", when a run\'s ends round to the same age', async () => {
    const DAY = 24 * 60
    await mount(platform([person('bob@example.com')], { audit: [entry(7 * DAY), entry(7 * DAY + 30)] }))
    const card = document.querySelector<HTMLElement>('.pp-audit')!
    fireEvent.click(within(card).getByRole('button', { name: /Recent admin actions/ }))
    const line = visible(card.querySelector('.pp-audit-list li'))
    // MUTATION: compare the timestamps rather than what is printed -> "7d ago to 7d ago".
    expect(line).toMatch(/^7d ago · /)
    expect(line).not.toContain(' to ')
  })

  it('still says the span when the ends read differently', async () => {
    await mount(platform([person('bob@example.com')], { audit: [entry(5), entry(50)] }))
    const card = document.querySelector<HTMLElement>('.pp-audit')!
    fireEvent.click(within(card).getByRole('button', { name: /Recent admin actions/ }))
    expect(visible(card.querySelector('.pp-audit-list li'))).toMatch(/^5m ago to 50m ago · /)
  })
})

describe('the fixture build serves what the render review needed', () => {
  async function fixtureApi() {
    expect(import.meta.env.DEV, 'this reads the fixture build').toBeTruthy()
    expect(import.meta.env.VITE_LIVE, 'VITE_LIVE turns the fixtures off').toBeFalsy()
    document.cookie = 'swarm_qa_persona=platform-admin; path=/'
    vi.resetModules()
    return import('../api')
  }

  it('a person with seven accounts lent by Engineering, about 24 audit entries with an unknown kind, two platform owners', async () => {
    const api = await fixtureApi()
    const r = await api.loadPeople()
    expect(r.status).toBe('ok')
    const doc = (r as { data: import('../types').PeopleDoc }).data
    const seven = doc.people.find((p) => (p.claude_account.lent_accounts ?? []).length === 7)
    // MUTATION: drop the fixture person -> the seven-loan line is never rendered.
    expect(seven?.claude_account.lent_accounts?.every((a) => a.owner_name === 'Engineering')).toBe(true)
    expect(doc.count).toBe(doc.people.length)
    expect(doc.audit.length).toBe(24)
    expect(doc.audit.map((a) => a.action)).toContain('frobnicated_mcp_widget')
    expect(doc.admins?.filter((a) => a.role === 'owner')).toHaveLength(2)
  })

  it('a notification feed with one admin request pending', async () => {
    const api = await fixtureApi()
    const r = await api.loadNotifications()
    // MUTATION: keep the fixture's 404 -> the Admin badge is never drawn.
    expect(r.status).toBe('ok')
    expect((r as { data: import('../api').NotificationFeed }).data.admin_pending).toBe(1)
  })
})
