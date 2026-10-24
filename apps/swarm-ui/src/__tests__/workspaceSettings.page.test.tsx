/**
 * CONFIG › WORKSPACE SETTINGS, ONE PAGE WITH PARTS (WS-SETTINGS-PAGE; part of
 * #2157, BGD's decisions of 2026-10-23 and his pick of proposal E,
 * docs/web-ui/mockups/config-e.html S6a-S6f).
 *
 *   * Settings: the workspace's settings, as Settings › Workspace had them
 *     (code intelligence), and the workspace it is about.
 *   * Workspace setup: Work › Setup's checklist, mounted as a part of this
 *     page rather than as a page of its own, so it draws no second heading.
 *     Its Connect GitHub, Enable orgs and Choose repositories steps link to
 *     Config › GitHub access.
 *   * Add a new workspace: a platform admin's part only (BGD,
 *     2026-10-23T23:57Z), the same rule and the same route as Admin › Tenants ›
 *     New workspace, `POST /v1/admin/workspaces`. A member sees no such part.
 *
 * Fixtures only; no network.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>
type Calls = { method: string; url: string; body: unknown }[]

const step = (name: string, state: string, evidence: Json = {}): Json => ({ step: name, state, code: null, copy: null, checked_at: null, evidence, issues: [] })

/** Nothing connected yet: every GitHub step has its action to offer. */
const ONBOARDING: Json = {
  tenant_id: 'eng',
  user: 'ana.b@example.com',
  user_hash: 'h',
  derived_at: new Date().toISOString(),
  steps: [
    step('signed_in', 'done', { email: 'ana.b@example.com', tenant_id: 'eng' }),
    step('github_connected', 'todo'),
    step('orgs_enabled', 'todo'),
    step('repos_chosen', 'todo'),
    step('access_verified', 'todo'),
    step('ready', 'todo'),
  ],
  next_step: 'github_connected',
  complete: false,
  dismissed: false,
  source: 'derived',
}

const CODE_INTEL: Json = {
  tenant_id: 'eng',
  can_edit: true,
  jev: false,
  context: 'inherit',
  context_ab: 'inherit',
  defaults: { jev: false, context: 'on', context_ab: 'ab' },
  updated_at: null,
  updated_by: null,
  repositories: [],
}

const profile = (isAdmin: boolean): Json => ({
  identity: { email: 'ana.b@example.com', display_name: 'Ana', platform_role: isAdmin ? 'admin' : null, is_admin: isAdmin },
  workspaces: [
    { tenant_id: 'u-ana', display_name: 'ana.b@example.com', kind: 'user', role: { name: 'admin', source: 'personal' }, is_acting: false },
    { tenant_id: 'eng', display_name: 'Engineering', kind: 'group', role: { name: isAdmin ? 'admin' : 'member', source: 'override' }, is_acting: true },
  ],
  default_workspace: { tenant_id: 'eng', source: 'profile', ignored: null },
  connections: {},
  revision: 1,
  unavailable: [],
})

const WS_ID = 'research'
const created = (): Json => ({
  workspace_id: WS_ID,
  kind: 'group',
  state: 'approved',
  principal: 'research@example.com',
  display_name: null,
  steps: {},
  decision: { verdict: 'approved', reason: null, at: new Date().toISOString(), auto: true },
})

function api(isAdmin: boolean): Calls {
  return serve((m, url) => {
    if (m === 'GET' && url === '/v1/profile') return { status: 200, body: profile(isAdmin) }
    if (m === 'GET' && url === '/v1/onboarding') return { status: 200, body: ONBOARDING }
    if (m === 'GET' && url === '/v1/code-intelligence') return { status: 200, body: CODE_INTEL }
    if (m === 'GET' && url === '/v1/judgment') return { status: 404, body: { detail: 'Not Found' } }
    if (m === 'POST' && url === '/v1/admin/workspaces') return { status: 201, body: created() }
    if (m === 'GET' && url === `/v1/admin/workspaces/${WS_ID}`) return { status: 200, body: { workspace: created() } }
    return null
  })
}

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../WorkspaceSettings')
}

