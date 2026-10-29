// QA2-FIX-18 (visual QA pass 2, part of #2694): Admin › People and roles.
//
// WHAT EACH CASE HOLDS:
//   * admin2-04: the Manage sheet's account select and 'Now using' say an
//     account's state in plain words ('available', 'paused', 'needs signing
//     in again'), never the broker's enum, and name the lender as a
//     workspace ('Bo’s workspace'), never by its tenant id;
//   * admin2-22: a workspace request on Waiting for you says what is asked
//     for ('A personal workspace for carol@example.com'), with the generated
//     id as the secondary line, not as the request's name.
//
// The doc shape is accounts.name2.test.tsx's. Placeholders only; nothing here
// is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }

type Json = Record<string, unknown>

const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString()

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

function workspace(state: string, id: string): Json {
  return {
    state,
    setup_url: '/setup#workspace',
    setup_command: '/sc:setup',
    workspace_id: id,
    request_id: `req-${id}`,
    requested_at: ago(60 * 24 * 10),
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

function person(email: string, name: string | null, ws: Json, claude: Json): Json {
  return {
    email,
    display_name: name,
    teams: ['eng'],
    github: 'connected',
    workspace: ws,
    claude_account: claude,
    loan_request: null,
    first_seen: ago(600),
    last_seen: ago(1),
    last_submitted: null,
    last_active: ago(1),
  }
}

const NONE = { source: 'none', own: 0, lent: 0, lent_by: [], provider_key: false }
// An older API's loan: the lender tenant alone, no `lent_accounts`.
const LENT_BY_BO = { source: 'lent', own: 0, lent: 1, lent_by: ['u-bo'], provider_key: false }

const account = (label: string, state: string): Json => ({ account_id: `u-bo:${label}`, owner_tenant: 'u-bo', label, state, lend_to: ['u-bob'], display_name: null })

function peopleDoc(rows: Json[], lendable: Json[]): Json {
  return {
    people: rows,
    count: rows.length,
    pending: 1,
    audit: [],
    lendable_accounts: lendable,
    lendable_error: null,
    admins: [{ email: 'owner@example.com', role: 'owner', granted_by: 'config:PLATFORM_OWNER', granted_at: null }],
    admins_error: null,
  }
}

const DOC = peopleDoc(
  [
    person('bo@example.com', 'Bo', workspace('ready', 'u-bo'), NONE),
    person('bob@example.com', 'Bob', workspace('ready', 'u-bob'), LENT_BY_BO),
    person('carol@example.com', 'Carol', workspace('requested', 'w-c4r01e'), NONE),
  ],
  [account('primary', 'AVAILABLE'), account('weekly-burn', 'PAUSED'), account('retired-laptop', 'REAUTH_REQUIRED')],
)

async function mountPeople(body: Json) {
  serve((m, url) => (m === 'GET' && url === '/v1/admin/people' ? { status: 200, body } : null))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { PeopleScreen } = await import('../People')
  render(<PeopleScreen />)
  await screen.findByRole('heading', { name: 'People and roles', level: 1 }, WAIT)
  await waitFor(() => expect(document.querySelector('.pp-people tbody tr')).not.toBeNull(), WAIT)
}

const rowOf = (email: string) => document.querySelector<HTMLElement>(`.pp-people tr[data-email="${email}"]`)!

async function claudeSection(email: string): Promise<HTMLElement> {
  fireEvent.click(within(rowOf(email)).getByRole('button', { name: 'Manage' }))
  const drawer = await screen.findByRole('dialog', { name: /^Manage / }, WAIT)
  return drawer.querySelector<HTMLElement>('[data-section="claude"]')!
}

describe('People Manage sheet says account states and lenders in words (admin2-04)', () => {
  it('lists each lent account by plain state words and its lender workspace', async () => {
    await mountPeople(DOC)
    const section = await claudeSection('bob@example.com')
    const select = within(section).getByLabelText('Account to reclaim from bob@example.com')
    const options = [...select.querySelectorAll('option')].map((o) => o.textContent)
    // MUTATION: the <option> back to `· ${a.owner_tenant}` and `· ${a.state}`.
    expect(options).toEqual([
      'choose…',
      'primary · Bo’s workspace · available',
      'weekly-burn · Bo’s workspace · paused (starts no new agent)',
      'retired-laptop · Bo’s workspace · needs signing in again (starts no new agent)',
    ])
    expect(visible(section)).not.toMatch(/AVAILABLE|PAUSED|REAUTH_REQUIRED/)
  })

  it("names the lender in 'Now using' and the confirmation as a workspace, not a tenant id", async () => {
    await mountPeople(DOC)
    // MUTATION: `claudeText` back to `c.lent_by.join(', ')`.
    expect(visible(rowOf('bob@example.com'))).toContain('lent by Bo’s workspace')
    const section = await claudeSection('bob@example.com')
    expect(visible(section)).toContain('Now using lent by Bo’s workspace')
    expect(visible(section)).not.toContain('lent by u-bo')
    fireEvent.change(within(section).getByLabelText('Account to reclaim from bob@example.com'), { target: { value: 'u-bo:primary' } })
    fireEvent.click(within(section).getByRole('button', { name: 'Reclaim' }))
    const confirm = await screen.findByRole('dialog', { name: 'Reclaim this account' }, WAIT)
    // MUTATION: the confirmation's `name` and hint back to `chosen.owner_tenant`.
    expect(visible(confirm)).toContain('Reclaim primary (Bo’s workspace) from bob@example.com?')
    expect(visible(confirm)).toContain('Bo’s workspace stops lending it to them')
  })
})

describe('A workspace request says what is asked for (admin2-22)', () => {
  it('reads as a personal workspace for the person, the id on its own line', async () => {
    await mountPeople(DOC)
    const req = await waitFor(() => {
      const li = document.querySelector<HTMLElement>('.pp-waiting li[data-kind="workspace"][data-email="carol@example.com"]')
      expect(li).not.toBeNull()
      return li!
    }, WAIT)
    const what = req.querySelector<HTMLElement>('.pp-req-what')!
    // MUTATION: the line back to `Workspace <code>{workspace_id}</code> · asked …`.
    expect(visible(what)).toMatch(/^A personal workspace for carol@example\.com · /)
    expect(visible(what)).not.toMatch(/^Workspace w-c4r01e/)
    // The id is still there, as the secondary line.
    expect(what.querySelector('.pp-req-id')?.textContent).toBe('id w-c4r01e')
  })
})
