// AN ACCOUNT'S NAME ON PEOPLE AND PROFILE (ACCOUNT-NAME-2; part of #1913).
//
// ACCOUNT-NAME gave an account an editable Name, shown in place of its label
// "everywhere the console names the account" (owner decision 2026-10-22, BGD,
// "Editable display name"). Its implement and fix steps both found two screens
// still printing the label: Admin › People (the Claude account section, the
// lend and reclaim picker and its confirmation) and Settings › Profile ('Your
// Claude accounts'). BGD, 2026-10-22: "Yes, one small follow-up lane".
//
// WHAT EACH CASE HOLDS:
//   * a named account shows its Name where the label was, and its id stays
//     printed wherever it was printed before;
//   * an account with no Name (absent or null) reads exactly as it did.
//
// Placeholders only; nothing here is token-shaped.

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

// -- Admin › People ------------------------------------------------------------

function ready(id: string): Json {
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

function person(email: string, id: string, claude: Json): Json {
  return {
    email,
    teams: ['eng'],
    github: 'connected',
    workspace: ready(id),
    claude_account: claude,
    loan_request: null,
    first_seen: ago(600),
    last_seen: ago(1),
    last_submitted: null,
    last_active: ago(1),
  }
}

const NONE = { source: 'none', own: 0, lent: 0, lent_by: [], provider_key: false }

function lentTo(email: string, id: string, accounts: Json[]): Json {
  const owners = [...new Set(accounts.map((a) => a.owner_tenant as string))].sort()
  return person(email, id, { source: 'lent', own: 0, lent: accounts.length, lent_by: owners, lent_accounts: accounts, provider_key: false })
}

const NAMED = { account_id: 'eng:ai-eng-2', owner_tenant: 'eng', label: 'ai-eng-2', state: 'AVAILABLE', lend_to: [], display_name: 'AI Eng 2' }
const PLAIN = { account_id: 'eng:plain', owner_tenant: 'eng', label: 'plain', state: 'AVAILABLE', lend_to: [], display_name: null }
const LENT_NAMED = { account_id: 'eng:ai-eng-2', label: 'ai-eng-2', owner_tenant: 'eng', owner_name: 'Engineering', state: 'AVAILABLE', display_name: 'AI Eng 2' }
const LENT_PLAIN = { account_id: 'eng:plain', label: 'plain', owner_tenant: 'eng', owner_name: 'Engineering', state: 'AVAILABLE' }

function peopleDoc(rows: Json[], lendable: Json[]): Json {
  return {
    people: rows,
    count: rows.length,
    pending: 0,
    audit: [],
    lendable_accounts: lendable,
    lendable_error: null,
    admins: [{ email: 'owner@example.com', role: 'owner', granted_by: 'config:PLATFORM_OWNER', granted_at: null }],
    admins_error: null,
  }
}

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
  const drawer = await screen.findByRole('dialog', { name: `Manage ${email}` }, WAIT)
  return drawer.querySelector<HTMLElement>('[data-section="claude"]')!
}

describe('Admin › People names each account by its Name', () => {
  it('says a lent account by its id, then its Name, in the row and the drawer', async () => {
    await mountPeople(peopleDoc([lentTo('hal@example.com', 'w-4a1000', [LENT_NAMED])], [NAMED]))
    // MUTATION: People's lent-account text back to its own `${account_id} (${label})`.
    expect(visible(rowOf('hal@example.com'))).toContain('eng:ai-eng-2 (AI Eng 2), lent by Engineering')
    const section = await claudeSection('hal@example.com')
    expect(visible(section)).toContain('Now using eng:ai-eng-2 (AI Eng 2), lent by Engineering')
    // MUTATION: the one-account reclaim text back to the label.
    expect(section.querySelector('[data-testid="pp-loan-one"]')?.textContent).toContain('eng:ai-eng-2 (AI Eng 2)')
    expect(visible(section)).not.toContain('ai-eng-2)')
  })

  it('names the account in the reclaim confirmation by its Name', async () => {
    await mountPeople(peopleDoc([lentTo('hal@example.com', 'w-4a1000', [LENT_NAMED])], [NAMED]))
    const section = await claudeSection('hal@example.com')
    fireEvent.click(within(section).getByRole('button', { name: 'Reclaim' }))
    const confirm = await screen.findByRole('dialog', { name: 'Reclaim this account' }, WAIT)
    // MUTATION: the confirmation's `name` back to `chosen.label ?? chosen.account_id`.
    expect(visible(confirm)).toContain('Reclaim AI Eng 2 (eng) from hal@example.com?')
  })

  it('names each account in the lend picker and the lend confirmation by its Name', async () => {
    await mountPeople(peopleDoc([person('bob@example.com', 'w-91c0de', NONE)], [NAMED, PLAIN]))
    const section = await claudeSection('bob@example.com')
    const select = within(section).getByLabelText('Account to lend to bob@example.com')
    const options = [...select.querySelectorAll('option')]
    // MUTATION: the `<option>` back to `a.label ?? a.account_id`.
    expect(options.map((o) => o.textContent)).toEqual(['choose…', 'AI Eng 2 · eng · available', 'plain · eng · available'])
    // The id is still what the option selects.
    expect(options.map((o) => o.getAttribute('value'))).toEqual(['', 'eng:ai-eng-2', 'eng:plain'])
    fireEvent.change(select, { target: { value: 'eng:ai-eng-2' } })
    fireEvent.click(within(section).getByRole('button', { name: 'Lend' }))
    const confirm = await screen.findByRole('dialog', { name: 'Lend this account' }, WAIT)
    // MUTATION: the confirmation's `name` back to `chosen.label ?? chosen.account_id`.
    expect(visible(confirm)).toContain('Lend AI Eng 2 (eng) to bob@example.com?')
  })

  it('says an unnamed account by its label, exactly as before', async () => {
    await mountPeople(peopleDoc([lentTo('hal@example.com', 'w-4a1000', [LENT_PLAIN])], [PLAIN]))
    // MUTATION: print the id twice, or nothing, when the account has no Name.
    expect(visible(rowOf('hal@example.com'))).toContain('eng:plain (plain), lent by Engineering')
    const section = await claudeSection('hal@example.com')
    expect(section.querySelector('[data-testid="pp-loan-one"]')?.textContent).toContain('eng:plain (plain)')
    fireEvent.click(within(section).getByRole('button', { name: 'Reclaim' }))
    const confirm = await screen.findByRole('dialog', { name: 'Reclaim this account' }, WAIT)
    expect(visible(confirm)).toContain('Reclaim plain (eng) from hal@example.com?')
  })
})