const parts = () => screen.getByRole('navigation', { name: 'Workspace Settings' })
const stepEl = (name: string) => document.querySelector(`[data-step="${name}"]`) as HTMLElement

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('Config › Workspace Settings: its settings and its Workspace setup part', () => {
  it('draws the workspace\'s settings under one heading, with Workspace setup a part of the page', async () => {
    api(false)
    const { WorkspaceSettingsScreen } = await load()
    render(<WorkspaceSettingsScreen part="settings" />)
    // MUTATION: drop the Code intelligence card from the Settings part -> fails.
    expect(await screen.findByRole('switch')).toBeTruthy()
    // MUTATION: retitle the page or draw no h1 -> fails.
    expect(visible(document.querySelector('h1'))).toBe('Workspace Settings')
    // MUTATION: drop the This workspace card, or name a workspace the person is not acting in -> fails.
    const ws = (await screen.findByRole('heading', { name: 'This workspace' })).closest('section')
    expect(visible(ws)).toContain('Engineering')
    expect(visible(ws)).toContain('eng')
    expect(visible(ws)).toContain('member')
    // MUTATION: leave Workspace setup out of the page's parts, or point it at the old Work › Setup -> fails.
    const setup = within(parts()).getByRole('link', { name: 'Workspace setup' })
    expect(setup.getAttribute('href')).toBe('/config/workspace/setup')
    expect(within(parts()).getByRole('link', { name: 'Settings' }).getAttribute('aria-current')).toBe('page')
  })

  it('draws the setup checklist as its Workspace setup part, with no heading of its own', async () => {
    api(false)
    const { WorkspaceSettingsScreen } = await load()
    render(<WorkspaceSettingsScreen part="setup" />)
    // MUTATION: render Settings for the setup part, or drop the checklist -> fails.
    expect(await screen.findByRole('list', { name: 'Setup steps' })).toBeTruthy()
    // MUTATION: mount OnboardingScreen whole, with its own "Setup" h1 -> two headings, fails.
    const h1s = [...document.querySelectorAll('h1')].map((h) => visible(h))
    expect(h1s).toEqual(['Workspace Settings'])
    // MUTATION: mark the wrong part current -> fails.
    expect(within(parts()).getByRole('link', { name: 'Workspace setup' }).getAttribute('aria-current')).toBe('page')
    // MUTATION: keep the old head's "Access" button -> fails (it named a page that is GitHub access now).
    expect(screen.queryByRole('link', { name: 'Access' })).toBeNull()
  })
})

describe('each part\'s lead agrees with the checklist under it', () => {
  // Rendered review, dark-phone and light-desktop Workspace setup: the lead
  // read only the strip's two measured steps and said "No measured step is
  // left" over a checklist whose Enable orgs had failed.
  const FAILED: Json = { ...ONBOARDING, steps: [step('signed_in', 'done'), step('github_connected', 'done'), step('repos_chosen', 'done'), step('orgs_enabled', 'failed')], next_step: 'orgs_enabled' }

  async function withRead(part: 'settings' | 'setup') {
    api(false)
    const [{ WorkspaceSettingsScreen }, { SetupContext }] = await Promise.all([load(), import('../ConfigSetup')])
    render(
      <SetupContext.Provider value={{ doc: { status: 'ok', fetchedAt: Date.now(), data: FAILED as never }, personal: false }}>
        <WorkspaceSettingsScreen part={part} />
      </SetupContext.Provider>,
    )
  }

  it('leads Workspace setup with the checklist\'s next step, and sends it to GitHub access', async () => {
    await withRead('setup')
    // MUTATION: lead from the strip's measured steps alone -> "No measured step is left", fails.
    const lead = await screen.findByRole('region', { name: 'Step 4 of 4 · next' })
    expect(visible(lead)).toContain('Enable orgs: failed')
    expect(within(lead).getByRole('link', { name: 'Open GitHub access' }).getAttribute('href')).toBe('/config/github')
  })

  it('leads Settings with the same next step, never "Nothing here needs you" while a step has failed', async () => {
    await withRead('settings')
    const lead = await screen.findByRole('region', { name: 'Setup’s next step' })
    // MUTATION: call setup done while the checklist has a failed step -> fails.
    expect(visible(lead)).toContain('Enable orgs: failed')
    expect(screen.queryByText('Nothing here needs you')).toBeNull()
    expect(within(lead).getByRole('link', { name: 'Open Workspace setup' }).getAttribute('href')).toBe('/config/workspace/setup')
  })
})

