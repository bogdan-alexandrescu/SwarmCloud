// ADMIN › PEOPLE (#847, lane W8; docs/workspaces.md §6.4, §6.5).
//
// WHAT EACH CASE HOLDS:
//   * the tab is in the Admin section, admin only, and routes to the screen;
//   * a non-admin's read (403, admin_auth's sentence) draws the admin gate
//     and no table, no button;
//   * `requested` rows sort first, the server's order otherwise;
//   * the Waiting card's Approve posts the approve route by WORKSPACE ID --
//     no email in any workspace route; the drawer's Approve of a denied
//     record asks once, in §6.4's words (#1206 moved both out of the row);
//   * Deny without a reason posts nothing; with one, posts `{reason}`;
//   * a failed person's drawer has Retry; a needs_owner row says it waits for
//     the platform owner and names the step the guard stopped at;
//   * a ceiling save (in the drawer) sends `{max_active}` and nothing else;
//   * Lend (in the drawer) posts `{account_id, lend: true}` for the account chosen;
//   * the API's refusal for the last admin is shown as it came;
//   * zero pending is prose, `none pending`, with "measured" in its accessible
//     name and no `real zero` mark (owner, 2026-10-09); an unread lendable list as
//     not read, never as an empty one;
//   * the layout seen live at 1456 on 2026-10-09: the table has its own scroll
//     container inside the card and, since #1206, no control in it at all; a
//     workspace id has no break opportunity; a label sits a token's gap from
//     its field; below 560px a short record per person;
//   * the Admins card lists who the admins are, the owner first and marked,
//     and Remove acts on a listed admin.
//
// The bodies are `people.People.everyone`'s shape
// (tests/unit/control_plane/test_people_admin.py). Placeholders only;
// nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { SECTIONS } from '../App'

const WAIT = { timeout: 4000 }

type Json = Record<string, unknown>

const now = () => new Date().toISOString()

function ws(state: string, id: string | null, over: Json = {}): Json {
  const base: Json = { state, setup_url: '/setup#workspace', setup_command: '/sc:setup' }
  if (id === null) return base
  return {
    ...base,
    workspace_id: id,
    request_id: `req-${id}`,
    requested_at: now(),
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
    first_seen: now(),
    last_seen: now(),
    last_submitted: null,
    last_active: now(),
    ...over,
  }
}

const ADMINS = [
  { email: 'owner@example.com', role: 'owner', granted_by: 'config:PLATFORM_OWNER', granted_at: null },
  { email: 'bob@example.com', role: 'admin', granted_by: 'owner@example.com', granted_at: new Date().toISOString() },
]

const ACCOUNT = { account_id: 'acct-eng-1', owner_tenant: 'eng', label: 'eng shared', state: 'AVAILABLE', lend_to: [] }

function people(rows: Json[], over: Json = {}): Json {
  return {
    people: rows,
    count: rows.length,
    pending: rows.filter((r) => (r.workspace as Json).state === 'requested').length,
    audit: [],
    lendable_accounts: [ACCOUNT],
    lendable_error: null,
    admins: ADMINS,
    admins_error: null,
    ...over,
  }
}

const ROWS = [
  person('bob@example.com', ws('ready', 'w-91c0de'), { claude_account: { source: 'own', own: 2, lent: 0, lent_by: [], provider_key: false } }),
  person('dan@example.com', ws('failed', 'w-d4n000', { failure: { code: 'NAMESPACE_APPLY_FAILED', step: 'A7', retryable: true, at: now(), copy: 'x' } })),
  person('alice@example.com', ws('requested', 'w-3f9a2c')),
  person('erin@example.com', ws('needs_owner', 'w-e41000', { needs_owner_step: 'A5', steps: { A1: { state: 'done' }, A5: { state: 'held' } } })),
  person('carol@example.com', ws('none', null), { teams: [] }),
]

