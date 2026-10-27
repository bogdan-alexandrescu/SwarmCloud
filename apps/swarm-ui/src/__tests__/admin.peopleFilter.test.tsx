// ADMIN › PEOPLE AND ROLES, THE GRID'S OWN CONTROLS (ADMIN-PEOPLE-FILTER;
// part of #2157, proposal E frames S5c and S5f, config-e.html).
//
// WHAT EACH CASE HOLDS:
//   * the workspace filter sits in the grid card's head: a platform admin's
//     "Every workspace" and one choice per workspace; choosing one narrows the
//     grid to its column and to the people who hold a role there, and the
//     choice is remembered on this device -- and a storage that throws (a
//     private window) costs the memory, never the page;
//   * a workspace admin's filter offers only the workspaces they administer,
//     "Both" when there are two (S5f);
//   * "Find a person" matches a name or an email in the loaded grid, counts
//     what it matched, and says so when nothing does;
//   * Recent admin actions starts folded, its head counting this week's
//     entries, and opens on press;
//   * Grant admin is in the grid card's head (S5c), checked and confirmed as
//     the Admins card's is;
//   * with a workspace chosen, the head's count is the chosen workspace's, of
//     the total, never the whole platform's above a narrowed grid (review);
//   * the filter's segmented control shrinks inside the card: the shared
//     `.c-seg` is `flex: none`, which pushed it past a 390px viewport (review).
//
// Placeholders only; nothing here is token-shaped.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }

type Json = Record<string, unknown>

const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString()
const DAY_MIN = 24 * 60

function person(email: string, roles: Json, over: Json = {}): Json {
  return {
    email,
    display_name: null,
    teams: [],
    github: 'connected',
    workspace: { state: 'none', setup_url: '/setup#workspace', setup_command: '/sc:setup' },
    claude_account: { source: 'none', own: 0, lent: 0, lent_by: [], provider_key: false },
    loan_request: null,
    first_seen: ago(600),
    last_seen: ago(1),
    last_submitted: null,
    last_active: ago(1),
    roles,
    ...over,
  }
}

const COLUMNS = [
  { tenant_id: 'eng', group: 'eng@example.com', display_name: 'Engineering', default_role: 'member', unavailable: false },
  { tenant_id: 'labs', group: 'labs@example.com', display_name: 'Labs', default_role: 'viewer', unavailable: false },
  { tenant_id: 'pod', group: 'pod@example.com', display_name: 'Research Pod', default_role: 'viewer', unavailable: false },
]

const BOB = person('bob@example.com', { eng: 'admin', labs: 'viewer', pod: 'none' }, { display_name: 'Robert Stone' })
const FAY = person('fay@example.com', { eng: 'member', labs: 'none', pod: 'none' })
const LEE = person('lee@example.com', { eng: 'none', labs: 'member', pod: 'member' })

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
    admins: [{ email: 'bob@example.com', role: 'admin', granted_by: 'owner@example.com', granted_at: ago(60) }],
    admins_error: null,
    ...over,
  }
}

const SCOPED: Json = {
  scope: 'workspaces',
  workspaces: [
    { ...COLUMNS[0], display_name: 'Team' },
    { ...COLUMNS[1] },
  ],
  workspaces_error: null,
  people: [
    { email: 'bob@example.com', display_name: 'Robert Stone', roles: { eng: 'admin', labs: 'none' }, last_active: ago(3) },
    { email: 'lee@example.com', display_name: null, roles: { eng: 'none', labs: 'member' }, last_active: null },
  ],
  count: 2,
  pending: 0,
}

async function mount(body: Json, writes: Record<string, () => { status: number; body: unknown }> = {}) {
  const calls = serve((m, url) => {
    if (m === 'GET' && url === '/v1/admin/people') return { status: 200, body }
    return writes[`${m} ${url}`]?.() ?? null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { PeopleScreen } = await import('../People')
  const utils = render(<PeopleScreen />)
  await screen.findByRole('heading', { name: 'People and roles', level: 1 }, WAIT)
  await waitFor(() => expect(document.querySelector('.pp-people tbody tr')).not.toBeNull(), WAIT)
  return { ...utils, calls }
}

const grid = () => document.querySelector<HTMLElement>('.pp-people')!
const head = () => grid().querySelector<HTMLElement>('.c-card-h')!
const heads = () => [...grid().querySelectorAll<HTMLElement>('thead th')].map((th) => th.textContent?.trim())
const emails = () => [...grid().querySelectorAll<HTMLElement>('tbody tr')].map((tr) => tr.dataset.email)
const filter = () => within(head()).getByRole('group', { name: 'Workspace' })
const choices = () => within(filter()).getAllByRole('button').map((b) => b.textContent)
const pressed = () => within(filter()).getAllByRole('button').find((b) => b.getAttribute('aria-pressed') === 'true')?.textContent
const find = () => within(grid()).getByRole('searchbox', { name: 'Find a person' })

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  try {
    window.localStorage.clear()
  } catch {
    // jsdom's storage; nothing to clear.
  }
})

