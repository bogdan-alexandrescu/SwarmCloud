// ADMIN › PEOPLE, PROPOSAL A (#1206, lane PEOPLE-A; owner picked A on
// 2026-10-12 17:40 PDT).
//
// Measured 2026-10-12 at 1260px: the seven-column table's Actions column took
// 653px, so `devops@example.com` wrapped as `devops@example. com`, every row crammed
// up to seven controls, and a pending request was one small green dot. A
// workspace request (w-7f9285) sat unseen from 15:31 PDT.
//
// WHAT EACH CASE HOLDS:
//   * the amber "Waiting for you" card lists each open request on top, a
//     workspace request with the limits it would get and Approve / Deny, a
//     loan request with the pool's lendable count, a picker, Lend and
//     Decline (an optional reason, LOAN-DECLINE); each
//     calls its existing route by WORKSPACE ID; the card is absent when
//     nothing is open;
//   * the table has no Actions column: Person, Teams, Workspace, Claude
//     account, Ceiling, Last active and a Manage button, a row with an open
//     request marked, and no column that breaks a word;
//   * Manage opens a drawer with Facts, Ceiling, Claude account, Admin and
//     Roles in workspaces (ROLES-UI-ADMIN);
//   * the drawer offers Lend OR Reclaim, never both, and says why when it
//     offers neither; the owner's Admin switch is locked and says why;
//   * the audit folds housekeeping (history_purged) into one line behind a
//     toggle, and says so when nothing else is in the latest 50.
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

// devops asked for a workspace 40 minutes ago; fay asked to borrow a Claude
// account 10 minutes ago; bob and owner have nothing open.
const DEVOPS = person('devops@example.com', ws('requested', 'w-7f9285', { requested_at: ago(40) }))
const FAY = person('fay@example.com', ws('ready', 'w-fa1000'), { loan_request: { state: 'requested', requested_at: ago(10) } })
const BOB = person('bob@example.com', ws('ready', 'w-91c0de'), { claude_account: { source: 'own', own: 2, lent: 0, lent_by: [], provider_key: false } })
const OWNER = person('owner@example.com', ws('ready', 'w-0e1000'), { github: 'none' })
const ROWS = [BOB, DEVOPS, FAY, OWNER]

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
const reqOf = (kind: string, email: string) => document.querySelector<HTMLElement>(`.pp-waiting li[data-kind="${kind}"][data-email="${email}"]`)!
const writesOf = (calls: { method: string; url: string; body: unknown }[]) => calls.filter((c) => c.method !== 'GET')