async function mount(body: Json, writes: Record<string, (body: unknown) => { status: number; body: unknown }> = {}, status = 200) {
  const calls = serve((m, url, b) => {
    if (m === 'GET' && url === '/v1/admin/people') return { status, body }
    const key = `${m} ${url}`
    if (writes[key] !== undefined) return writes[key]!(b)
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { PeopleScreen } = await import('../People')
  const utils = render(<PeopleScreen />)
  await screen.findByRole('heading', { name: 'People and roles', level: 1 }, WAIT)
  return { ...utils, calls }
}

const rowOf = (email: string) => document.querySelector<HTMLElement>(`tr[data-email="${email}"]`)!
/** A request's row in the Waiting for you card (#1206). */
const reqOf = (kind: string, email: string) => document.querySelector<HTMLElement>(`.pp-waiting li[data-kind="${kind}"][data-email="${email}"]`)!
/** Open a person's Manage drawer (#1206), where the ceiling, loans, Retry and a denied row's Approve now live. */
async function manage(email: string): Promise<HTMLElement> {
  fireEvent.click(within(rowOf(email)).getByRole('button', { name: 'Manage' }))
  return screen.findByRole('dialog', { name: `Manage ${email}` }, WAIT)
}
const writesOf = (calls: { method: string; url: string; body: unknown }[]) => calls.filter((c) => c.method !== 'GET')
/** Recent admin actions opens folded (ADMIN-PEOPLE-FILTER, S5c): open it to read its entries. */
async function openAudit(): Promise<void> {
  fireEvent.click(await screen.findByRole('button', { name: /Recent admin actions/ }, WAIT))
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

const WIDE: CascadeEnv = { width: 1456 }
const PHONE: CascadeEnv = { width: 390 }

/** Stub `matchMedia`, which jsdom does not have, as a phone or a desktop (honesty.admin.test.tsx's). */
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

/** A CSS length in px at a 16px root, or null for one that is not a fixed length (auto, %, 0, none). */
function fixedPx(value: string | null): number | null {
  if (value === null) return null
  const m = /^(-?\d+(?:\.\d+)?)(px|rem|em|ch)$/.exec(value.trim())
  if (m === null) return null
  const n = Number(m[1])
  return m[2] === 'px' ? n : m[2] === 'ch' ? n * 8 : n * 16
}

describe('the tab', () => {
  it('is in the Admin section, admin only', () => {
    const admin = SECTIONS.find((s) => s.id === 'admin')!
    const tab = admin.tabs.find((t) => t.id === 'people')
    expect(tab).toEqual({ id: 'people', label: 'People and roles', admin: true })
  })

  // A tab and a route are not enough: the panel draws PANEL_PAGES, and a page
  // missing there is reachable only by typing its address (visual QA Q7).
  // Drawn for a platform admin on the live path: since NAV-ADMIN-ROLE (part of
  // #1633) the fixture caller, who administers nothing, is shown no Admin panel.
  it('is drawn in the Admin panel, and lit on /admin/people', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/v1/tenants/me') && !url.includes('/v1/tenants/mine')) {
        const me = { tenant: { tenant_id: 'eng', display_name: 'Engineering' }, principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: true }, environment: 'dev', environment_declared: true }
        return new Response(JSON.stringify(me), { status: 200, headers: { 'content-type': 'application/json' } })
      }
      return new Response('{}', { status: 503, headers: { 'content-type': 'application/json' } })
    }))
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { SkyShell: LiveShell } = await import('../Spine')
    render(
      <LiveShell section="admin" tab="people" title="People and roles" go={vi.fn()} foot={null}>
        {null}
      </LiveShell>,
    )
    await waitFor(() => expect(document.querySelectorAll('.sk-panel .sk-pk').length).toBeGreaterThan(0), WAIT)
    const labels = [...document.querySelectorAll('.sk-panel .sk-pk .sk-pl')].map((el) => el.textContent)
    expect(labels).toContain('People and roles')
    expect(document.querySelector('.sk-panel .sk-pk.is-on .sk-pl')?.textContent).toBe('People and roles')
  })
})

describe('a non-admin never sees the pane', () => {
  it('draws the admin gate, and no table or button', async () => {
    const { container } = await mount({ code: 'forbidden', message: 'admin group membership is required for this operation' }, {}, 403)
    await waitFor(() => expect(container.querySelector('.state.admin-gate')).not.toBeNull(), WAIT)
    expect(container.querySelector('table')).toBeNull()
    expect(within(container).queryByRole('button', { name: /Approve|Deny|Retry|Grant admin/ })).toBeNull()
  })
})

describe('the people table', () => {
  it('sorts requested rows first, keeping the server order for the rest', async () => {
    await mount(people(ROWS))
    await waitFor(() => expect(document.querySelectorAll('tbody tr')).toHaveLength(5), WAIT)
    const order = [...document.querySelectorAll<HTMLElement>('tbody tr')].map((tr) => tr.dataset.email)
    expect(order).toEqual(['alice@example.com', 'bob@example.com', 'dan@example.com', 'erin@example.com', 'carol@example.com'])
    expect(visible(rowOf('bob@example.com'))).toContain('own (2)')
    expect(visible(rowOf('bob@example.com'))).toContain('w-91c0de')
    // A row holds no action but Manage (#1206).
    expect(within(rowOf('carol@example.com')).getAllByRole('button').map((b) => b.textContent)).toEqual(['Manage'])
  })

  it('a failed row has Retry; a needs_owner row waits for the owner at its step', async () => {
    const { calls } = await mount(people(ROWS), {
      'POST /v1/admin/workspaces/w-d4n000/retry': () => ({ status: 200, body: { workspace: ws('approved', 'w-d4n000'), dispatch: { published: true, mode: 'create', reason: null } } }),
    })
    await waitFor(() => expect(rowOf('dan@example.com')).not.toBeNull(), WAIT)
    expect(visible(rowOf('dan@example.com'))).toContain('failed w-d4n000 (Namespace)')
    expect(visible(rowOf('erin@example.com'))).toContain('Waiting for the platform owner · stopped at Access')
    const erin = await manage('erin@example.com')
    expect(within(erin).queryByRole('button', { name: 'Retry' })).toBeNull()
    fireEvent.click(within(erin).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), WAIT)
    fireEvent.click(within(await manage('dan@example.com')).getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(writesOf(calls)).toHaveLength(1), WAIT)
    expect(writesOf(calls)[0]).toMatchObject({ method: 'POST', url: '/v1/admin/workspaces/w-d4n000/retry' })
  })

  it('a held (migrating) request offers no Approve or Deny, and an auto-approved row says so', async () => {
    const held = person('gus@example.com', ws('requested', 'w-9a5000', { held: { reason: 'migrating', copy: 'x' } }))
    const auto = person('hal@example.com', ws('approved', 'w-4a1000', { decision: { verdict: 'approved', reason: 'requester is an admin', at: now(), auto: true } }))
    await mount(people([held, auto]))
    await waitFor(() => expect(rowOf('gus@example.com')).not.toBeNull(), WAIT)
    expect(within(rowOf('gus@example.com')).queryByRole('button', { name: /Approve|Deny/ })).toBeNull()
    expect(document.querySelector('.pp-waiting'), 'a held request is waiting on an admin').toBeNull()
    expect(visible(rowOf('gus@example.com'))).toContain('Being migrated by the platform owner')
    expect(visible(rowOf('hal@example.com'))).toContain('approved automatically (admin)')
  })

  it('a denied row may still be approved, and is not offered Retry', async () => {
    const denied = person('fay@example.com', ws('denied', 'w-fa1000', { decision: { verdict: 'denied', reason: 'Use the eng space.', at: now() } }))
    const { calls } = await mount(people([denied]), {
      'POST /v1/admin/workspaces/w-fa1000/approve': () => ({ status: 200, body: { workspace: ws('approved', 'w-fa1000'), dispatch: { published: true, mode: 'create', reason: null } } }),
    })
    await waitFor(() => expect(rowOf('fay@example.com')).not.toBeNull(), WAIT)
    expect(visible(rowOf('fay@example.com'))).toContain('“Use the eng space.”')
    const drawer = await manage('fay@example.com')
    expect(within(drawer).queryByRole('button', { name: 'Retry' })).toBeNull()
    fireEvent.click(within(drawer).getByRole('button', { name: 'Approve' }))
    const dialog = await screen.findByRole('dialog', { name: 'Approve this workspace' }, WAIT)
    // Nothing beside the drawer's Approve says what it creates, so it asks in §6.4's words.
    expect(visible(dialog)).toContain('Create a workspace for fay@example.com with 8 agents? A CI job will create its identity and namespace.')
    expect(writesOf(calls), 'it posted before the confirmation').toHaveLength(0)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(writesOf(calls)).toHaveLength(1), WAIT)
    expect(writesOf(calls)[0]).toMatchObject({ method: 'POST', url: '/v1/admin/workspaces/w-fa1000/approve' })
  })

  it('writes zero pending as words, measured, not as the real-zero mark', async () => {
    await mount(people([ROWS[0]!]))
    await waitFor(() => expect(rowOf('bob@example.com')).not.toBeNull(), WAIT)
    const chip = document.querySelector<HTMLElement>('.pp-people')!
    expect(visible(chip)).toContain('1 person · none pending')
    expect(within(chip).getByLabelText('none pending (measured)')).toBeTruthy()
    expect(chip.querySelector('.ctl-mark.is-zero')).toBeNull()
  })

  it('draws an unread lendable list as not read, never as an empty one', async () => {
    await mount(people([ROWS[2]!], { lendable_accounts: null, lendable_error: 'upstream_unavailable' }))
    await waitFor(() => expect(rowOf('alice@example.com')).not.toBeNull(), WAIT)
    const section = (await manage('alice@example.com')).querySelector<HTMLElement>('[data-section="claude"]')!
    expect(section.querySelector('.ctl-mark.is-unread')).not.toBeNull()
    expect(visible(section)).toContain('upstream_unavailable')
    expect(section.querySelector('.ctl-mark.is-zero')).toBeNull()
    expect(within(section).queryByRole('button', { name: /Lend|Reclaim/ })).toBeNull()
  })
})

