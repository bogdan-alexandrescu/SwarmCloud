// ADMIN-PEOPLE-VQ (part of #2391; visual QA VQ-051, VQ-052): Admin › People
// and roles against config-e.html S5c and S1b.
//
//   * a person's Claude account is ONE short line -- "7 accounts · lent by
//     Engineering" -- with the accounts behind a disclosure, not a
//     semicolon-joined paragraph that repeated the lender seven times and
//     made the row about 200px tall;
//   * the name reads whole: it does not shrink below its own width (up to
//     the 15rem cap), and the platform role left its cell for a column;
//   * the grid has S5c's Platform role column;
//   * Recent admin actions prints words, from one table with a fallback;
//   * Set default's "The default is already member" is a muted note, not a
//     ▲ warning: choosing the value already in effect is not a problem;
//   * the Admin panel's People and roles badge counts the join requests the
//     feed serves a workspace admin, and nothing it does not serve.
//
// Placeholders only; nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WAIT = { timeout: 4000 }
const WIDE: CascadeEnv = { width: 1440 }

type Json = Record<string, unknown>

const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString()

function ws(id: string): Json {
  return {
    state: 'ready',
    setup_url: '/setup#workspace',
    setup_command: '/sc:setup',
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
  }
}

function person(email: string, over: Json = {}): Json {
  return {
    email,
    display_name: null,
    teams: [],
    github: 'connected',
    workspace: ws(`w-${email.length.toString(16).padStart(6, '0')}`),
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

const lent = (n: number, owner: string, name: string) => ({
  account_id: `ai-${owner}-${n}`,
  label: `${owner} ${n}`,
  owner_tenant: owner,
  owner_name: name,
  state: 'AVAILABLE',
})

function borrower(email: string, accounts: Json[], over: Json = {}): Json {
  const owners = [...new Set(accounts.map((a) => a.owner_tenant as string))].sort()
  return person(email, {
    claude_account: { source: 'lent', own: 0, lent: accounts.length, lent_by: owners, lent_accounts: accounts, provider_key: false },
    ...over,
  })
}

const SEVEN = Array.from({ length: 7 }, (_, i) => lent(i + 1, 'eng', 'Engineering'))

const COLUMNS = [{ tenant_id: 'eng', group: 'eng@example.com', display_name: 'Engineering', default_role: 'member', unavailable: false }]

const ADMINS = [
  { email: 'owner@example.com', role: 'owner', granted_by: 'config:PLATFORM_OWNER', granted_at: null },
  { email: 'bob@example.com', role: 'admin', granted_by: 'owner@example.com', granted_at: ago(60) },
]

function platform(rows: Json[], over: Json = {}): Json {
  return {
    scope: 'platform',
    workspaces: COLUMNS,
    workspaces_error: null,
    people: rows,
    count: rows.length,
    pending: 0,
    audit: [],
    lendable_accounts: [],
    lendable_error: null,
    admins: ADMINS,
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
const claudeOf = (email: string) => rowOf(email).querySelector<HTMLElement>('td[data-label="Claude account"]')!
const heads = () => [...document.querySelectorAll<HTMLElement>('.pp-people thead th')].map((th) => th.textContent?.trim())

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('VQ-052: the Claude account cell is one short line', () => {
  it('counts seven loans from one workspace, the lender said once, the accounts folded', async () => {
    await mount(platform([borrower('bogdan@example.com', SEVEN, { display_name: 'Bogdan Alexandrescu' })]))
    const td = claudeOf('bogdan@example.com')
    const fold = td.querySelector<HTMLDetailsElement>('details.pp-loans')
    // MUTATION: join every loan with '; ' again -> no fold, and "lent by" seven times.
    expect(fold).not.toBeNull()
    expect(visible(fold!.querySelector('summary'))).toBe('7 accounts · lent by Engineering')
    expect(fold!.open).toBe(false)
    expect(visible(td).match(/lent by/g)).toHaveLength(1)
    expect(visible(td)).not.toContain(';')
    // Behind the disclosure, every account by its id and name.
    const items = [...fold!.querySelectorAll('li')].map((li) => visible(li))
    expect(items).toHaveLength(7)
    expect(items[0]).toBe('ai-eng-1 (eng 1)')
  })

  it('names each lender once in the line, and beside its own account in the fold', async () => {
    await mount(platform([borrower('hal@example.com', [lent(1, 'eng', 'Engineering'), lent(2, 'eng', 'Engineering'), lent(1, 'ops', 'Operations')])]))
    const fold = claudeOf('hal@example.com').querySelector('details.pp-loans')!
    expect(visible(fold.querySelector('summary'))).toBe('3 accounts · lent by Engineering, Operations')
    expect([...fold.querySelectorAll('li')].map((li) => visible(li))).toEqual([
      'ai-eng-1 (eng 1), lent by Engineering',
      'ai-eng-2 (eng 2), lent by Engineering',
      'ai-ops-1 (ops 1), lent by Operations',
    ])
  })

  it('keeps one loan as its one line, with nothing to unfold', async () => {
    await mount(platform([borrower('kay@example.com', [lent(1, 'eng', 'Engineering')])]))
    expect(visible(claudeOf('kay@example.com'))).toBe('ai-eng-1 (eng 1), lent by Engineering')
    expect(claudeOf('kay@example.com').querySelector('details')).toBeNull()
  })

  it('keeps every account in the Manage drawer, behind the same one line', async () => {
    await mount(platform([borrower('bogdan@example.com', SEVEN)]))
    fireEvent.click(within(rowOf('bogdan@example.com')).getByRole('button', { name: 'Manage' }))
    const drawer = await screen.findByRole('dialog', { name: 'Manage bogdan@example.com' }, WAIT)
    const items = [...drawer.querySelectorAll<HTMLElement>('[data-section="claude"] .pp-loan-list > li')].map((li) => visible(li))
    expect(items).toHaveLength(7)
    expect(items[6]).toBe('ai-eng-7 (eng 7)')
  })

  it('reads the name whole at 1440: the name over the address, the avatar on its line, the cell two lines', async () => {
    await mount(platform([borrower('bogdan@example.com', SEVEN, { display_name: 'Bogdan Alexandrescu' })]))
    const cell = rowOf('bogdan@example.com').querySelector('.pp-person')!
    const who = cell.querySelector('.pp-who')!
    const card = who.querySelector('.person-name')!
    // The cell holds the avatar, the who and the GitHub mark, in that order.
    expect([...cell.children].map((c) => c.className)).toEqual(['pp-avatar', 'pp-who', 'pp-gh'])
    // The name and the address are separate boxes; nothing else prints between them.
    expect(who.querySelector('.person-name-name')?.textContent).toBe('Bogdan Alexandrescu')
    expect(who.querySelector('.person-name-email')?.textContent).toBe('bogdan@example.com')
    // MUTATION: draw the name and the address on one line -> together they pass
    // the 15rem cap, the address does not shrink, and the name is cut.
    expect(painted(card, 'flex-direction', WIDE)).toBe('column')
    expect(painted(who.querySelector('.person-name-dot')!, 'display', WIDE)).toBe('none')
    // MUTATION: let the wide cell wrap -> the capped who falls under the
    // avatar and the GitHub mark under that: three lines, 89px against 56px.
    expect(painted(cell, 'flex-wrap', WIDE)).toBe('nowrap')
    expect(painted(cell, 'align-items', WIDE)).toBe('flex-start')
    // The who keeps its own width up to the cap; only an address past it is cut.
    expect(painted(who, 'flex-shrink', WIDE)).toBe('0')
    expect(painted(who, 'width', WIDE)).toBe('max-content')
    expect(painted(who, 'max-width', WIDE)).toBe('15rem')
  })

  it('spaces the wide Person cell at 4px, so the grid fits its 1009px pane at 1440 (render review of FIX2)', async () => {
    await mount(platform([borrower('bogdan@example.com', SEVEN, { display_name: 'Bogdan Alexandrescu' })]))
    const cell = rowOf('bogdan@example.com').querySelector('.pp-person')!
    // MUTATION: put back the 8px gap -> the nowrap cell (avatar, gap, who, gap,
    // GitHub mark) is 8px wider, and with the seven-loan row the table's least
    // width is 1010.8px in a 1009px pane: a 2px sideways scroll under the grid.
    // jsdom cannot lay out, so the fit itself is measured on the render.
    expect(painted(cell, 'gap', WIDE)).toBe('var(--ctl-s1)')
    expect(painted(cell, 'flex-wrap', WIDE)).toBe('nowrap')
  })
})

describe('VQ-052: the loan line wraps between words, never inside one', () => {
  const loans =
    '<details class="pp-loans"><summary class="pp-loan-sum"><span class="pp-nobr">7 accounts ·</span> lent by <span class="pp-nobr">Engineering</span></summary>' +
    '<ul class="pp-loan-list"><li><span class="pp-nobr">ai-eng-1</span> (eng 1)</li></ul></details>'

  it('lets the wide table wrap it, so the grid fits its pane at 1440 (rendered review of PR 2464)', () => {
    document.body.innerHTML = `<div class="ctl-table is-scroll"><table class="pp-table"><tbody><tr><td>${loans}</td></tr></tbody></table></div>`
    // MUTATION: put back the wide-table nowrap -> with the Platform role column
    // the grid is 1124px in a 1009px pane and Manage is behind its scroll.
    expect(painted(document.querySelector('.pp-loan-sum')!, 'white-space', WIDE)).toBeNull()
    expect(painted(document.querySelector('.pp-loan-list > li')!, 'white-space', WIDE)).toBeNull()
    for (const span of document.querySelectorAll('.pp-nobr')) expect(painted(span, 'white-space', WIDE)).toBe('nowrap')
  })

  it('wraps it the same way where a phone draws it, the Manage drawer, never inside the count, a lender or an id', () => {
    // A phone's grid has no Claude column: the drawer is the one place it is drawn.
    document.body.innerHTML = `<div class="pp-scrim"><aside class="pp-drawer"><section class="pp-dsec" data-section="claude"><div class="pp-now">Now using <div class="pp-now-what">${loans}</div></div></section></aside></div>`
    const PHONE: CascadeEnv = { width: 390 }
    expect(painted(document.querySelector('.pp-loan-sum')!, 'white-space', PHONE)).toBeNull()
    for (const span of document.querySelectorAll('.pp-nobr')) expect(painted(span, 'white-space', PHONE)).toBe('nowrap')
  })
})

describe('VQ-051: the grid has S5c\'s Platform role column', () => {
  it('draws Platform role after Person, and the role in it rather than beside the name', async () => {
    await mount(platform([person('bob@example.com'), person('owner@example.com'), person('devops@example.com')]))
    // MUTATION: keep the role as a chip in the person's cell -> no column.
    expect(heads()).toEqual(['Person', 'Platform role', 'Engineering', 'Own workspace', 'Claude account', 'Ceiling', 'Last active', 'Manage'])
    const role = (email: string) => rowOf(email).querySelector<HTMLElement>('td[data-label="Platform role"]')!
    expect(visible(role('bob@example.com'))).toBe('platform admin')
    expect(visible(role('owner@example.com'))).toBe('platform owner')
    expect(role('devops@example.com').querySelector('.c-dash')?.getAttribute('title')).toBe('No platform role')
    expect(visible(rowOf('bob@example.com').querySelector('th'))).not.toContain('platform admin')
  })

  it('says the platform roles were not read, rather than that nobody holds one', async () => {
    await mount(platform([person('bob@example.com')], { admins: null, admins_error: 'firestore_unavailable' }))
    const dash = rowOf('bob@example.com').querySelector('td[data-label="Platform role"] .c-dash')
    // MUTATION: draw an unread list as "No platform role" -> fails.
    expect(dash?.getAttribute('title')).toBe('The platform roles were not read')
  })
})

describe('VQ-052: Recent admin actions reads as words', () => {
  const entry = (action: string, i: number): Json => ({ action, target_workspace_id: 'w-3f9a2c', by: 'bob@example.com', at: ago(i), detail: {} })

  it('names each kind from the table, and an unknown kind in plain words', async () => {
    const audit = [
      ...Array.from({ length: 12 }, (_, i) => entry('qa_target_credential_set', i + 1)),
      ...Array.from({ length: 10 }, (_, i) => entry('repository_workflow_files_set', i + 20)),
      entry('approve', 40),
      entry('frobnicated_mcp_widget', 41),
    ]
    await mount(platform([person('bob@example.com')], { audit }))
    const card = document.querySelector<HTMLElement>('.pp-audit')!
    fireEvent.click(within(card).getByRole('button', { name: /Recent admin actions/ }))
    const lines = [...card.querySelectorAll<HTMLElement>('.pp-audit-list li')].map((li) => visible(li))
    // MUTATION: print `r.first.action` -> 'qa_target_credential_set × 12' and every line fails.
    expect(lines[0]).toContain('QA target credentials set × 12')
    expect(lines[1]).toContain('Repository workflow files set × 10')
    expect(lines[2]).toContain('Workspace approved w-3f9a2c by bob@example.com')
    expect(lines[3]).toContain('Frobnicated MCP widget w-3f9a2c')
    // The recorded kind stays findable, in the name's title.
    expect(card.querySelector('.pp-audit-list li b')?.getAttribute('title')).toBe('qa_target_credential_set')
  })
})

describe('VQ-052: Set default says a no-op quietly', () => {
  it('draws no ▲ beside "The default is already …" in Workspace roles', () => {
    document.body.innerHTML =
      '<div class="wra-card"><form class="wra-form"><span class="c-btn-wrap"><button class="c-btn is-sm" disabled>Set default</button>' +
      '<span class="c-why"><svg></svg>The default is already member</span></span></form></div>'
    const why = document.querySelector('.c-why')!
    // MUTATION: drop the rule -> the shared warning glyph is drawn.
    expect(painted(why.querySelector('svg')!, 'display', WIDE)).toBe('none')
    // The note itself stays, in the muted text colour.
    expect(painted(why, 'color', WIDE)).toBe('var(--text-dim)')
    // Anywhere else a disabled button's reason keeps its glyph.
    document.body.innerHTML = '<span class="c-btn-wrap"><span class="c-why"><svg></svg>Not yet</span></span>'
    expect(painted(document.querySelector('.c-why svg')!, 'display', WIDE)).toBeNull()
  })
})
