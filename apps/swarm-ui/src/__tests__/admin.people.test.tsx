// ADMIN › PEOPLE (#847, lane W8; docs/workspaces.md §6.4, §6.5).
//
// WHAT EACH CASE HOLDS:
//   * the tab is in the Admin section, admin only, and routes to the screen;
//   * a non-admin's read (403, admin_auth's sentence) draws the admin gate
//     and no table, no button;
//   * `requested` rows sort first, the server's order otherwise;
//   * Approve asks once, in §6.4's words, then posts the approve route by
//     WORKSPACE ID -- no email in any workspace route;
//   * Deny without a reason posts nothing; with one, posts `{reason}`;
//   * a failed row has Retry; a needs_owner row says it waits for the platform
//     owner and names the step the guard stopped at;
//   * a ceiling save sends `{max_active}` and nothing else;
//   * Lend posts `{account_id, lend: true}` for the account chosen;
//   * the API's refusal for the last admin is shown as it came;
//   * zero pending is prose, `none pending`, with "measured" in its accessible
//     name and no `real zero` mark (owner, 2026-10-09); an unread lendable list as
//     not read, never as an empty one;
//   * the layout seen live at 1456 on 2026-10-09: the table has its own scroll
//     container inside the card and no action control has a width of its own
//     wider than its cell; a workspace id has no break opportunity; a label
//     sits a token's gap from its field; below 560px a record per person;
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
import { SkyShell } from '../Spine'

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
  await screen.findByRole('heading', { name: 'People', level: 1 }, WAIT)
  return { ...utils, calls }
}

const rowOf = (email: string) => document.querySelector<HTMLElement>(`tr[data-email="${email}"]`)!
const writesOf = (calls: { method: string; url: string; body: unknown }[]) => calls.filter((c) => c.method !== 'GET')

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
    expect(tab).toEqual({ id: 'people', label: 'People', admin: true })
  })

  // A tab and a route are not enough: the panel draws PANEL_PAGES, and a page
  // missing there is reachable only by typing its address (visual QA Q7).
  it('is drawn in the Admin panel, and lit on /admin/people', () => {
    render(
      <SkyShell section="admin" tab="people" title="People" go={vi.fn()} foot={null}>
        {null}
      </SkyShell>,
    )
    const labels = [...document.querySelectorAll('.sk-panel .sk-pk .sk-pl')].map((el) => el.textContent)
    expect(labels).toContain('People')
    expect(document.querySelector('.sk-panel .sk-pk.is-on .sk-pl')?.textContent).toBe('People')
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
    // A person with no record has no action on a workspace.
    expect(within(rowOf('carol@example.com')).queryByRole('button')).toBeNull()
  })

  it('a failed row has Retry; a needs_owner row waits for the owner at its step', async () => {
    const { calls } = await mount(people(ROWS), {
      'POST /v1/admin/workspaces/w-d4n000/retry': () => ({ status: 200, body: { workspace: ws('approved', 'w-d4n000'), dispatch: { published: true, mode: 'create', reason: null } } }),
    })
    await waitFor(() => expect(rowOf('dan@example.com')).not.toBeNull(), WAIT)
    expect(visible(rowOf('dan@example.com'))).toContain('failed w-d4n000 (Namespace)')
    expect(within(rowOf('erin@example.com')).queryByRole('button', { name: 'Retry' })).toBeNull()
    expect(visible(rowOf('erin@example.com'))).toContain('Waiting for the platform owner · stopped at Access')
    fireEvent.click(within(rowOf('dan@example.com')).getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(writesOf(calls)).toHaveLength(1), WAIT)
    expect(writesOf(calls)[0]).toMatchObject({ method: 'POST', url: '/v1/admin/workspaces/w-d4n000/retry' })
  })

  it('a denied row may still be approved, and is not offered Retry', async () => {
    const denied = person('fay@example.com', ws('denied', 'w-fa1000', { decision: { verdict: 'denied', reason: 'Use the eng space.', at: now() } }))
    const { calls } = await mount(people([denied]), {
      'POST /v1/admin/workspaces/w-fa1000/approve': () => ({ status: 200, body: { workspace: ws('approved', 'w-fa1000'), dispatch: { published: true, mode: 'create', reason: null } } }),
    })
    await waitFor(() => expect(rowOf('fay@example.com')).not.toBeNull(), WAIT)
    const row = rowOf('fay@example.com')
    expect(visible(row)).toContain('“Use the eng space.”')
    expect(within(row).queryByRole('button', { name: 'Retry' })).toBeNull()
    fireEvent.click(within(row).getByRole('button', { name: 'Approve' }))
    fireEvent.click(within(await screen.findByRole('dialog', {}, WAIT)).getByRole('button', { name: 'Approve' }))
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
    const row = rowOf('alice@example.com')
    expect(row.querySelector('.ctl-mark.is-unread')).not.toBeNull()
    expect(visible(row)).toContain('upstream_unavailable')
    expect(row.querySelector('.ctl-mark.is-zero')).toBeNull()
  })
})