describe('approve and deny', () => {
  it('the card\'s Approve, beside the limits it gives, posts the approve route by workspace id', async () => {
    const { calls } = await mount(people(ROWS), {
      'POST /v1/admin/workspaces/w-3f9a2c/approve': () => ({ status: 200, body: { workspace: ws('approved', 'w-3f9a2c'), dispatch: { published: true, mode: 'create', reason: null } } }),
    })
    await waitFor(() => expect(reqOf('workspace', 'alice@example.com')).not.toBeNull(), WAIT)
    const item = reqOf('workspace', 'alice@example.com')
    expect(visible(item)).toContain('8 agents at once')
    fireEvent.click(within(item).getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(writesOf(calls)).toHaveLength(1), WAIT)
    expect(writesOf(calls)[0]).toMatchObject({ method: 'POST', url: '/v1/admin/workspaces/w-3f9a2c/approve' })
    for (const c of writesOf(calls)) expect(c.url, 'an email reached a workspace route').not.toContain('@')
  })

  it('Deny without a reason posts nothing; with one, posts {reason}', async () => {
    const { calls } = await mount(people(ROWS), {
      'POST /v1/admin/workspaces/w-3f9a2c/deny': () => ({ status: 200, body: { workspace: ws('denied', 'w-3f9a2c') } }),
    })
    await waitFor(() => expect(reqOf('workspace', 'alice@example.com')).not.toBeNull(), WAIT)
    const row = reqOf('workspace', 'alice@example.com')
    fireEvent.click(within(row).getByRole('button', { name: 'Deny' }))
    const send = within(row).getAllByRole('button', { name: 'Deny' }).at(-1)! as HTMLButtonElement
    expect(send.disabled).toBe(true)
    fireEvent.change(within(row).getByRole('textbox'), { target: { value: '   ' } })
    fireEvent.click(send)
    expect(writesOf(calls), 'a blank reason was sent').toHaveLength(0)
    fireEvent.change(within(row).getByRole('textbox'), { target: { value: 'Use the eng team space.' } })
    fireEvent.click(within(row).getAllByRole('button', { name: 'Deny' }).at(-1)!)
    await waitFor(() => expect(writesOf(calls)).toHaveLength(1), WAIT)
    expect(writesOf(calls)[0]).toEqual({ method: 'POST', url: '/v1/admin/workspaces/w-3f9a2c/deny', body: { reason: 'Use the eng team space.' } })
  })
})

