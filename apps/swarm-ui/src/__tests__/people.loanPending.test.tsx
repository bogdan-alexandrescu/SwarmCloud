// ADMIN › PEOPLE, A LOAN TO A WORKSPACE STILL BEING MADE IS PENDING (LOAN-ON-READ, part of #2688).
//
// Owner report 2026-11-06: an admin lent eng:ai-eng-3-max to a person whose
// workspace (w-4e4036) was still provisioning. The API recorded it pending
// (`state: "pending"`, with a message) and granted nothing yet, but the picker
// ignored the answer's state: the first click said "Lent eng:ai-eng-3-max."
// and a second "eng:ai-eng-3-max was already lent.".
//
// WHAT EACH CASE HOLDS:
//   * a pending answer says the loan is recorded and applies when their
//     workspace is ready, one line, the API's message behind an (i); never
//     "Lent" and never "already lent";
//   * a second lend of a pending loan says it is already pending;
//   * an applied answer still says "Lent";
//   * the row and the drawer show a pending loan as pending, and once it is
//     applied (the row's `lent_accounts`) as lent and no longer pending.
//
// Placeholders only; nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }

type Json = Record<string, unknown>

const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString()

function ws(state: string, id: string): Json {
  return {
    state,
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

function person(email: string, workspace: Json, over: Json = {}): Json {
  return {
    email,
    teams: ['eng'],
    github: 'connected',
    workspace,
    claude_account: { source: 'none', own: 0, lent: 0, lent_by: [], provider_key: false },
    loan_request: null,
    pending_loans: [],
    first_seen: ago(600),
    last_seen: ago(1),
    last_submitted: null,
    last_active: ago(1),
    ...over,
  }
}

const ENG = { account_id: 'acct-eng-1', owner_tenant: 'eng', label: 'eng shared', state: 'AVAILABLE', lend_to: [] }
const ADMINS = [{ email: 'owner@example.com', role: 'owner', granted_by: 'config:PLATFORM_OWNER', granted_at: null }]

function people(rows: Json[]): Json {
  return {
    people: rows,
    count: rows.length,
    pending: 0,
    audit: [],
    lendable_accounts: [ENG],
    lendable_error: null,
    admins: ADMINS,
    admins_error: null,
  }
}

const MESSAGE = 'Recorded. Workspace w-4e4036 is being created; the loan will apply when the workspace is ready.'

function answer(over: Json): Json {
  return { workspace_id: 'w-4e4036', account_id: 'acct-eng-1', owner_tenant: 'eng', lent: true, ...over }
}

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
  await screen.findByRole('heading', { name: 'Platform people', level: 1 }, WAIT)
  await waitFor(() => expect(document.querySelector('.pp-people tbody tr')).not.toBeNull(), WAIT)
  return { ...utils, calls }
}

const rowOf = (email: string) => document.querySelector<HTMLElement>(`.pp-people tr[data-email="${email}"]`)!
const sectionOf = (drawer: HTMLElement) => drawer.querySelector<HTMLElement>('[data-section="claude"]')!

async function lendFromDrawer(email: string): Promise<HTMLElement> {
  fireEvent.click(within(rowOf(email)).getByRole('button', { name: 'Manage' }))
  const drawer = await screen.findByRole('dialog', { name: `Manage ${email}` }, WAIT)
  const section = sectionOf(drawer)
  fireEvent.change(within(section).getByLabelText(`Account to lend to ${email}`), { target: { value: 'acct-eng-1' } })
  fireEvent.click(within(section).getByRole('button', { name: 'Lend' }))
  const confirm = await screen.findByRole('dialog', { name: 'Lend this account' }, WAIT)
  fireEvent.click(within(confirm).getByRole('button', { name: 'Lend' }))
  return section
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

const AIENG = 'aieng3@example.com'

describe('the lend picker says what the answer says', () => {
  it('says a pending loan is recorded and applies when the workspace is ready, never "Lent"', async () => {
    await mount(people([person(AIENG, ws('applying', 'w-4e4036'))]), {
      'PUT /v1/admin/people/w-4e4036/loan': () => ({ status: 200, body: answer({ changed: true, state: 'pending', message: MESSAGE }) }),
    })
    const section = await lendFromDrawer(AIENG)
    const status = await within(section).findByRole('status', {}, WAIT)
    // MUTATION: back to `d.changed ? (d.lent ? \`Lent ${d.account_id}.\` ...`.
    expect(visible(status)).toContain('acct-eng-1 is recorded: it applies when their workspace is ready.')
    expect(visible(status)).not.toMatch(/\bLent\b|already lent/)
    // The API's sentence is behind the (i), not printed.
    const tip = within(status).getByRole('button', { name: 'About the pending loan' })
    expect(document.getElementById(tip.getAttribute('aria-describedby')!)?.textContent).toContain(MESSAGE)
  })

  it('says a second lend of a pending loan is already pending, never "already lent"', async () => {
    await mount(people([person(AIENG, ws('applying', 'w-4e4036'))]), {
      'PUT /v1/admin/people/w-4e4036/loan': () => ({ status: 200, body: answer({ changed: false, state: 'pending', message: MESSAGE }) }),
    })
    const section = await lendFromDrawer(AIENG)
    const status = await within(section).findByRole('status', {}, WAIT)
    expect(visible(status)).toContain('acct-eng-1 is already pending.')
    expect(visible(status)).not.toMatch(/already lent|\bLent\b/)
  })

  it('still says "Lent" when the loan was applied at once', async () => {
    await mount(people([person(AIENG, ws('ready', 'w-4e4036'))]), {
      'PUT /v1/admin/people/w-4e4036/loan': () => ({ status: 200, body: answer({ changed: true, state: 'applied' }) }),
    })
    const section = await lendFromDrawer(AIENG)
    const status = await within(section).findByRole('status', {}, WAIT)
    expect(visible(status)).toContain('Lent acct-eng-1.')
  })
})

describe('the row shows a pending loan as pending, then as lent', () => {
  const PENDING = [{ account_id: 'acct-eng-1', owner_tenant: 'eng', since: ago(2), last_error: null }]

  it('marks the row and the drawer with the pending loan', async () => {
    await mount(people([person(AIENG, ws('applying', 'w-4e4036'), { pending_loans: PENDING })]))
    // MUTATION: drop the pending chip from ClaudeCell.
    expect(visible(rowOf(AIENG))).toContain('loan pending')
    fireEvent.click(within(rowOf(AIENG)).getByRole('button', { name: 'Manage' }))
    const drawer = await screen.findByRole('dialog', { name: `Manage ${AIENG}` }, WAIT)
    expect(visible(sectionOf(drawer))).toContain('loan pending: acct-eng-1')
  })

  it('puts a refused try behind the (i)', async () => {
    const refused = [{ ...PENDING[0], last_error: 'the quota broker refused the grant' }]
    await mount(people([person(AIENG, ws('ready', 'w-4e4036'), { pending_loans: refused })]))
    fireEvent.click(within(rowOf(AIENG)).getByRole('button', { name: 'Manage' }))
    const drawer = await screen.findByRole('dialog', { name: `Manage ${AIENG}` }, WAIT)
    const tip = within(sectionOf(drawer)).getByRole('button', { name: 'About the pending loan of acct-eng-1' })
    expect(document.getElementById(tip.getAttribute('aria-describedby')!)?.textContent).toContain('the quota broker refused the grant')
  })

  it('shows an applied loan as lent and no longer pending', async () => {
    const lent = [{ account_id: 'acct-eng-1', label: 'eng shared', owner_tenant: 'eng', owner_name: 'Engineering', state: 'AVAILABLE' }]
    await mount(
      people([
        person(AIENG, ws('ready', 'w-4e4036'), {
          pending_loans: [],
          claude_account: { source: 'lent', own: 0, lent: 1, lent_by: ['eng'], lent_accounts: lent, provider_key: false },
        }),
      ]),
    )
    expect(visible(rowOf(AIENG))).toContain('acct-eng-1 (eng shared), lent by Engineering')
    expect(visible(rowOf(AIENG))).not.toContain('loan pending')
  })
})