describe('the workspace filter (S5c, S5f)', () => {
  it("is in the grid card's head, Every workspace and one choice per workspace, and narrows columns and rows", async () => {
    await mount(platform([BOB, FAY, LEE]))
    // MUTATION: draw the filter above the cards, outside the grid's head -> fails.
    expect(choices()).toEqual(['Every workspace', 'Engineering', 'Labs', 'Research Pod'])
    expect(pressed()).toBe('Every workspace')
    expect(emails()).toEqual(['bob@example.com', 'fay@example.com', 'lee@example.com'])
    fireEvent.click(within(filter()).getByRole('button', { name: 'Labs' }))
    // MUTATION: narrow the rows only, keeping every column -> the heads fail.
    expect(heads()).toEqual(['Person', 'Platform role', 'Labs', 'Own workspace', 'Claude account', 'Ceiling', 'Last active', 'Manage'])
    // fay holds no role in Labs; bob (viewer) and lee (member) do.
    // MUTATION: count `none` as being in the workspace -> fay stays and this fails.
    expect(emails()).toEqual(['bob@example.com', 'lee@example.com'])
    expect(pressed()).toBe('Labs')
  })

  it('is remembered on this device', async () => {
    const first = await mount(platform([BOB, FAY, LEE]))
    fireEvent.click(within(filter()).getByRole('button', { name: 'Research Pod' }))
    expect(emails()).toEqual(['lee@example.com'])
    first.unmount()
    // MUTATION: keep the choice in component state only -> the new mount opens on Every workspace.
    await mount(platform([BOB, FAY, LEE]))
    expect(pressed()).toBe('Research Pod')
    expect(emails()).toEqual(['lee@example.com'])
  })

  it('opens on Every workspace when storage throws, and still narrows', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('storage is off')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('storage is off')
    })
    await mount(platform([BOB, FAY, LEE]))
    expect(pressed()).toBe('Every workspace')
    fireEvent.click(within(filter()).getByRole('button', { name: 'Engineering' }))
    expect(emails()).toEqual(['bob@example.com', 'fay@example.com'])
  })

  it('opens on Every workspace when the remembered workspace is no longer served', async () => {
    window.localStorage.setItem('swarm.people.workspace', 'gone')
    await mount(platform([BOB, FAY, LEE]))
    expect(pressed()).toBe('Every workspace')
    expect(emails()).toHaveLength(3)
  })

  it("offers a workspace admin only the workspaces they administer, Both when there are two", async () => {
    await mount(SCOPED)
    // MUTATION: offer the platform's "Every workspace" here -> fails.
    expect(choices()).toEqual(['Both', 'Team', 'Labs'])
    expect(heads()).toEqual(['Person', 'Team', 'Labs', 'Last active'])
    fireEvent.click(within(filter()).getByRole('button', { name: 'Team' }))
    expect(heads()).toEqual(['Person', 'Team', 'Last active'])
    expect(emails()).toEqual(['bob@example.com'])
  })
})

describe("the grid head's count follows the filter (review)", () => {
  const chip = () => head().querySelector<HTMLElement>('.c-chip')!.textContent?.replace(/\s+/g, ' ').trim()

  it("says the chosen workspace's count of the platform's, for a platform admin", async () => {
    await mount(platform([BOB, FAY, LEE], { pending: 1 }))
    expect(chip()).toBe('3 people · 1 pending')
    fireEvent.click(within(filter()).getByRole('button', { name: 'Labs' }))
    // MUTATION: keep `doc.count` in the head while a workspace is chosen -> '3 people · 1 pending'.
    expect(chip()).toBe('2 of 3 people in Labs')
    fireEvent.click(within(filter()).getByRole('button', { name: 'Every workspace' }))
    expect(chip()).toBe('3 people · 1 pending')
  })

  it("says the chosen workspace's count of theirs, for a workspace admin", async () => {
    await mount(SCOPED)
    expect(chip()).toBe('2 people')
    fireEvent.click(within(filter()).getByRole('button', { name: 'Team' }))
    // MUTATION: keep `doc.count` here -> '2 people' above one row.
    expect(chip()).toBe('1 of 2 people in Team')
  })

  it('counts the workspace, not what Find a person narrowed it to', async () => {
    await mount(platform([BOB, FAY, LEE]))
    fireEvent.click(within(filter()).getByRole('button', { name: 'Engineering' }))
    fireEvent.change(find(), { target: { value: 'fay' } })
    expect(emails()).toEqual(['fay@example.com'])
    expect(chip()).toBe('2 of 3 people in Engineering')
  })
})