describe('ceiling and loans', () => {
  it('a ceiling save sends {max_active} and nothing else', async () => {
    const { calls } = await mount(people(ROWS), {
      'PUT /v1/admin/workspaces/w-91c0de/limits': (b) => ({
        status: 200,
        body: { workspace: ws('ready', 'w-91c0de', { limits: { max_active: (b as Json).max_active } }), tenant_written: true, dispatch: { published: true, mode: 'limits', reason: null } },
      }),
    })
    await waitFor(() => expect(rowOf('bob@example.com')).not.toBeNull(), WAIT)
    const row = await manage('bob@example.com')
    const field = within(row).getByLabelText('Ceiling for bob@example.com') as HTMLInputElement
    expect(field.value).toBe('8')
    fireEvent.change(field, { target: { value: '12' } })
    fireEvent.click(within(row).getByRole('button', { name: 'Save' }))
    const dialog = await screen.findByRole('dialog', { name: 'Change this ceiling' }, WAIT)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Set to 12' }))
    await waitFor(() => expect(writesOf(calls)).toHaveLength(1), WAIT)
    expect(writesOf(calls)[0]).toEqual({ method: 'PUT', url: '/v1/admin/workspaces/w-91c0de/limits', body: { max_active: 12 } })
    await waitFor(() => expect(visible(row)).toContain('Ceiling 12 saved.'), WAIT)
  })

  it('Lend posts {account_id, lend: true} for the account chosen', async () => {
    const { calls } = await mount(people(ROWS), {
      'PUT /v1/admin/people/w-91c0de/loan': () => ({ status: 200, body: { workspace_id: 'w-91c0de', account_id: 'acct-eng-1', owner_tenant: 'eng', lent: true, changed: true } }),
    })
    await waitFor(() => expect(rowOf('bob@example.com')).not.toBeNull(), WAIT)
    const row = await manage('bob@example.com')
    const lend = within(row).getByRole('button', { name: 'Lend' }) as HTMLButtonElement
    expect(lend.disabled, 'Lend is live with no account chosen').toBe(true)
    fireEvent.change(within(row).getByLabelText('Account to lend to bob@example.com'), { target: { value: 'acct-eng-1' } })
    fireEvent.click(lend)
    const dialog = await screen.findByRole('dialog', { name: 'Lend this account' }, WAIT)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Lend' }))
    await waitFor(() => expect(writesOf(calls)).toHaveLength(1), WAIT)
    expect(writesOf(calls)[0]).toEqual({ method: 'PUT', url: '/v1/admin/people/w-91c0de/loan', body: { account_id: 'acct-eng-1', lend: true } })
    await waitFor(() => expect(visible(row)).toContain('Lent acct-eng-1.'), WAIT)
  })
})