async function manage(email: string): Promise<HTMLElement> {
  fireEvent.click(within(rowOf(email)).getByRole('button', { name: 'Manage' }))
  return screen.findByRole('dialog', { name: `Manage ${email}` }, WAIT)
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

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('the page says what it is for', () => {
  it('in one plain line', async () => {
    await mount(people(ROWS))
    expect(document.querySelector('.pp-page .ur-sub')?.textContent).toBe('Every person against every workspace, what they are waiting for, and who is an admin.')
  })
})

describe('Waiting for you', () => {
  it('lists each open request on top, oldest first, with the count and the oldest age', async () => {
    // MUTATION: drop the loan requests from the card, or render it under the table.
    await mount(people(ROWS))
    const card = document.querySelector<HTMLElement>('.pp-waiting')!
    expect(card, 'no Waiting for you card').not.toBeNull()
    expect(within(card).getByRole('heading', { name: 'Waiting for you' })).toBeTruthy()
    const items = [...card.querySelectorAll<HTMLElement>('li.pp-req')]
    expect(items.map((li) => `${li.dataset.kind}:${li.dataset.email}`)).toEqual(['workspace:devops@example.com', 'loan:fay@example.com'])
    expect(visible(card)).toContain('2 waiting · oldest 40m ago')
    const table = document.querySelector('.pp-people')!
    expect(card.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING, 'the card is not above the table').toBeTruthy()
  })

  it('a workspace request shows the id, the time and the limits it would get, and Approve posts on one click', async () => {
    const { calls } = await mount(people(ROWS), {
      'POST /v1/admin/workspaces/w-7f9285/approve': () => ({ status: 200, body: { workspace: ws('approved', 'w-7f9285'), dispatch: { published: true, mode: 'create', reason: null } } }),
    })
    const item = reqOf('workspace', 'devops@example.com')
    expect(visible(item)).toContain('w-7f9285')
    expect(visible(item)).toContain('40m ago')
    const chips = [...item.querySelectorAll<HTMLElement>('.pp-limits .c-chip')].map((c) => c.textContent)
    expect(chips).toEqual(['8 agents at once', '64 vCPU', '16 pods', '8 capacity units'])
    // MUTATION: route Approve through a confirmation dialog again.
    fireEvent.click(within(item).getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(writesOf(calls)).toHaveLength(1), WAIT)
    expect(writesOf(calls)[0]).toMatchObject({ method: 'POST', url: '/v1/admin/workspaces/w-7f9285/approve' })
    for (const c of writesOf(calls)) expect(c.url, 'an email reached a workspace route').not.toContain('@')
  })

  it('Deny asks for the reason shown to the person, and posts {reason}', async () => {
    const { calls } = await mount(people(ROWS), {
      'POST /v1/admin/workspaces/w-7f9285/deny': () => ({ status: 200, body: { workspace: ws('denied', 'w-7f9285') } }),
    })
    const item = reqOf('workspace', 'devops@example.com')
    fireEvent.click(within(item).getByRole('button', { name: 'Deny' }))
    const send = within(item).getAllByRole('button', { name: 'Deny' }).at(-1)! as HTMLButtonElement
    expect(send.disabled, 'a blank reason may be sent').toBe(true)
    fireEvent.change(within(item).getByRole('textbox'), { target: { value: 'Use the eng team space.' } })
    fireEvent.click(within(item).getAllByRole('button', { name: 'Deny' }).at(-1)!)
    await waitFor(() => expect(writesOf(calls)).toHaveLength(1), WAIT)
    expect(writesOf(calls)[0]).toEqual({ method: 'POST', url: '/v1/admin/workspaces/w-7f9285/deny', body: { reason: 'Use the eng team space.' } })
  })

  it('a loan request names the pool, picks an account and lends it through the loan route', async () => {
    const { calls } = await mount(people(ROWS), {
      'PUT /v1/admin/people/w-fa1000/loan': () => ({ status: 200, body: { workspace_id: 'w-fa1000', account_id: 'acct-ops-1', owner_tenant: 'ops', lent: true, changed: true } }),
    })
    const item = reqOf('loan', 'fay@example.com')
    expect(visible(item)).toContain('10m ago')
    expect(visible(item)).toContain('2 lendable accounts in the pool')
    const lend = within(item).getByRole('button', { name: 'Lend' }) as HTMLButtonElement
    expect(lend.disabled, 'Lend is live with no account chosen').toBe(true)
    fireEvent.change(within(item).getByLabelText('Account to lend to fay@example.com'), { target: { value: 'acct-ops-1' } })
    fireEvent.click(lend)
    // Lending hands one tenant's account to another person (live QA ADM-1): it still asks.
    const dialog = await screen.findByRole('dialog', { name: 'Lend this account' }, WAIT)
    expect(writesOf(calls)).toHaveLength(0)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Lend' }))
    await waitFor(() => expect(writesOf(calls)).toHaveLength(1), WAIT)
    expect(writesOf(calls)[0]).toEqual({ method: 'PUT', url: '/v1/admin/people/w-fa1000/loan', body: { account_id: 'acct-ops-1', lend: true } })
  })

  it('Decline asks for an optional reason, posts it to the decline route, and the request leaves the card', async () => {
    // LOAN-DECLINE: the route exists now, so the card draws a real button.
    // MUTATION: post without the reason, or keep the request on the card after the read.
    const body = people(ROWS)
    const { calls } = await mount(body, {
      'POST /v1/admin/people/w-fa1000/loan/decline': (b) => {
        const declined = { state: 'declined', requested_at: ago(10), decided_at: ago(0), reason: (b as Json).reason ?? null }
        body.people = (body.people as Json[]).map((r) => (r.email === 'fay@example.com' ? { ...r, loan_request: declined } : r))
        return { status: 200, body: { workspace_id: 'w-fa1000', state: 'declined', reason: (b as Json).reason ?? null } }
      },
    })
    const item = reqOf('loan', 'fay@example.com')
    expect(item.querySelector('[data-testid="pp-no-decline"]'), 'the "no route" line is still drawn').toBeNull()
    fireEvent.click(within(item).getByRole('button', { name: 'Decline' }))
    // A small confirm: nothing is sent until it is confirmed.
    expect(writesOf(calls)).toHaveLength(0)
    const send = within(item).getAllByRole('button', { name: 'Decline' }).at(-1)! as HTMLButtonElement
    expect(send.disabled, 'a reason is optional, so Decline is live without one').toBe(false)
    fireEvent.change(within(item).getByLabelText('Reason, shown to fay@example.com (optional)'), { target: { value: 'Use the eng account.' } })
    fireEvent.click(within(item).getAllByRole('button', { name: 'Decline' }).at(-1)!)
    await waitFor(() => expect(writesOf(calls)).toHaveLength(1), WAIT)
    expect(writesOf(calls)[0]).toEqual({ method: 'POST', url: '/v1/admin/people/w-fa1000/loan/decline', body: { reason: 'Use the eng account.' } })
    await waitFor(() => expect(document.querySelector('.pp-waiting li[data-kind="loan"]')).toBeNull(), WAIT)
    expect(visible(document.querySelector('.pp-waiting'))).toContain('Declined fay@example.com')
    // The person's row says so until they ask again.
    expect(visible(rowOf('fay@example.com'))).toContain('loan declined')
  })

  it('Decline with no reason posts an empty body', async () => {
    const { calls } = await mount(people(ROWS), {
      'POST /v1/admin/people/w-fa1000/loan/decline': () => ({ status: 200, body: { workspace_id: 'w-fa1000', state: 'declined', reason: null } }),
    })
    const item = reqOf('loan', 'fay@example.com')
    fireEvent.click(within(item).getByRole('button', { name: 'Decline' }))
    fireEvent.click(within(item).getAllByRole('button', { name: 'Decline' }).at(-1)!)
    await waitFor(() => expect(writesOf(calls)).toHaveLength(1), WAIT)
    expect(writesOf(calls)[0]).toEqual({ method: 'POST', url: '/v1/admin/people/w-fa1000/loan/decline', body: {} })
  })

  it('is not drawn when nothing is waiting, and a held request is not waiting on an admin', async () => {
    // MUTATION: draw the card with an empty list.
    const held = person('gus@example.com', ws('requested', 'w-9a5000', { held: { reason: 'migrating', copy: 'x' } }))
    await mount(people([BOB, OWNER, held]))
    expect(document.querySelector('.pp-waiting')).toBeNull()
  })

  it('is amber and stacks on a phone', async () => {
    await mount(people(ROWS))
    const card = document.querySelector<HTMLElement>('.pp-waiting')!
    expect(painted(card, 'border-color', WIDE) ?? painted(card, 'border-left-color', WIDE)).toContain('var(--warn')
    const item = reqOf('workspace', 'devops@example.com')
    expect(painted(item, 'display', WIDE)).toBe('grid')
    expect(painted(item, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
  })
})

describe('the lean table', () => {
  it('has no Actions column: Person, Teams, Workspace, Claude account, Ceiling, Last active, and Manage', async () => {
    // MUTATION: put an Approve, Deny, Lend, select or ceiling field back in a row.
    await mount(people(ROWS))
    const heads = [...document.querySelectorAll<HTMLElement>('.pp-people thead th')].map((th) => th.textContent?.trim())
    expect(heads).toEqual(['Person', 'Teams', 'Workspace', 'Claude account', 'Ceiling', 'Last active', 'Manage'])
    for (const tr of document.querySelectorAll<HTMLElement>('.pp-people tbody tr')) {
      expect(within(tr).getAllByRole('button').map((b) => b.textContent)).toEqual(['Manage'])
      expect(tr.querySelector('select, input, textarea')).toBeNull()
    }
  })

  it('draws the person as an initial, the email, a GitHub icon when connected and the owner chip', async () => {
    await mount(people(ROWS))
    const bob = rowOf('bob@example.com')
    expect(bob.querySelector('.pp-avatar')?.textContent).toBe('B')
    expect(within(bob).getByRole('img', { name: 'GitHub connected' })).toBeTruthy()
    expect(bob.querySelector('.pp-owner')).toBeNull()
    const owner = rowOf('owner@example.com')
    expect(within(owner).queryByRole('img', { name: /GitHub/ })).toBeNull()
    expect(owner.querySelector('.pp-owner')?.textContent).toBe('platform owner')
    // No "connected" word left to wrap as "connecte d".
    expect(visible(bob)).not.toContain('connected')
  })

  it('shows the ceiling, the workspace id and state, and a borrow request in the Claude account', async () => {
    await mount(people(ROWS))
    expect(rowOf('bob@example.com').querySelector('td[data-label="Ceiling"]')?.textContent).toBe('8')
    expect(visible(rowOf('bob@example.com'))).toContain('w-91c0de')
    expect(visible(rowOf('fay@example.com').querySelector('td[data-label="Claude account"]'))).toContain('borrow requested')
  })

  it('marks a row with an open request with the amber edge, and no other', async () => {
    await mount(people(ROWS))
    expect(rowOf('devops@example.com').classList.contains('is-waiting')).toBe(true)
    expect(rowOf('fay@example.com').classList.contains('is-waiting')).toBe(true)
    expect(rowOf('bob@example.com').classList.contains('is-waiting')).toBe(false)
    const edge = painted(rowOf('devops@example.com').querySelector('th')!, 'box-shadow', WIDE)
    expect(edge).toContain('var(--warn')
  })

  it('wraps no column mid-word: an address is one line, cut with an ellipsis, never broken', async () => {
    // MUTATION: `.pp-who` back to `overflow-wrap: anywhere` (devops@example. com).
    await mount(people(ROWS))
    const who = rowOf('devops@example.com').querySelector<HTMLElement>('.pp-who')!
    expect(who.getAttribute('title')).toBe('devops@example.com')
    for (const env of [WIDE, PHONE]) {
      expect(painted(who, 'white-space', env)).toBe('nowrap')
      expect(painted(who, 'overflow-wrap', env) ?? 'normal').toBe('normal')
      expect(painted(who, 'text-overflow', env)).toBe('ellipsis')
    }
    for (const cell of document.querySelectorAll<HTMLElement>('.pp-people tbody td, .pp-people tbody th')) {
      expect(painted(cell, 'overflow-wrap', WIDE) ?? 'normal', `${cell.dataset.label ?? 'Person'} breaks words`).toBe('normal')
      expect(painted(cell, 'word-break', WIDE) ?? 'normal').toBe('normal')
    }
  })

  it('is one row per person on a phone, and Manage opens the drawer as a full sheet', async () => {
    media(true)
    await mount(people(ROWS))
    const wrap = document.querySelector<HTMLElement>('.pp-people table')!.parentElement!
    expect(wrap.classList.contains('is-stacked')).toBe(true)
    const drawer = await manage('bob@example.com')
    expect(painted(drawer, 'width', PHONE)).toBe('100%')
    expect(painted(drawer, 'max-width', PHONE) ?? 'none').toBe('none')
  })
})

describe('the Manage drawer', () => {
  it('holds Facts, Ceiling, Claude account and Admin, and closes on Escape', async () => {
    await mount(people(ROWS))
    const drawer = await manage('bob@example.com')
    const heads = [...drawer.querySelectorAll('h3')].map((h) => h.textContent)
    // ROLES-UI-ADMIN added "Roles in workspaces" (workspace-roles.md §4.8).
    expect(heads).toEqual(['Facts', 'Ceiling', 'Claude account', 'Admin', 'Roles in workspaces'])
    expect(visible(drawer)).toContain('w-91c0de')
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Manage bob@example.com' })).toBeNull(), WAIT)
  })

  it('steps the ceiling and saves {max_active} after the old → new confirm', async () => {
    const { calls } = await mount(people(ROWS), {
      'PUT /v1/admin/workspaces/w-91c0de/limits': (b) => ({
        status: 200,
        body: { workspace: ws('ready', 'w-91c0de', { limits: { max_active: (b as Json).max_active } }), tenant_written: true, dispatch: { published: true, mode: 'limits', reason: null } },
      }),
    })
    const drawer = await manage('bob@example.com')
    const save = within(drawer).getByRole('button', { name: 'Save' }) as HTMLButtonElement
    expect(save.disabled).toBe(true)
    fireEvent.click(within(drawer).getByRole('button', { name: 'One more' }))
    fireEvent.click(within(drawer).getByRole('button', { name: 'One more' }))
    expect((within(drawer).getByLabelText('Ceiling for bob@example.com') as HTMLInputElement).value).toBe('10')
    fireEvent.click(save)
    const confirm = await screen.findByRole('dialog', { name: 'Change this ceiling' }, WAIT)
    expect(writesOf(calls)).toHaveLength(0)
    fireEvent.click(within(confirm).getByRole('button', { name: 'Set to 10' }))
    await waitFor(() => expect(writesOf(calls)).toHaveLength(1), WAIT)
    expect(writesOf(calls)[0]).toEqual({ method: 'PUT', url: '/v1/admin/workspaces/w-91c0de/limits', body: { max_active: 10 } })
  })

  it('offers Lend from the pool and no Reclaim when nothing is lent to them', async () => {
    // MUTATION: draw both buttons, one disabled.
    await mount(people(ROWS))
    const drawer = await manage('bob@example.com')
    const section = drawer.querySelector<HTMLElement>('[data-section="claude"]')!
    expect(visible(section)).toContain('Now using own (2)')
    expect(within(section).getByRole('button', { name: 'Lend' })).toBeTruthy()
    expect(within(section).queryByRole('button', { name: 'Reclaim' })).toBeNull()
  })

  it('offers Reclaim and no Lend when an account is lent to them, and reclaims it', async () => {
    const lent = person('hal@example.com', ws('ready', 'w-4a1000'), { claude_account: { source: 'lent', own: 0, lent: 1, lent_by: ['eng'], provider_key: false } })
    const { calls } = await mount(people([lent]), {
      'PUT /v1/admin/people/w-4a1000/loan': () => ({ status: 200, body: { workspace_id: 'w-4a1000', account_id: 'acct-eng-1', owner_tenant: 'eng', lent: false, changed: true } }),
    })
    const drawer = await manage('hal@example.com')
    const section = drawer.querySelector<HTMLElement>('[data-section="claude"]')!
    expect(within(section).queryByRole('button', { name: 'Lend' })).toBeNull()
    const reclaim = within(section).getByRole('button', { name: 'Reclaim' }) as HTMLButtonElement
    expect(reclaim.disabled, 'the one lent account is not preselected').toBe(false)
    fireEvent.click(reclaim)
    const confirm = await screen.findByRole('dialog', { name: 'Reclaim this account' }, WAIT)
    expect(visible(confirm)).toContain('Reclaim eng shared (eng) from hal@example.com?')
    fireEvent.click(within(confirm).getByRole('button', { name: 'Reclaim' }))
    await waitFor(() => expect(writesOf(calls)).toHaveLength(1), WAIT)
    expect(writesOf(calls)[0]!.body).toEqual({ account_id: 'acct-eng-1', lend: false })
  })

  it('offers neither when the pool is empty, and says why in one line', async () => {
    await mount(people(ROWS, { lendable_accounts: [] }))
    const drawer = await manage('bob@example.com')
    const section = drawer.querySelector<HTMLElement>('[data-section="claude"]')!
    expect(within(section).queryByRole('button', { name: /Lend|Reclaim/ })).toBeNull()
    expect(section.querySelector('[data-testid="pp-loan-why"]')?.textContent).toMatch(/no accounts in the pool/i)
  })

  it("locks the owner's Admin switch and says why; another admin's switch removes after a confirm", async () => {
    const { calls } = await mount(people(ROWS), {
      'DELETE /v1/admin/admins/bob%40example.com': () => ({ status: 200, body: { email: 'bob@example.com', removed: true } }),
    })
    let drawer = await manage('owner@example.com')
    let sw = within(drawer).getByRole('switch', { name: 'Admin' }) as HTMLButtonElement
    expect(sw.getAttribute('aria-checked')).toBe('true')
    expect(sw.disabled).toBe(true)
    expect(visible(drawer)).toContain('Platform owner: set in the deployment’s configuration')
    fireEvent.click(within(drawer).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), WAIT)
    drawer = await manage('bob@example.com')
    sw = within(drawer).getByRole('switch', { name: 'Admin' }) as HTMLButtonElement
    expect(sw.getAttribute('aria-checked')).toBe('true')
    fireEvent.click(sw)
    const confirm = await screen.findByRole('dialog', { name: 'Remove admin' }, WAIT)
    expect(writesOf(calls)).toHaveLength(0)
    fireEvent.click(within(confirm).getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(writesOf(calls)).toHaveLength(1), WAIT)
    expect(writesOf(calls)[0]).toMatchObject({ method: 'DELETE', url: '/v1/admin/admins/bob%40example.com' })
  })
})

describe('Admins and Recent admin actions', () => {
  const purged = (i: number) => ({
    action: 'history_purged',
    target_kind: 'task',
    target_id: `task_${String(i).padStart(4, '0')}`,
    by: 'bob@example.com',
    at: ago(i),
    detail: { tenant_id: 'smoke' },
  })

  it('sit side by side on a wide screen', async () => {
    await mount(people(ROWS))
    const side = document.querySelector<HTMLElement>('.pp-side')!
    expect(side.querySelector('.pp-admins')).not.toBeNull()
    expect(side.querySelector('.pp-audit')).not.toBeNull()
    expect(painted(side, 'display', WIDE)).toBe('grid')
    expect(painted(side, 'grid-template-columns', WIDE)).toBe('repeat(2, minmax(0, 1fr))')
  })

  it('folds housekeeping into one line behind a toggle', async () => {
    // MUTATION: list history_purged runs among the decisions.
    const audit = [purged(1), purged(2), { action: 'approve', target_workspace_id: 'w-3f9a2c', by: 'bob@example.com', at: ago(3), detail: {} }, purged(4)]
    await mount(people(ROWS, { audit }))
    const card = document.querySelector<HTMLElement>('.pp-audit')!
    // It opens folded (ADMIN-PEOPLE-FILTER, S5c).
    fireEvent.click(within(card).getByRole('button', { name: /Recent admin actions/ }))
    const lines = () => [...card.querySelectorAll<HTMLElement>('.pp-audit-list li')].map((li) => visible(li))
    expect(lines()).toHaveLength(1)
    expect(lines()[0]).toContain('Workspace approved w-3f9a2c by bob@example.com')
    expect(card.querySelector('[data-testid="pp-housekeeping"]')?.textContent).toContain('History purged × 3 housekeeping hidden')
    const toggle = within(card).getByRole('button', { name: 'Show housekeeping' })
    expect(toggle.getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(toggle)
    expect(lines().some((l) => l.includes('History purged'))).toBe(true)
    expect(within(card).getByRole('button', { name: 'Hide housekeeping' }).getAttribute('aria-pressed')).toBe('true')
  })

  it('says so when nothing but housekeeping is in the latest 50', async () => {
    await mount(people(ROWS, { audit: Array.from({ length: 50 }, (_, i) => purged(i)) }))
    const card = document.querySelector<HTMLElement>('.pp-audit')!
    fireEvent.click(within(card).getByRole('button', { name: /Recent admin actions/ }))
    expect(visible(card)).toContain('No approvals, denials, loans or admin grants in the latest 50 entries.')
    expect(card.querySelector('.pp-audit-list')).toBeNull()
  })
})