describe("the filter stays inside the card on a phone (review)", () => {
  const css = readFileSync(join(__dirname, '..', 'styles', 'people.css'), 'utf8')
  const rule = (css.match(/\.pp-filter\s*>\s*\.c-seg\s*\{([^}]*)\}/) ?? [])[1] ?? ''

  it('lets the segmented control shrink beside its label, overriding the shared flex: none', () => {
    // jsdom lays nothing out, so the rule is read. Without it, `.c-seg { flex: none }`
    // held the control at full width beside "Workspace" and it ended at x=427 on a 390px phone.
    // MUTATION: drop `flex: 1 1 auto` or `min-width: 0` -> fails.
    expect(rule).toMatch(/flex:\s*1 1 auto/)
    expect(rule).toMatch(/min-width:\s*0/)
    expect(css).toMatch(/\.pp-filter\s*\{[^}]*min-width:\s*0[^}]*max-width:\s*100%/)
  })
})

describe('Find a person', () => {
  it('matches by name and by email, and counts the matches', async () => {
    await mount(platform([BOB, FAY, LEE]))
    // MUTATION: match the email only -> "robert" finds nobody and this fails.
    fireEvent.change(find(), { target: { value: 'Robert' } })
    expect(emails()).toEqual(['bob@example.com'])
    expect(visible(grid())).toContain('1 person matches “Robert”')
    fireEvent.change(find(), { target: { value: 'FAY@' } })
    expect(emails()).toEqual(['fay@example.com'])
    fireEvent.change(find(), { target: { value: 'example.com' } })
    expect(visible(grid())).toContain('3 people match “example.com”')
  })

  it('says so when nothing matches, and draws no rows', async () => {
    await mount(platform([BOB, FAY, LEE]))
    fireEvent.change(find(), { target: { value: 'zed' } })
    expect(emails()).toEqual([])
    expect(visible(grid())).toContain('Nobody matches “zed”')
  })

  it('searches within the chosen workspace, for a workspace admin too', async () => {
    await mount(SCOPED)
    fireEvent.click(within(filter()).getByRole('button', { name: 'Team' }))
    fireEvent.change(find(), { target: { value: 'lee' } })
    expect(emails()).toEqual([])
    expect(visible(grid())).toContain('Nobody in Team matches “lee”')
  })
})

describe('Recent admin actions', () => {
  it("starts folded with this week's count in its head, and opens on press", async () => {
    const audit = [
      { action: 'approve', target_workspace_id: 'w-3f9a2c', by: 'bob@example.com', at: ago(30), detail: {} },
      { action: 'deny', target_workspace_id: 'w-4e0b3d', by: 'bob@example.com', at: ago(2 * DAY_MIN), detail: {} },
      { action: 'grant_admin', target_email: 'fay@example.com', by: 'bob@example.com', at: ago(10 * DAY_MIN), detail: {} },
    ]
    await mount(platform([BOB], { audit }))
    const card = document.querySelector<HTMLElement>('.pp-audit')!
    const toggle = within(card).getByRole('button', { name: /Recent admin actions/ })
    // MUTATION: open the card by default -> the list is drawn and this fails.
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(card.querySelector('.pp-audit-list')).toBeNull()
    // MUTATION: count every entry -> "3 this week".
    expect(visible(card.querySelector('.c-card-h'))).toContain('2 this week')
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(visible(card.querySelector('.pp-audit-list'))).toContain('Workspace approved w-3f9a2c by bob@example.com')
  })
})

describe('Grant admin in the grid head (S5c)', () => {
  it('asks for one email address, then confirms before it grants', async () => {
    const { calls } = await mount(platform([BOB, FAY]), {
      'PUT /v1/admin/admins/fay%40example.com': () => ({ status: 200, body: { email: 'fay@example.com', role: 'admin', changed: true } }),
    })
    // MUTATION: leave Grant admin in the Admins card only -> fails.
    fireEvent.click(within(head()).getByRole('button', { name: 'Grant admin' }))
    const dialog = await screen.findByRole('dialog', { name: 'Grant admin' }, WAIT)
    const field = within(dialog).getByLabelText('Email') as HTMLInputElement
    const next = within(dialog).getByRole('button', { name: 'Grant admin' }) as HTMLButtonElement
    fireEvent.change(field, { target: { value: 'not-an-email' } })
    expect(next.disabled, 'Grant admin is live for not-an-email').toBe(true)
    fireEvent.change(field, { target: { value: 'fay@example.com' } })
    fireEvent.submit(field.form!)
    await waitFor(() => expect(visible(dialog)).toContain('Make fay@example.com a platform admin?'), WAIT)
    expect(calls.filter((c) => c.method !== 'GET')).toHaveLength(0)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Grant admin' }))
    await waitFor(() => expect(calls.filter((c) => c.method !== 'GET')).toHaveLength(1), WAIT)
    expect(calls.filter((c) => c.method !== 'GET')[0]!.url).toBe('/v1/admin/admins/fay%40example.com')
    await waitFor(() => expect(visible(grid())).toContain('fay@example.com is now an admin.'), WAIT)
  })

  it('is not offered to a workspace admin, who cannot grant platform admin', async () => {
    await mount(SCOPED)
    expect(within(head()).queryByRole('button', { name: 'Grant admin' })).toBeNull()
  })
})