// LIVE QA, ADMIN (2026-10-11): ADM-1 to ADM-5. Every write on this screen
// that hands out an account, takes one back, moves a ceiling or makes an
// admin asks first; nothing is sent on the first click.
describe('live QA admin: nothing writes on the first click', () => {
  const choose = (row: HTMLElement, id: string) =>
    fireEvent.change(within(row).getByLabelText('Account to lend to bob@example.com'), { target: { value: id } })

  it('ADM-1: Lend asks first, naming the account, its owner tenant and the person, and Cancel sends nothing', async () => {
    const { calls } = await mount(people(ROWS))
    await waitFor(() => expect(rowOf('bob@example.com')).not.toBeNull(), WAIT)
    const row = await manage('bob@example.com')
    choose(row, 'acct-eng-1')
    fireEvent.click(within(row).getByRole('button', { name: 'Lend' }))
    const dialog = await screen.findByRole('dialog', { name: 'Lend this account' }, WAIT)
    // MUTATION: call setWorkspaceLoan from the Lend button's onClick.
    expect(writesOf(calls)).toHaveLength(0)
    expect(visible(dialog)).toContain('Lend eng shared (eng) to bob@example.com?')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Lend this account' })).toBeNull(), WAIT)
    expect(writesOf(calls)).toHaveLength(0)
  })

  it('ADM-2: Reclaim offers only an account lent to this person, and reclaims it behind a confirm', async () => {
    const lentRow = person('bob@example.com', ws('ready', 'w-91c0de'), {
      claude_account: { source: 'lent', own: 0, lent: 1, lent_by: ['eng'], provider_key: false },
    })
    const other = { account_id: 'acct-ops-1', owner_tenant: 'ops', label: 'ops shared', state: 'AVAILABLE', lend_to: [] }
    const { calls } = await mount(people([lentRow], { lendable_accounts: [ACCOUNT, other] }), {
      'PUT /v1/admin/people/w-91c0de/loan': () => ({ status: 200, body: { workspace_id: 'w-91c0de', account_id: 'acct-eng-1', owner_tenant: 'eng', lent: false, changed: true } }),
    })
    await waitFor(() => expect(rowOf('bob@example.com')).not.toBeNull(), WAIT)
    const row = await manage('bob@example.com')
    const reclaim = () => within(row).getByRole('button', { name: 'Reclaim' }) as HTMLButtonElement
    // MUTATION: list every lendable account under Reclaim. One lent account
    // is said as text beside Reclaim, not an empty select (PEOPLE-LOAN-VIEW).
    expect(row.querySelector('select'), 'Reclaim offers an account ops never lent them').toBeNull()
    expect(row.querySelector('[data-testid="pp-loan-one"]')?.textContent).toContain('acct-eng-1 (eng shared)')
    expect(reclaim().disabled).toBe(false)
    fireEvent.click(reclaim())
    const dialog = await screen.findByRole('dialog', { name: 'Reclaim this account' }, WAIT)
    expect(writesOf(calls)).toHaveLength(0)
    expect(visible(dialog)).toContain('Reclaim eng shared (eng) from bob@example.com?')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Reclaim' }))
    await waitFor(() => expect(writesOf(calls)).toHaveLength(1), WAIT)
    expect(writesOf(calls)[0]!.body).toEqual({ account_id: 'acct-eng-1', lend: false })
  })

  it('ADM-2: a paused account is marked in the list and the lend confirm says it starts no new agent', async () => {
    const paused = { ...ACCOUNT, account_id: 'acct-eng-2', label: 'eng paused', state: 'PAUSED' }
    await mount(people(ROWS, { lendable_accounts: [ACCOUNT, paused] }))
    await waitFor(() => expect(rowOf('bob@example.com')).not.toBeNull(), WAIT)
    const row = await manage('bob@example.com')
    const option = within(row).getByRole('option', { name: /eng paused/ })
    expect(option.textContent).toContain('starts no new agent')
    choose(row, 'acct-eng-2')
    fireEvent.click(within(row).getByRole('button', { name: 'Lend' }))
    const dialog = await screen.findByRole('dialog', { name: 'Lend this account' }, WAIT)
    // In words, not the broker's enum (QA2-FIX-18, admin2-04).
    expect(within(dialog).getByTestId('pp-loan-idle').textContent).toContain('This account is paused:')
  })

  it('ADM-3: a ceiling Save asks first with old → new and the change, and an unchanged value has nothing to save', async () => {
    const { calls } = await mount(people(ROWS))
    await waitFor(() => expect(rowOf('bob@example.com')).not.toBeNull(), WAIT)
    const row = await manage('bob@example.com')
    const save = within(row).getByRole('button', { name: 'Save' }) as HTMLButtonElement
    expect(save.disabled, 'Save is live with the value it already holds').toBe(true)
    fireEvent.change(within(row).getByLabelText('Ceiling for bob@example.com'), { target: { value: '6' } })
    fireEvent.click(save)
    const dialog = await screen.findByRole('dialog', { name: 'Change this ceiling' }, WAIT)
    // MUTATION: call setWorkspaceCeiling from Save's onClick.
    expect(writesOf(calls)).toHaveLength(0)
    expect(within(dialog).getByTestId('pp-ceiling-change').textContent).toBe('8 → 6 · −25%')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Keep 8' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Change this ceiling' })).toBeNull(), WAIT)
    expect(writesOf(calls)).toHaveLength(0)
  })

  it('ADM-4: Grant admin refuses a non-email, submits on Enter, and asks before it grants', async () => {
    const { calls } = await mount(people(ROWS), {
      'PUT /v1/admin/admins/new%40example.com': () => ({ status: 200, body: { email: 'new@example.com', role: 'admin', changed: true } }),
    })
    await waitFor(() => expect(rowOf('bob@example.com')).not.toBeNull(), WAIT)
    const card = document.querySelector<HTMLElement>('.pp-admins')!
    const field = within(card).getByLabelText('Email') as HTMLInputElement
    const grant = within(card).getByRole('button', { name: 'Grant admin' }) as HTMLButtonElement
    fireEvent.change(field, { target: { value: 'not-an-email' } })
    // MUTATION: gate the button on a non-blank value only.
    expect(grant.disabled, 'Grant admin is live for not-an-email').toBe(true)
    fireEvent.submit(field.form!)
    expect(screen.queryByRole('dialog')).toBeNull()
    fireEvent.change(field, { target: { value: 'new@example.com' } })
    expect(grant.disabled).toBe(false)
    expect(field.form, 'the field is in a form, so Enter submits it').not.toBeNull()
    fireEvent.submit(field.form!)
    const dialog = await screen.findByRole('dialog', {}, WAIT)
    expect(writesOf(calls)).toHaveLength(0)
    expect(visible(dialog)).toContain('Make new@example.com a platform admin?')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Grant admin' }))
    await waitFor(() => expect(writesOf(calls)).toHaveLength(1), WAIT)
    expect(writesOf(calls)[0]!.url).toBe('/v1/admin/admins/new%40example.com')
  })

  it('ADM-5: a task-scoped entry names its target and tenant, a run of one action is one line, and the cap is said', async () => {
    const purged = (i: number) => ({
      action: 'history_purged',
      target_kind: 'task',
      target_id: `task_${String(i).padStart(4, '0')}`,
      by: 'bob@example.com',
      at: new Date(Date.now() - i * 1000).toISOString(),
      detail: { tenant_id: 'smoke', state: 'SUCCEEDED', event_count: 3 },
    })
    const audit = [purged(0), purged(1), ...Array.from({ length: 48 }, (_, i) => purged(i + 2))]
    await mount(people(ROWS, { audit }))
    await openAudit()
    // Housekeeping is folded away until asked for (#1206).
    fireEvent.click(await screen.findByRole('button', { name: 'Show housekeeping' }, WAIT))
    await waitFor(() => expect(document.querySelector('.pp-audit-list')).not.toBeNull(), WAIT)
    const lines = [...document.querySelectorAll<HTMLElement>('.pp-audit-list li')]
    // MUTATION: draw one line per entry.
    expect(lines).toHaveLength(1)
    expect(visible(lines[0]!)).toContain('History purged × 50 · smoke by bob@example.com')
    expect(screen.getByTestId('pp-audit-shown').textContent).toContain('latest 50')
  })

  it('ADM-5: a single task-scoped entry shows its target_id, never a bare ?', async () => {
    await mount(people(ROWS, { audit: [{ action: 'history_purged', target_kind: 'task', target_id: 'task_0001', by: 'bob@example.com', at: now(), detail: { tenant_id: 'smoke' } }] }))
    await openAudit()
    fireEvent.click(await screen.findByRole('button', { name: 'Show housekeeping' }, WAIT))
    await waitFor(() => expect(document.querySelector('.pp-audit-list')).not.toBeNull(), WAIT)
    const line = visible(document.querySelector('.pp-audit-list'))
    expect(line).toContain('History purged task_0001 · smoke by bob@example.com')
    expect(line).not.toContain('?')
  })
})