describe('approve and deny', () => {
  it('Approve asks once in §6.4\'s words, then posts the approve route by workspace id', async () => {
    const { calls } = await mount(people(ROWS), {
      'POST /v1/admin/workspaces/w-3f9a2c/approve': () => ({ status: 200, body: { workspace: ws('approved', 'w-3f9a2c'), dispatch: { published: true, mode: 'create', reason: null } } }),
    })
    await waitFor(() => expect(rowOf('alice@example.com')).not.toBeNull(), WAIT)
    fireEvent.click(within(rowOf('alice@example.com')).getByRole('button', { name: 'Approve' }))
    const dialog = await screen.findByRole('dialog', {}, WAIT)
    expect(visible(dialog)).toContain('Create a workspace for alice@example.com with 8 agents? A CI job will create its identity and namespace.')
    expect(writesOf(calls), 'it posted before the confirmation').toHaveLength(0)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(writesOf(calls)).toHaveLength(1), WAIT)
    expect(writesOf(calls)[0]).toMatchObject({ method: 'POST', url: '/v1/admin/workspaces/w-3f9a2c/approve' })
    for (const c of writesOf(calls)) expect(c.url, 'an email reached a workspace route').not.toContain('@')
  })

  it('Deny without a reason posts nothing; with one, posts {reason}', async () => {
    const { calls } = await mount(people(ROWS), {
      'POST /v1/admin/workspaces/w-3f9a2c/deny': () => ({ status: 200, body: { workspace: ws('denied', 'w-3f9a2c') } }),
    })
    await waitFor(() => expect(rowOf('alice@example.com')).not.toBeNull(), WAIT)
    const row = rowOf('alice@example.com')
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
    const row = rowOf('bob@example.com')
    const field = within(row).getByLabelText('Ceiling for bob@example.com') as HTMLInputElement
    expect(field.value).toBe('8')
    fireEvent.change(field, { target: { value: '12' } })
    fireEvent.click(within(row).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(writesOf(calls)).toHaveLength(1), WAIT)
    expect(writesOf(calls)[0]).toEqual({ method: 'PUT', url: '/v1/admin/workspaces/w-91c0de/limits', body: { max_active: 12 } })
    await waitFor(() => expect(visible(row)).toContain('Ceiling 12 saved.'), WAIT)
  })

  it('Lend posts {account_id, lend: true} for the account chosen', async () => {
    const { calls } = await mount(people(ROWS), {
      'PUT /v1/admin/people/w-91c0de/loan': () => ({ status: 200, body: { workspace_id: 'w-91c0de', account_id: 'acct-eng-1', owner_tenant: 'eng', lent: true, changed: true } }),
    })
    await waitFor(() => expect(rowOf('bob@example.com')).not.toBeNull(), WAIT)
    const row = rowOf('bob@example.com')
    const lend = within(row).getByRole('button', { name: 'Lend' }) as HTMLButtonElement
    expect(lend.disabled, 'Lend is live with no account chosen').toBe(true)
    fireEvent.change(within(row).getByLabelText('Account to lend to bob@example.com'), { target: { value: 'acct-eng-1' } })
    fireEvent.click(lend)
    await waitFor(() => expect(writesOf(calls)).toHaveLength(1), WAIT)
    expect(writesOf(calls)[0]).toEqual({ method: 'PUT', url: '/v1/admin/people/w-91c0de/loan', body: { account_id: 'acct-eng-1', lend: true } })
    await waitFor(() => expect(visible(row)).toContain('Lent acct-eng-1.'), WAIT)
  })
})

