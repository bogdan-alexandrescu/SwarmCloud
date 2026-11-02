/**
 * VISUAL QA PASS 1, LANE QA1-FIX-31 (part of #2596 and #2391; BGD 2026-10-28).
 *
 *   * admin-shell-23: a role refusal said in place of an Admin page
 *     (`AdminRoleRefusal`), naming the page, the role and the workspace, with
 *     a link to Overview. The frame mounting it is Spine.tsx's change.
 *   * config-29: People and roles in a personal workspace offers a way on --
 *     Workspace Settings, and Add a new workspace for a platform admin.
 *   * config-30: an editor's Visual QA card draws QA flows per step once, as
 *     the field, with its default noted once.
 *   * config-31: a card whose route is not served draws no refresh beside
 *     "Not served yet".
 *
 * Fixtures only; no network.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>

const WAIT = { timeout: 4000 }

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

describe('admin-shell-23: an Admin page refused by role', () => {
  it('names the page, the role and the workspace, and links to Overview', async () => {
    const { AdminRoleRefusal } = await import('../WorkspaceRefusal')
    const { container } = render(<AdminRoleRefusal page="Pool limits" role="member" workspace="Bogdan" />)
    // MUTATION: drop AdminRoleRefusal (or its title) and this is not found.
    expect(screen.getByText('Pool limits is for platform admins')).toBeTruthy()
    // MUTATION: say the role without its article ("You are member") and this fails.
    expect(visible(container)).toContain('You are a member of Bogdan.')
    const link = screen.getByRole('link', { name: 'Open Overview' })
    // MUTATION: point the link anywhere but Overview and this fails.
    expect(link.getAttribute('href')).toBe('/overview')
  })

  it('says an unread role as unread, never a guessed one', async () => {
    const { AdminRoleRefusal } = await import('../WorkspaceRefusal')
    const { container } = render(<AdminRoleRefusal page="People and roles" role={null} workspace="Engineering" />)
    expect(screen.getByText('People and roles is for platform admins')).toBeTruthy()
    // MUTATION: default an unread role to "member" and this fails.
    expect(visible(container)).toContain('your role in Engineering was not read')
    expect(visible(container)).not.toContain('a member')
  })
})

const PERSONAL: Json = {
  tenant_id: 'u-carol',
  kind: 'user',
  default_role: null,
  default_source: null,
  me: { email: 'carol@example.com', role: 'admin', source: 'personal', via: 'personal' },
  people: [],
  roles: [],
  can_manage: false,
  audit: [],
}

const me = (isAdmin: boolean): Json => ({
  tenant: { tenant_id: 'u-carol', kind: 'user', display_name: 'Carol' },
  principal: { email: 'carol@example.com', domain: 'example.com', groups: [], is_admin: isAdmin },
  environment: 'dev',
  environment_declared: true,
})

async function personalPeople(isAdmin: boolean) {
  serve((m, url) => {
    if (m === 'GET' && url === '/v1/workspace/roles') return { status: 200, body: PERSONAL }
    if (m === 'GET' && url.startsWith('/v1/tenants/me')) return { status: 200, body: me(isAdmin) }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { WorkspacePeopleScreen } = await import('../WorkspacePeople')
  render(<WorkspacePeopleScreen />)
  await screen.findByText(/^You are this workspace.s admin; a personal workspace has only you\.$/, undefined, WAIT)
}

describe('config-29: People and roles in a personal workspace', () => {
  it('links back to Workspace Settings, and offers no Add to a non-admin', async () => {
    await personalPeople(false)
    // MUTATION: draw the personal card as the bare sentence again and this is not found.
    const back = await screen.findByRole('link', { name: 'Open Workspace Settings' }, WAIT)
    expect(back.getAttribute('href')).toBe('/config/workspace')
    // MUTATION (visual QA pass 2, config2-21): put back 'a platform admin adds one' -- adds a person, or a
    // workspace? -- and this is not found.
    expect(screen.getByText('Only a group workspace has people and roles; a platform admin creates group workspaces.')).toBeTruthy()
    expect(screen.queryByText(/adds one/)).toBeNull()
    // MUTATION: offer Add a new workspace to everyone and this fails.
    expect(screen.queryByRole('link', { name: 'Add a new workspace' })).toBeNull()
  })

  it('offers a platform admin Add a new workspace', async () => {
    await personalPeople(true)
    // MUTATION: gate Add on anything but `principal.is_admin` and this is never drawn.
    const add = await screen.findByRole('link', { name: 'Add a new workspace' }, WAIT)
    expect(add.getAttribute('href')).toBe('/config/workspace?part=new')
    // MUTATION (config2-21): put back 'You may add one.' and what the admin may add is unnamed again.
    expect(screen.getByText('Only a group workspace has people and roles; as a platform admin, you may create one.')).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Open Workspace Settings' })).toBeTruthy()
  })
})

const profile: Json = {
  identity: { email: 'ana.b@example.com', display_name: 'Ana', platform_role: null, is_admin: false },
  workspaces: [{ tenant_id: 'eng', display_name: 'Engineering', kind: 'group', role: { name: 'admin', source: 'override' }, is_acting: true }],
  default_workspace: { tenant_id: 'eng', source: 'profile', ignored: null },
  connections: {},
  revision: 1,
  unavailable: [],
}

const qaSettings = (canEdit: boolean): Json => ({
  tenant_id: 'eng',
  qa_flows_per_step: 15,
  defaults: { qa_flows_per_step: 15 },
  bounds: { qa_flows_per_step: { min: 1, max: 30 } },
  set_at: null,
  set_by: null,
  can_edit: canEdit,
})

/** The Settings part with `/v1/qa-settings` answering `qa` and `/v1/budget` not served (404 with no code). */
async function settingsPage(qa: Json) {
  serve((m, url) => {
    if (m === 'GET' && url === '/v1/profile') return { status: 200, body: profile }
    if (m === 'GET' && url === '/v1/qa-settings') return { status: 200, body: qa }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { WorkspaceSettingsScreen } = await import('../WorkspaceSettings')
  render(<WorkspaceSettingsScreen part="settings" />)
}

async function card(name: string) {
  const heading = await screen.findByRole('heading', { name }, WAIT)
  return heading.closest('section') as HTMLElement
}

describe('config-30: QA flows per step drawn once', () => {
  it("an editor sees the field alone, with the default noted once", async () => {
    await settingsPage(qaSettings(true))
    const qa = await card('Visual QA')
    await waitFor(() => expect(qa.querySelector<HTMLInputElement>('input[name="qa_flows_per_step"]')?.value).toBe('15'), WAIT)
    // MUTATION: draw the <dl> read-out above the form again and both fail.
    expect(qa.querySelector('[data-qa-flows-per-step]')).toBeNull()
    expect(visible(qa)).not.toContain('(the default)')
    expect(visible(qa).match(/QA flows per step/g)?.length).toBe(1)
    expect(visible(qa).match(/default is 15/g)?.length).toBe(1)
  })

  it('a reader still sees the figure with its default', async () => {
    await settingsPage(qaSettings(false))
    const qa = await card('Visual QA')
    await waitFor(() => expect(qa.querySelector('[data-qa-flows-per-step]')?.textContent).toBe('15 (the default)'), WAIT)
  })
})

describe('config-31: no refresh on a route the build does not serve', () => {
  it('the Per-agent budget card says Not served yet with no refresh, and a served card keeps its own', async () => {
    await settingsPage(qaSettings(true))
    const budget = await card('Per-agent budget')
    await waitFor(() => expect(within(budget).getByText('Not served yet')).toBeTruthy(), WAIT)
    // MUTATION: draw UrRefresh in the card head whatever the read answered and this fails.
    expect(budget.querySelector('.c-refresh')).toBeNull()
    const qa = await card('Visual QA')
    await waitFor(() => expect(qa.querySelector('input[name="qa_flows_per_step"]')).not.toBeNull(), WAIT)
    // MUTATION: drop the refresh from every card and this fails.
    expect(qa.querySelector('.c-refresh')).not.toBeNull()
  })
})