describe('the layout fits the card (seen live at 1456, 2026-10-09)', () => {
  it('gives the table its own scroll container, holds no control in it, and keeps the card\'s Deny form in its row', async () => {
    // MUTATION: put `ten-table` back on the table (equal fixed columns), drop
    // the `.table-wrap` around it, or give the Deny textarea a width of its own.
    media(false)
    await mount(people(ROWS))
    await waitFor(() => expect(rowOf('alice@example.com')).not.toBeNull(), WAIT)
    const table = document.querySelector<HTMLElement>('.pp-people table')!
    const wrap = table.parentElement!
    expect(wrap.classList.contains('table-wrap'), 'the table has no container of its own').toBe(true)
    expect(wrap.classList.contains('is-scroll')).toBe(true)
    expect(wrap.closest('.pp-people'), 'the container is not inside the card').not.toBeNull()
    expect(painted(wrap, 'overflow-x', WIDE)).toBe('auto')
    expect(painted(table, 'table-layout', WIDE) ?? 'auto', 'a fixed layout splits the columns evenly').toBe('auto')
    // THE ACTIONS COLUMN IS GONE (#1206): it took 653 of 1260px.
    expect(document.querySelector('td.pp-acts-cell, .pp-acts')).toBeNull()
    expect(table.querySelector('select, textarea, input')).toBeNull()
    fireEvent.click(within(reqOf('workspace', 'alice@example.com')).getByRole('button', { name: 'Deny' }))
    const reason = reqOf('workspace', 'alice@example.com').querySelector<HTMLElement>('textarea')!
    expect(painted(reason, 'width', WIDE)).toBe('100%')
    expect(painted(reason, 'min-width', WIDE)).toBe('0')
  })

  it('keeps a workspace id on one line, in mono, with no break opportunity', async () => {
    // MUTATION: drop `white-space: nowrap` from `.pp-id` (the id broke as
    // `w-` / `752763`), or put `ten-table`'s `overflow-wrap: anywhere` back.
    await mount(people(ROWS))
    await waitFor(() => expect(rowOf('bob@example.com')).not.toBeNull(), WAIT)
    const ids = [...document.querySelectorAll<HTMLElement>('code.pp-id')]
    expect(ids.map((c) => c.textContent)).toContain('w-91c0de')
    for (const id of ids) {
      expect(id.children, 'markup inside the id').toHaveLength(0)
      expect(id.textContent, 'a soft hyphen or zero-width space in the id').not.toMatch(/[\u00ad\u200b]/)
      for (const env of [WIDE, PHONE]) {
        expect(painted(id, 'white-space', env), `the id wraps at ${env.width}`).toBe('nowrap')
        expect(painted(id, 'overflow-wrap', env) ?? 'normal').toBe('normal')
        expect(painted(id, 'word-break', env) ?? 'normal').toBe('normal')
        expect(painted(id, 'font-family', env)).toBe('var(--mono)')
      }
    }
  })

  it('sets every label a token gap above its field', async () => {
    // MUTATION: a bare `<label>` again ('Ceiling' touching its box), or a
    // `.c-field` without its gap.
    await mount(people(ROWS))
    await waitFor(() => expect(rowOf('bob@example.com')).not.toBeNull(), WAIT)
    const ceiling = within(await manage('bob@example.com')).getByLabelText('Ceiling for bob@example.com')
    const email = within(document.querySelector<HTMLElement>('.pp-admins')!).getByLabelText('Email')
    for (const field of [ceiling, email]) {
      const label = field.closest('label')!
      expect(label.classList.contains('c-field'), `${label.textContent} is not a field`).toBe(true)
      expect(painted(label, 'display', WIDE)).toBe('flex')
      expect(painted(label, 'flex-direction', WIDE)).toBe('column')
      expect(fixedPx(painted(label, 'gap', WIDE)), 'the label touches its field').toBeGreaterThan(0)
      expect(label.querySelector(':scope > .c-lbl'), 'the label text is not the field label').not.toBeNull()
    }
  })

  it('is a short record per person on a phone, every cell named, Manage opening the sheet', async () => {
    // MUTATION: a literal `is-scroll` on the wrapper, or a cell without its
    // `data-label` (its value would sit under no name in the record).
    media(true)
    await mount(people(ROWS))
    await waitFor(() => expect(rowOf('bob@example.com')).not.toBeNull(), WAIT)
    const wrap = document.querySelector<HTMLElement>('.pp-people table')!.parentElement!
    expect(wrap.classList.contains('is-stacked')).toBe(true)
    expect(painted(wrap, 'overflow-x', PHONE)).toBe('visible')
    for (const tr of document.querySelectorAll<HTMLElement>('.pp-people tbody tr')) {
      expect(tr.firstElementChild?.matches('th[scope="row"]'), 'the record has no name').toBe(true)
      const labels = [...tr.querySelectorAll('td')].map((td) => td.getAttribute('data-label'))
      expect(labels, 'the phone record is the person, their workspace and Manage').toEqual(['Workspace', 'Manage'])
      expect(painted(tr.querySelector('td')!, 'display', PHONE)).toBe('grid')
    }
  })
})