describe('the layout fits the card (seen live at 1456, 2026-10-09)', () => {
  it('gives the table its own scroll container, and no action control a width wider than its cell', async () => {
    // MUTATION: put `ten-table` back on the table (seven equal fixed columns),
    // drop the `.table-wrap` around it, or give the Deny textarea back its
    // `min-width: 220px` -- each is what pushed Deny and the select off the card.
    media(false)
    await mount(people(ROWS))
    await waitFor(() => expect(rowOf('alice@example.com')).not.toBeNull(), WAIT)
    fireEvent.click(within(rowOf('alice@example.com')).getByRole('button', { name: 'Deny' }))
    const table = document.querySelector<HTMLElement>('.pp-people table')!
    const wrap = table.parentElement!
    expect(wrap.classList.contains('table-wrap'), 'the table has no container of its own').toBe(true)
    expect(wrap.classList.contains('is-scroll')).toBe(true)
    expect(wrap.closest('.pp-people'), 'the container is not inside the card').not.toBeNull()
    expect(painted(wrap, 'overflow-x', WIDE)).toBe('auto')
    expect(painted(table, 'table-layout', WIDE) ?? 'auto', 'a fixed layout splits the columns evenly').toBe('auto')

    const cells = [...document.querySelectorAll<HTMLElement>('td.pp-acts-cell')]
    expect(cells.length, 'no Actions cell was drawn').toBe(ROWS.length)
    const floor = fixedPx(painted(cells[0]!, 'min-width', WIDE))
    expect(floor, 'the Actions column has no floor').not.toBeNull()
    const controls = cells.flatMap((c) => [...c.querySelectorAll<HTMLElement>('*')])
    expect(controls.some((el) => el.tagName === 'TEXTAREA'), 'the open Deny form was not drawn').toBe(true)
    expect(controls.some((el) => el.tagName === 'SELECT'), 'the Account select was not drawn').toBe(true)
    for (const el of controls) {
      for (const prop of ['width', 'min-width']) {
        const px = fixedPx(painted(el, prop, WIDE))
        if (px === null) continue
        expect(px, `<${el.tagName.toLowerCase()} class="${el.className}"> ${prop} is wider than its cell`).toBeLessThanOrEqual(floor!)
      }
    }
    for (const el of document.querySelectorAll<HTMLElement>('.pp-acts select, .pp-acts textarea')) {
      expect(painted(el, 'width', WIDE), `${el.tagName} is not the cell's width`).toBe('100%')
      expect(painted(el, 'min-width', WIDE)).toBe('0')
    }
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
    const ceiling = within(rowOf('bob@example.com')).getByLabelText('Ceiling for bob@example.com')
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

  it('is a record per person on a phone, every cell named', async () => {
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
      for (const td of tr.querySelectorAll('td')) expect(td.getAttribute('data-label'), 'a cell with no name').toBeTruthy()
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
    await waitFor(() => expect(document.querySelector('.pp-audit-list')).not.toBeNull(), WAIT)
    expect(visible(document.querySelector('.pp-audit-list'))).toContain('approve w-3f9a2c by bob@example.com')
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
    await waitFor(() => expect(rowOf('alice@example.com')).not.toBeNull(), WAIT)
    fireEvent.click(within(rowOf('alice@example.com')).getByRole('button', { name: 'Approve' }))
    const dialog = await screen.findByRole('dialog', {}, WAIT)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Approve' }))
    await screen.findByText(said, {}, WAIT)
  })
})