// -- Settings › Profile ----------------------------------------------------------

function profileAccount(over: Json): Json {
  return {
    account_id: 'u-ada:main',
    label: 'main',
    relation: 'own',
    state: 'AVAILABLE',
    windows: {},
    lent_to_count: 0,
    stale: false,
    ...over,
  }
}

function profile(accounts: Json[]): Json {
  return {
    identity: {
      email: 'ada@example.com',
      display_name: null,
      display_name_suggested: null,
      domain: 'example.com',
      platform_role: null,
      is_admin: false,
      is_admin_unresolved: false,
    },
    workspaces: [
      {
        tenant_id: 'u-ada',
        display_name: 'Your workspace (u-ada)',
        kind: 'user',
        role: { name: 'owner', source: 'derived', via: null },
        state: 'ready',
        selectable: true,
        is_acting: true,
        is_default: true,
      },
    ],
    default_workspace: { tenant_id: 'u-ada', source: 'personal', ignored: null },
    connections: { github: [], typesafe: { personal: { set: false }, acting_workspace: { tenant_id: 'u-ada', set: false }, uses: 'user' } },
    notifications: { preferences: { muted: [], reminder: 'as_set' }, topics: ['approval'] },
    claude_accounts: { personal_workspace: 'u-ada', accounts, unreadable_document_count: 0 },
    revision: 1,
    unavailable: [],
  }
}

/**
 * The real read and the real normalisation: `normAccount` in `api.ts` carries
 * `display_name` (ACCOUNT-NAME-3), so nothing here puts the Name back.
 */
async function mountProfile(body: Json): Promise<HTMLElement> {
  serve((m, url) => (m === 'GET' && url === '/v1/profile' ? { status: 200, body } : null))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { ProfileScreen } = await import('../Profile')
  render(<ProfileScreen />)
  await screen.findByText('ada@example.com', undefined, WAIT)
  return screen.getByRole('region', { name: 'Claude accounts' })
}

describe("Settings › Profile names each of 'Your Claude accounts' by its Name", () => {
  it('shows a named account by its Name, and an unnamed one by its label as today', async () => {
    const region = await mountProfile(
      profile([
        profileAccount({ display_name: 'My main one' }),
        profileAccount({ account_id: 'eng:shared', label: 'shared', relation: 'borrowed', lent_to_count: null, display_name: null }),
        profileAccount({ account_id: 'eng:other', label: 'other', relation: 'borrowed', lent_to_count: null }),
      ]),
    )
    const names = [...region.querySelectorAll('li > b')].map((b) => b.textContent)
    // MUTATION: drop `display_name` from `normAccount` in api.ts (the page then prints `main`).
    // MUTATION: AccountRow back to `a.label ?? a.account_id`.
    expect(names[0]).toBe('My main one')
    // The label is not printed beside the Name, and no id appears where none was.
    expect(visible(region)).not.toMatch(/\bmain ·/)
    expect(visible(region)).not.toContain('u-ada:main')
    // MUTATION: serve the Name over an absent or null one as something other than the label.
    expect(names.slice(1)).toEqual(['shared', 'other'])
    expect(visible(region)).toMatch(/My main one · yours · AVAILABLE/)
  })
})