describe('admins and the audit', () => {
  it('lists the admins, the platform owner first and marked, with no Remove for the owner', async () => {
    // MUTATION: drop the owner's chip, or offer the owner a Remove the API refuses.
    await mount(people(ROWS))
    const list = await screen.findByRole('list', { name: 'Admins' }, WAIT)
    const items = [...list.querySelectorAll<HTMLElement>('li')]
    expect(items.map((li) => li.dataset.email)).toEqual(['owner@example.com', 'bob@example.com'])
    const owner = items[0]!
    expect(owner.querySelector('.pp-owner')?.textContent).toBe('platform owner')
    expect(within(owner).queryByRole('button', { name: 'Remove admin' })).toBeNull()
    expect(items[1]!.querySelector('.pp-owner')).toBeNull()
    expect(visible(items[1]!)).toContain('granted by owner@example.com')
    expect(within(items[1]!).getByRole('button', { name: 'Remove admin' })).toBeDefined()
  })

  it('draws an unread admin list as not read, and keeps the typed Remove', async () => {
    await mount(people(ROWS, { admins: null, admins_error: 'RuntimeError' }))
    await waitFor(() => expect(document.querySelector('.pp-admins .ctl-mark.is-unread')).not.toBeNull(), WAIT)
    const card = document.querySelector<HTMLElement>('.pp-admins')!
    expect(visible(card)).toContain('RuntimeError')
    expect(within(card).queryByRole('list', { name: 'Admins' })).toBeNull()
    expect(within(card).getByRole('button', { name: 'Remove admin' })).toBeDefined()
  })

  it("removes a listed admin, and shows the API's last-admin refusal as it came", async () => {
    const message = 'removing bob@example.com would leave no admin; grant someone else first'
    const { calls } = await mount(people(ROWS), {
      'DELETE /v1/admin/admins/bob%40example.com': () => ({ status: 409, body: { code: 'LAST_ADMIN', message } }),
    })
    await waitFor(() => expect(rowOf('bob@example.com')).not.toBeNull(), WAIT)
    const card = document.querySelector<HTMLElement>('.pp-admins')!
    const bob = card.querySelector<HTMLElement>('li[data-email="bob@example.com"]')!
    fireEvent.click(within(bob).getByRole('button', { name: 'Remove admin' }))
    const dialog = await screen.findByRole('dialog', {}, WAIT)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(visible(card)).toContain(`LAST_ADMIN ${message}`), WAIT)
    expect(writesOf(calls)).toHaveLength(1)
  })

  it('lists the audit entries under the table, or a measured zero', async () => {
    await mount(people(ROWS, { audit: [{ action: 'approve', target_workspace_id: 'w-3f9a2c', by: 'bob@example.com', at: now(), detail: {} }] }))
    await openAudit()
    await waitFor(() => expect(document.querySelector('.pp-audit-list')).not.toBeNull(), WAIT)
    expect(visible(document.querySelector('.pp-audit-list'))).toContain('Workspace approved w-3f9a2c by bob@example.com')
  })
})

// WORKSPACE BUILDING OFF (2026-10-10, w-752763): `people.everyone`'s
// `provisioning` figures and each row's `provisioning` block.
describe('workspace building off in this deployment', () => {
  const waiting = (id: string) =>
    ws('approved', id, {
      decision: { verdict: 'approved', reason: null, at: now() },
      provisioning: { available: false, waiting_because: 'publishing_off', approved_minutes_ago: 1140 },
    })

  it('shows one banner with the count, naming where to switch it on', async () => {
    await mount(
      people([person('alice@example.com', waiting('w-3f9a2c')), person('erin@example.com', waiting('w-e41000')), ...ROWS.slice(0, 1)], {
        provisioning: { available: false, approved_waiting: 2 },
      }),
    )
    const banners = await screen.findAllByText(/Workspace building is off in this deployment/, {}, WAIT)
    expect(banners).toHaveLength(1)
    expect(visible(banners[0]!.closest('.c-banner') as HTMLElement)).toContain(
      'Workspace building is off in this deployment: 2 approved requests are waiting',
    )
    expect(visible(banners[0]!.closest('.c-banner') as HTMLElement)).toContain('docs/workspaces.md §10')
    // The waiting rows say so, and draw no live step rows.
    const row = rowOf('alice@example.com')
    expect(visible(row)).toContain("workspace building isn't switched on")
    expect(row.querySelector('ol[aria-label="Workspace set-up steps"]')).toBeNull()
  })

  it('shows no banner when building is on', async () => {
    await mount(people(ROWS, { provisioning: { available: true, approved_waiting: 0 } }))
    await waitFor(() => expect(rowOf('bob@example.com')).not.toBeNull(), WAIT)
    expect(screen.queryByText(/Workspace building is off/)).toBeNull()
  })

  it("Approve says the API's own sentence when the workspace was not sent for building", async () => {
    const said = 'Approved, and NOT sent for building: workspace provisioning is off in this deployment.'
    await mount(people(ROWS), {
      'POST /v1/admin/workspaces/w-3f9a2c/approve': () => ({
        status: 200,
        body: { workspace: waiting('w-3f9a2c'), dispatch: { published: false, mode: 'create', reason: 'publishing_off' }, sent_for_building: false, message: said },
      }),
    })
    await waitFor(() => expect(reqOf('workspace', 'alice@example.com')).not.toBeNull(), WAIT)
    fireEvent.click(within(reqOf('workspace', 'alice@example.com')).getByRole('button', { name: 'Approve' }))
    await screen.findByText(said, {}, WAIT)
  })
})

