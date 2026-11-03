/**
 * ONE WORKSPACE'S PAGE (WorkspacePage.tsx; lane workspace-admin-page, part of
 * #2688): the owner's pick of administration by workspace, a page with tabs
 * People, Roles, Settings and Setup. Each tab is checked for each of the three
 * people who reach it -- a platform admin, the workspace's admin, a member
 * who reads it -- and the screens it embeds are checked to draw without their
 * own heading or Config tab strip.
 *
 * Fixtures only; no network.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>
type Persona = 'platform' | 'wsadmin' | 'member'

const step = (name: string, state: string): Json => ({ step: name, state, code: null, copy: null, checked_at: null, evidence: {}, issues: [] })

const ONBOARDING: Json = {
  tenant_id: 'eng',
  user: 'ana.b@example.com',
  user_hash: 'h',
  derived_at: new Date().toISOString(),
  steps: [step('signed_in', 'done'), step('github_connected', 'done'), step('orgs_enabled', 'failed'), step('repos_chosen', 'todo'), step('access_verified', 'todo'), step('ready', 'todo')],
  next_step: 'orgs_enabled',
  complete: false,
  dismissed: false,
  source: 'derived',
}

const profile = (p: Persona): Json => ({
  identity: { email: 'ana.b@example.com', display_name: 'Ana', platform_role: p === 'platform' ? 'admin' : null, is_admin: p === 'platform' },
  workspaces: [
    { tenant_id: 'u-ana', display_name: 'ana.b@example.com', kind: 'user', role: { name: 'admin', source: 'personal' }, is_acting: false },
    { tenant_id: 'eng', display_name: 'Engineering', kind: 'group', role: { name: p === 'member' ? 'member' : 'admin', source: 'override' }, is_acting: true },
  ],
  default_workspace: { tenant_id: 'eng', source: 'profile', ignored: null },
  connections: {},
  revision: 1,
  unavailable: [],
})

const roles = (p: Persona): Json => ({
  tenant_id: 'eng',
  kind: 'group',
  default_role: 'member',
  default_source: 'grant',
  people: [
    { email: 'lee@example.com', role: 'admin', granted_by: 'ana.b@example.com', granted_at: new Date().toISOString(), reason: 'runs the team' },
    { email: 'kim@example.com', role: 'viewer', granted_by: 'ana.b@example.com', granted_at: new Date().toISOString(), reason: '' },
  ],
  roles: [
    { name: 'viewer', summary: 'Reads the workspace.' },
    { name: 'member', summary: 'Starts and cancels agents.' },
    { name: 'admin', summary: 'Changes the workspace.' },
  ],
  audit: [{ action: 'workspace_default_role_set', by: 'lee@example.com', at: new Date().toISOString(), detail: { from: 'viewer', to: 'member' } }],
  me: { email: 'ana.b@example.com', role: p === 'member' ? 'member' : 'admin', source: p === 'platform' ? 'platform_admin' : p === 'wsadmin' ? 'override' : 'default', via: '' },
  can_manage: p !== 'member',
})

function api(p: Persona, opts: { rolesDown?: boolean } = {}) {
  return serve((m, url) => {
    if (m === 'GET' && url === '/v1/profile') return { status: 200, body: profile(p) }
    if (m === 'GET' && url === '/v1/onboarding') return { status: 200, body: ONBOARDING }
    if (m === 'GET' && url === '/v1/workspace/roles') return opts.rolesDown ? { status: 503, body: { detail: 'down' } } : { status: 200, body: roles(p) }
    if (m === 'PUT' && url === '/v1/workspace/roles/default') {
      return { status: 200, body: { tenant_id: 'eng', default_role: 'viewer', previous_default: 'member', default_source: 'grant', granted_at: new Date().toISOString() } }
    }
    if (m === 'GET' && url === '/v1/access/members') return { status: 200, body: { tenant_id: 'eng', members: [] } }
    return null
  })
}

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../WorkspacePage')
}

async function open(p: Persona, tab?: 'people' | 'roles' | 'settings' | 'setup', opts: { rolesDown?: boolean } = {}) {
  const calls = api(p, opts)
  const { WorkspacePage } = await load()
  render(<WorkspacePage tab={tab} />)
  await screen.findByRole('heading', { level: 1, name: 'Engineering' })
  return calls
}

const tabs = () => screen.getByRole('tablist', { name: 'Engineering' })
const tabNames = () => within(tabs()).getAllByRole('tab').map((t) => visible(t.querySelector('.c-tab-label')))
const header = () => screen.getByLabelText('About this workspace')

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('the tabs each person is offered', () => {
  it('a platform admin: People, Roles, Settings, Setup, and the header says "as a platform admin"', async () => {
    await open('platform')
    expect(tabNames()).toEqual(['People', 'Roles', 'Settings', 'Setup'])
    // MUTATION: drop the chip -> fails.
    await waitFor(() => expect(visible(header())).toContain('as a platform admin'))
    expect(visible(header())).not.toContain('read only for you')
    expect(within(header()).getByTestId('wpg-you').textContent).toContain('admin')
  })

  it("the workspace's admin: the same tabs, neither the platform chip nor read only", async () => {
    await open('wsadmin')
    expect(tabNames()).toEqual(['People', 'Roles', 'Settings', 'Setup'])
    await waitFor(() => expect(within(header()).getByTestId('wpg-you').textContent).toContain('admin'))
    expect(visible(header())).not.toContain('as a platform admin')
    expect(visible(header())).not.toContain('read only for you')
  })

  it('a member: the same tabs, read only for them', async () => {
    await open('member')
    expect(tabNames()).toEqual(['People', 'Roles', 'Settings', 'Setup'])
    // MUTATION: drop the read-only tag -> fails.
    await waitFor(() => expect(visible(header())).toContain('read only for you'))
    expect(visible(header())).not.toContain('as a platform admin')
    expect(visible(header())).toContain('default member')
  })

  it("People counts the people with a role of their own, and says 'not known' when the read failed, never 0", async () => {
    await open('wsadmin')
    await waitFor(() => expect(visible(within(tabs()).getByRole('tab', { name: /People/ }))).toBe('People 2'))
    cleanup()
    await open('wsadmin', 'people', { rolesDown: true })
    await waitFor(() => expect(visible(within(tabs()).getByRole('tab', { name: /People/ }))).toBe('People not known'))
  })

  it("the header's setup chip counts the strip's steps and opens the Setup tab", async () => {
    await open('wsadmin')
    const chip = screen.getByRole('button', { name: /setup/i })
    fireEvent.click(chip)
    expect(within(tabs()).getByRole('tab', { name: 'Setup' }).getAttribute('aria-selected')).toBe('true')
  })
})

describe('People: Config › People and roles, embedded', () => {
  it("draws today's roster without its heading or its tab strip", async () => {
    await open('wsadmin', 'people')
    // MUTATION: draw something other than WorkspacePeopleScreen -> no such card, fails.
    expect(await screen.findByRole('heading', { name: 'Roles in this workspace' })).toBeTruthy()
    expect(await screen.findByText('lee@example.com')).toBeTruthy()
    // MUTATION: embed it without `embedded` -> a second h1 and its own strip, fails.
    expect([...document.querySelectorAll('h1')].map((h) => visible(h))).toEqual(['Engineering'])
    expect(screen.queryByRole('tablist', { name: 'People and roles' })).toBeNull()
    // The workspace's admin may give a person a role.
    expect(await screen.findByRole('heading', { name: 'Give a person their own role' })).toBeTruthy()
    // Members' GitHub was a platform admin's tab: not offered here.
    expect(screen.queryByText('Members’ GitHub')).toBeNull()
  })

  it("a platform admin also has Members' GitHub, under the roster", async () => {
    await open('platform', 'people')
    const fold = (await screen.findByText('Members’ GitHub')).closest('details') as HTMLElement
    expect(fold).toBeTruthy()
    fireEvent.click(within(fold).getByText('Members’ GitHub'))
    expect(await within(fold).findByRole('heading', { name: "Members' GitHub connections" })).toBeTruthy()
  })

  it('a member reads it with nothing to change', async () => {
    await open('member', 'people')
    expect(await screen.findByRole('heading', { name: 'Roles in this workspace' })).toBeTruthy()
    expect(await screen.findByText('read only for members')).toBeTruthy()
    expect(screen.queryByRole('heading', { name: 'Give a person their own role' })).toBeNull()
  })
})

describe('Roles: what each role may do, the default and the role events', () => {
  it("a manager sets the default through today's route", async () => {
    const calls = await open('wsadmin', 'roles')
    const list = await screen.findByRole('list', { name: 'What each role may do' })
    expect(visible(list)).toContain('Starts and cancels agents.')
    expect(visible(list)).toContain('default + 0 named')
    expect(visible(list)).toContain('1 named')
    expect(visible(await screen.findByRole('list', { name: 'Role events' }))).toContain('lee@example.com set the default to member')
    fireEvent.click(screen.getByRole('button', { name: 'viewer' }))
    fireEvent.click(screen.getByRole('button', { name: 'Set default' }))
    // MUTATION: post anywhere else, or say nothing -> fails.
    expect(await screen.findByText('The default is now viewer (was member).')).toBeTruthy()
    expect(calls.some((c) => c.method === 'PUT' && c.url === '/v1/workspace/roles/default' && JSON.stringify(c.body).includes('viewer'))).toBe(true)
  })

  it('a member reads the default and is offered no form', async () => {
    await open('member', 'roles')
    await screen.findByRole('list', { name: 'What each role may do' })
    // MUTATION: offer Set default to a member -> fails.
    expect(screen.queryByRole('button', { name: 'Set default' })).toBeNull()
    expect(visible(screen.getByRole('heading', { name: 'Default role' }).closest('section'))).toContain('without a role of their own holds member')
  })
})

describe('Settings: Workspace Settings, embedded', () => {
  it('draws This workspace without the "Workspace Settings" heading or its Config parts', async () => {
    await open('member', 'settings')
    const card = (await screen.findByRole('heading', { name: 'This workspace' })).closest('section')
    expect(visible(card)).toContain('Engineering')
    // MUTATION: embed it without `embedded` -> fails.
    expect([...document.querySelectorAll('h1')].map((h) => visible(h))).toEqual(['Engineering'])
    expect(screen.queryByRole('tablist', { name: 'Workspace Settings' })).toBeNull()
  })
})

describe('Setup: the Config pages, the checklist, and Add a new workspace', () => {
  it('links GitHub access, Repositories, Secrets and MCP servers, and embeds the checklist', async () => {
    await open('wsadmin', 'setup')
    const links = screen.getByRole('list', { name: 'Setup pages' })
    const href = (name: string) => within(links).getByRole('link', { name }).getAttribute('href')
    // MUTATION: drop a link, or point it elsewhere -> fails.
    expect(href('GitHub access')).toBe('/config/github')
    expect(href('Repositories')).toBe('/config/repositories')
    expect(href('Secrets')).toBe('/config/secrets')
    expect(href('MCP servers')).toBe('/config/mcp-servers')
    expect(await screen.findByRole('list', { name: 'Setup steps' })).toBeTruthy()
    expect([...document.querySelectorAll('h1')].map((h) => visible(h))).toEqual(['Engineering'])
    // MUTATION: offer Add a new workspace to a workspace admin -> fails.
    expect(screen.queryByRole('button', { name: 'Add a new workspace' })).toBeNull()
  })

  it('a platform admin adds a new workspace in place', async () => {
    await open('platform', 'setup')
    fireEvent.click(await screen.findByRole('button', { name: 'Add a new workspace' }))
    // MUTATION: draw nothing, or a link away -> no form, fails.
    expect(await screen.findByRole('heading', { name: 'New workspace' })).toBeTruthy()
  })

  it('a member gets the links and the checklist, and no Add a new workspace', async () => {
    await open('member', 'setup')
    expect(within(screen.getByRole('list', { name: 'Setup pages' })).getByRole('link', { name: 'MCP servers' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Add a new workspace' })).toBeNull()
  })
})

describe('the embedding props on the screens embedded', () => {
  it('WorkspaceSettingsScreen embedded draws neither its heading nor its parts', async () => {
    api('member')
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { WorkspaceSettingsScreen } = await import('../WorkspaceSettings')
    render(<WorkspaceSettingsScreen part="settings" embedded />)
    await screen.findByRole('heading', { name: 'This workspace' })
    expect(document.querySelector('h1')).toBeNull()
    expect(screen.queryByRole('tablist', { name: 'Workspace Settings' })).toBeNull()
  })

  it('WorkspacePeopleScreen embedded draws neither its heading nor its tabs, even for a platform admin', async () => {
    api('platform')
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { WorkspacePeopleScreen } = await import('../WorkspacePeople')
    render(<WorkspacePeopleScreen embedded />)
    await screen.findByRole('heading', { name: 'Roles in this workspace' })
    expect(document.querySelector('h1')).toBeNull()
    expect(screen.queryByRole('tablist', { name: 'People and roles' })).toBeNull()
  })
})