describe('Workspace setup\'s GitHub steps link to Config › GitHub access', () => {
  it('Connect GitHub, Enable orgs and Choose repositories each link to /config/github', async () => {
    api(false)
    const { WorkspaceSettingsScreen } = await load()
    render(<WorkspaceSettingsScreen part="setup" />)
    await screen.findByRole('list', { name: 'Setup steps' })
    // MUTATION: keep Connect GitHub as a button that starts the App authorisation in place -> no link, fails.
    expect(within(stepEl('github_connected')).getByRole('link', { name: 'Connect GitHub' }).getAttribute('href')).toBe('/config/github')
    // MUTATION: point Enable orgs anywhere but GitHub access -> fails.
    expect(within(stepEl('orgs_enabled')).getByRole('link', { name: 'Enable orgs' }).getAttribute('href')).toBe('/config/github')
    // MUTATION: point Choose repositories anywhere but GitHub access -> fails.
    expect(within(stepEl('repos_chosen')).getByRole('link', { name: 'Choose repositories' }).getAttribute('href')).toBe('/config/github')
  })

  it('names GitHub access, not Access or Work › Setup, in the setup copy', async () => {
    api(false)
    const { WorkspaceSettingsScreen } = await load()
    render(<WorkspaceSettingsScreen part="setup" />)
    await screen.findByRole('list', { name: 'Setup steps' })
    const text = visible(document.body)
    // MUTATION: leave "Work › Setup" or "on Access" in the part's copy -> fails.
    expect(text).not.toMatch(/Work › Setup|on Access\b/)
  })
})

describe('Add a new workspace: a platform admin\'s part only', () => {
  it('is a part of the page for a platform admin, and files through POST /v1/admin/workspaces once', async () => {
    const calls = api(true)
    const { WorkspaceSettingsScreen } = await load()
    render(<WorkspaceSettingsScreen part="new" />)
    // MUTATION: drop the admin's third part -> fails.
    const tab = await within(parts()).findByRole('link', { name: 'Add a new workspace' })
    expect(tab.getAttribute('href')).toBe('/config/workspace?part=new')
    expect(tab.getAttribute('aria-current')).toBe('page')
    // MUTATION: open a form of this page's own, or none -> the existing dialog's fields are missing, fails.
    fireEvent.click(await screen.findByRole('button', { name: 'New workspace' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('tab', { name: 'Group' }))
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Group address' }), { target: { value: 'research@example.com' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create workspace' }))
    // MUTATION: post to a route of this page's own, or post twice -> fails.
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST').length).toBe(1))
    const post = calls.find((c) => c.method === 'POST')!
    expect(post.url).toBe('/v1/admin/workspaces')
    expect(post.body).toEqual({ kind: 'group', principal: 'research@example.com', display_name: null })
  })

  it('is absent for a member, who cannot reach it by its address either', async () => {
    const calls = api(false)
    const { WorkspaceSettingsScreen } = await load()
    render(<WorkspaceSettingsScreen part="new" />)
    // The profile read must have landed before absence means anything.
    await waitFor(() => expect(calls.some((c) => c.url === '/v1/profile')).toBe(true))
    await screen.findByText(/for platform admins/)
    // MUTATION: show the part to everyone -> fails.
    expect(within(parts()).queryByRole('link', { name: 'Add a new workspace' })).toBeNull()
    // MUTATION: draw the New workspace button for a member who types the address -> fails.
    expect(screen.queryByRole('button', { name: 'New workspace' })).toBeNull()
    expect(calls.filter((c) => c.method === 'POST')).toEqual([])
  })

  it('is absent from the parts while the profile is unread', async () => {
    globalThis.fetch = vi.fn((input: RequestInfo | URL) =>
      String(input) === '/v1/profile' ? new Promise<Response>(() => {}) : Promise.resolve(new Response('{}', { status: 404 })),
    ) as unknown as typeof fetch
    const { WorkspaceSettingsScreen } = await load()
    render(<WorkspaceSettingsScreen part="settings" />)
    // MUTATION: draw the part before the read says the caller is a platform admin -> fails.
    expect(within(parts()).queryByRole('link', { name: 'Add a new workspace' })).toBeNull()
  })
})

describe('the Add a new workspace address', () => {
  it('routes /config/workspace?part=new to Workspace Settings with the part named', async () => {
    vi.resetModules()
    const [App, paths] = await Promise.all([import('../App'), import('../paths')])
    const p = paths.pathToAddress('/config/workspace', '?part=new')
    expect(p?.address).toBe('config/workspace?part=new')
    const r = App.fromAddress(p!.address)
    // MUTATION: drop `new` from the parts the route accepts -> view is lost, fails.
    expect(r.sectionId).toBe('config')
    expect(r.tab).toBe('workspace')
    expect(r.view).toBe('part=new')
    expect(paths.addressToPath(App.canonical(r))).toBe('/config/workspace?part=new')
  })
})