// VISUAL QA, LANE VQA-L06 (#1038): V021, V028, V064, V080, V081, V082. Each
// case asserts the property the finding was about, not the markup's shape.
describe('visual QA (VQA-L06)', () => {
  const LONG = `${'a'.repeat(64)}@example.com`

  it('V021: a long email is capped, so it widens no column, and is cut rather than broken (#1206)', async () => {
    // MUTATION: drop `.pp-who`'s `max-width`, or put back `overflow-wrap:
    // anywhere`, which is what broke `devops@example.com` as `devops@example. com`.
    await mount(people([...ROWS, person(LONG, ws('requested', 'w-10a000'))]))
    await waitFor(() => expect(rowOf(LONG)).not.toBeNull(), WAIT)
    const who = [...rowOf(LONG).querySelectorAll<HTMLElement>('.pp-who')]
    expect(who.map((w) => w.textContent)).toEqual([LONG])
    for (const el of who) {
      expect(el.getAttribute('title'), 'the cut address is not readable in full').toBe(LONG)
      for (const env of [WIDE, PHONE]) {
        expect(painted(el, 'overflow-wrap', env) ?? 'normal', `the address breaks mid-word at ${env.width}`).toBe('normal')
        expect(painted(el, 'text-overflow', env)).toBe('ellipsis')
        expect(painted(el, 'display', env)).toBe('block')
        const cap = fixedPx(painted(el, 'max-width', env))
        expect(cap, `the address has no cap at ${env.width}`).not.toBeNull()
        expect(cap!).toBeLessThanOrEqual(320)
      }
    }
  })

  it('V028: opening Deny focuses the reason, and Cancel gives focus back to Deny', async () => {
    // MUTATION: drop the focus effect in `Deny`.
    await mount(people(ROWS))
    await waitFor(() => expect(reqOf('workspace', 'alice@example.com')).not.toBeNull(), WAIT)
    const item = () => reqOf('workspace', 'alice@example.com')
    fireEvent.click(within(item()).getByRole('button', { name: 'Deny' }))
    const reason = item().querySelector('textarea')
    expect(reason).not.toBeNull()
    await waitFor(() => expect(document.activeElement).toBe(reason))
    fireEvent.click(within(item()).getByRole('button', { name: 'Cancel' }))
    const deny = within(item()).getByRole('button', { name: 'Deny' })
    await waitFor(() => expect(document.activeElement).toBe(deny))
  })

  it('V064: the gate keeps the refresh control, and the title row is a control tall in every state', async () => {
    // MUTATION: drop `.pp-page > .c-phead`'s `min-height`, or hide UrRefresh at the gate.
    const { container } = await mount({ code: 'forbidden', message: 'admin group membership is required for this operation' }, {}, 403)
    await waitFor(() => expect(container.querySelector('.state.admin-gate')).not.toBeNull(), WAIT)
    const head = container.querySelector<HTMLElement>('.pp-page > .c-phead')!
    expect(head.querySelector('.c-acts .c-refresh'), 'the gate dropped the head control').not.toBeNull()
    for (const env of [WIDE, PHONE]) expect(painted(head, 'min-height', env)).toBe('var(--h-ctl)')
  })

  it('V080: a request\'s decisions flow side by side, and the drawer never draws Lend and Reclaim together', async () => {
    // MUTATION: `.pp-req-acts` to `display: grid` (one control per line), or
    // the drawer drawing both verbs, one of them disabled.
    await mount(people(ROWS))
    await waitFor(() => expect(reqOf('workspace', 'alice@example.com')).not.toBeNull(), WAIT)
    const acts = reqOf('workspace', 'alice@example.com').querySelector<HTMLElement>('.pp-req-acts')!
    expect(painted(acts, 'display', WIDE)).toBe('flex')
    expect(painted(acts, 'flex-wrap', WIDE)).toBe('wrap')
    const drawer = await manage('bob@example.com')
    const verbs = within(drawer).queryAllByRole('button', { name: /^(Lend|Reclaim)$/ })
    expect(verbs.map((b) => b.textContent)).toEqual(['Lend'])
    expect(verbs[0]!.classList.contains('is-ghost')).toBe(false)
  })

  it('V081: a job step label wraps in its own column, its state does not, and a request id is one token', async () => {
    // MUTATION: drop the `.pp-ws .ob-ws-steps li` grid, `.ob-state`'s nowrap,
    // or `.pp-ws code` from the `.pp-id` rule.
    const moving = person('fay@example.com', ws('approved', 'w-fa0000', { steps: { A1: { state: 'done' }, A2: { state: 'running' } } }))
    const waiting = person('gil@example.com', ws('approved', 'w-61000a', { provisioning: { available: true, waiting_because: 'never_dispatched', approved_minutes_ago: 30 } }))
    await mount(people([...ROWS, moving, waiting]))
    await waitFor(() => expect(rowOf('fay@example.com')).not.toBeNull(), WAIT)
    const step = [...rowOf('fay@example.com').querySelectorAll<HTMLElement>('.ob-ws-steps li')].find((li) => li.textContent?.includes('Checking the name is free'))
    expect(step, 'the A2 step was not drawn').toBeDefined()
    expect(painted(step!, 'display', WIDE)).toBe('grid')
    // `min-content`, not `0` (VQA2-R4, 2026-10-16): at 0 a squeezed 1440
    // table let "Namespace" run into "not started" (people.vqa2r4.test.tsx).
    expect(painted(step!, 'grid-template-columns', WIDE)).toBe('auto minmax(min-content, 1fr) auto')
    expect(painted(step!.querySelector('.ob-state')!, 'white-space', WIDE)).toBe('nowrap')
    const rid = [...rowOf('gil@example.com').querySelectorAll<HTMLElement>('.pp-ws code')].find((c) => c.textContent === 'req-w-61000a')
    expect(rid, 'the request id was not drawn').toBeDefined()
    for (const env of [WIDE, PHONE]) {
      expect(painted(rid!, 'white-space', env)).toBe('nowrap')
      expect(painted(rid!, 'overflow-wrap', env) ?? 'normal').toBe('normal')
    }
  })

  it('V082: Remove admin is bordered like Grant admin, read or unread, and no card repeats the page title', async () => {
    // MUTATION: either Remove admin back to `kind="ghost"`, or the people card titled "People" again.
    await mount(people(ROWS))
    await waitFor(() => expect(document.querySelector('.pp-admin-list')).not.toBeNull(), WAIT)
    const grant = within(document.querySelector<HTMLElement>('.pp-admins')!).getByRole('button', { name: 'Grant admin' })
    for (const b of within(document.querySelector<HTMLElement>('.pp-admins')!).getAllByRole('button', { name: 'Remove admin' })) {
      expect(b.className).toBe(grant.className)
    }
    const titles = [...document.querySelectorAll('.pp-page .c-card h2, .pp-page .c-card h3')].map((h) => h.textContent?.trim())
    expect(titles.length, 'no card title was drawn').toBeGreaterThan(0)
    expect(titles).not.toContain('People and roles')
  })

  it('V082: the typed Remove admin of an unread list is bordered too', async () => {
    await mount(people(ROWS, { admins: null, admins_error: 'RuntimeError' }))
    await waitFor(() => expect(document.querySelector('.pp-admins .ctl-mark.is-unread')).not.toBeNull(), WAIT)
    const card = document.querySelector<HTMLElement>('.pp-admins')!
    expect(within(card).getByRole('button', { name: 'Remove admin' }).className).toBe(within(card).getByRole('button', { name: 'Grant admin' }).className)
  })
})
