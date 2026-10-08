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
//   * zero pending is drawn as a measured zero; an unread lendable list as
//     not read, never as an empty one.
//
// The bodies are `people.People.everyone`'s shape
// (tests/unit/control_plane/test_people_admin.py). Placeholders only;
// nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
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

const ACCOUNT = { account_id: 'acct-eng-1', owner_tenant: 'eng', label: 'eng shared', state: 'AVAILABLE', lend_to: [] }

function people(rows: Json[], over: Json = {}): Json {
  return {
    people: rows,
    count: rows.length,
    pending: rows.filter((r) => (r.workspace as Json).state === 'requested').length,
    audit: [],
    lendable_accounts: [ACCOUNT],
    lendable_error: null,
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
})

describe('the tab', () => {
  it('is in the Admin section, admin only', () => {
    const admin = SECTIONS.find((s) => s.id === 'admin')!
    const tab = admin.tabs.find((t) => t.id === 'people')
    expect(tab).toEqual({ id: 'people', label: 'People', admin: true })
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

  it('draws zero pending as a measured zero', async () => {
    await mount(people([ROWS[0]!]))
    await waitFor(() => expect(rowOf('bob@example.com')).not.toBeNull(), WAIT)
    const chip = document.querySelector<HTMLElement>('.pp-people')!
    expect(visible(chip)).toContain('1 person')
    expect(chip.querySelector('.ctl-mark.is-zero')?.textContent).toBe('real zero')
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

describe('admins and the audit', () => {
  it("shows the API's last-admin refusal as it came", async () => {
    const message = 'removing bob@example.com would leave no admin; grant someone else first'
    const { calls } = await mount(people(ROWS), {
      'DELETE /v1/admin/admins/bob%40example.com': () => ({ status: 409, body: { code: 'LAST_ADMIN', message } }),
    })
    await waitFor(() => expect(rowOf('bob@example.com')).not.toBeNull(), WAIT)
    const card = document.querySelector<HTMLElement>('.pp-admins')!
    fireEvent.change(within(card).getByLabelText('Email'), { target: { value: 'bob@example.com' } })
    fireEvent.click(within(card).getByRole('button', { name: 'Remove admin' }))
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
