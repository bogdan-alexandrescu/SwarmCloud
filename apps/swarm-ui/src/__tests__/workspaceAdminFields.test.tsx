/**
 * RENAME A WORKSPACE (S6a) AND NAME A NEW WORKSPACE'S FIRST ADMIN (S6c)
 * (WORKSPACE-ADMIN-FIELDS, part of #2157; docs/web-ui/mockups/config-e.html).
 *
 *   * Config › Workspace Settings › This workspace has a Rename button beside
 *     Name for the workspace's admins in a group workspace, and for nobody
 *     else. It sends `PUT /v1/workspaces/{tenant_id}/name` with the name and
 *     nothing else, draws the new name, keeps the id, and draws a refusal as
 *     the API's code and sentence.
 *   * The name is the one the shell draws (`/v1/tenants/me`), and a rename
 *     tells the shell, which reads it again: the header follows without a
 *     reload, and without blanking while it reads.
 *   * Add a new workspace's form asks a platform admin, on the group tab, for
 *     an optional first admin, posts it as `first_admin`, and says the role
 *     takes effect once they are in the group. A member reaches no form and
 *     no field.
 *
 * Fixtures only; no network. Nothing here is token-shaped.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>
type Calls = { method: string; url: string; body: unknown }[]

const WAIT = { timeout: 4000 }

const profile = (role: string, opts: { isAdmin?: boolean; acting?: 'eng' | 'u-ana' } = {}): Json => ({
  identity: { email: 'ana.b@example.com', display_name: 'Ana', platform_role: opts.isAdmin ? 'admin' : null, is_admin: opts.isAdmin === true },
  workspaces: [
    { tenant_id: 'u-ana', display_name: 'Your workspace (u-ana)', kind: 'user', role: { name: 'admin', source: 'personal' }, is_acting: opts.acting === 'u-ana' },
    { tenant_id: 'eng', display_name: 'eng@example.com', kind: 'group', role: { name: role, source: 'override' }, is_acting: opts.acting !== 'u-ana' },
  ],
  default_workspace: { tenant_id: 'eng', source: 'profile', ignored: null },
  connections: {},
  revision: 1,
  unavailable: [],
})

const me = (tenantId: string, name: string): Json => ({
  tenant: { tenant_id: tenantId, kind: 'group', principal: 'eng@example.com', display_name: name },
  principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: false },
  environment: 'dev',
  environment_declared: true,
})

function api(prof: Json, rename: (body: unknown) => { status: number; body: unknown } = (b) => ({
  status: 200,
  body: { tenant_id: 'eng', display_name: (b as { display_name: string }).display_name, previous_display_name: 'Engineering', changed: true },
})): Calls {
  return serve((m, url, body) => {
    if (m === 'GET' && url === '/v1/profile') return { status: 200, body: prof }
    if (m === 'GET' && url === '/v1/tenants/me') return { status: 200, body: me('eng', 'Engineering') }
    if (m === 'PUT' && url === '/v1/workspaces/eng/name') return rename(body)
    return null
  })
}

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../WorkspaceSettings')
}

const card = async () => (await screen.findByRole('heading', { name: 'This workspace' })).closest('section') as HTMLElement

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('Rename (S6a): the workspace’s admins only, the display name only', () => {
  it('draws the name the shell draws, and renames it through PUT /v1/workspaces/{id}/name, keeping the id', async () => {
    const calls = api(profile('admin'))
    const told: string[] = []
    const { WorkspaceSettingsScreen } = await load()
    const { WORKSPACE_RENAMED_EVENT } = await import('../api')
    const hear = (e: Event) => told.push(String((e as CustomEvent).detail))
    window.addEventListener(WORKSPACE_RENAMED_EVENT, hear)
    render(<WorkspaceSettingsScreen part="settings" />)
    const ws = await card()
    // MUTATION: draw the profile row's group address instead of `/v1/tenants/me`'s name -> fails.
    await waitFor(() => expect(visible(ws)).toContain('Engineering'), WAIT)
    // MUTATION: drop the Rename button -> fails.
    fireEvent.click(within(ws).getByRole('button', { name: 'Rename' }))
    const form = within(ws).getByRole('form', { name: 'Rename this workspace' })
    fireEvent.change(within(form).getByRole('textbox', { name: 'New name' }), { target: { value: '  Example Team ' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Save name' }))
    await waitFor(() => expect(within(ws).queryByRole('form')).toBeNull(), WAIT)
    const puts = calls.filter((c) => c.method === 'PUT')
    // MUTATION: send the id, the principal or anything but the trimmed name -> fails.
    expect(puts).toEqual([{ method: 'PUT', url: '/v1/workspaces/eng/name', body: { display_name: 'Example Team' } }])
    expect(visible(ws)).toContain('Example Team')
    // The id is drawn unchanged beside it.
    expect(within(ws).getByText('eng').tagName).toBe('CODE')
    // MUTATION: drop the event -> the shell never reads the new name, fails.
    expect(told).toEqual(['eng'])
    window.removeEventListener(WORKSPACE_RENAMED_EVENT, hear)
  })

  it('draws a refusal as the API’s code and sentence, and keeps the form', async () => {
    const message = "Only the workspace's admins, or a platform admin, may rename it. Nothing was changed."
    api(profile('admin'), () => ({ status: 403, body: { code: 'role_admin_required', message } }))
    const { WorkspaceSettingsScreen } = await load()
    render(<WorkspaceSettingsScreen part="settings" />)
    const ws = await card()
    fireEvent.click(await within(ws).findByRole('button', { name: 'Rename' }))
    fireEvent.change(within(ws).getByRole('textbox', { name: 'New name' }), { target: { value: 'Mine now' } })
    fireEvent.click(within(ws).getByRole('button', { name: 'Save name' }))
    const alert = await within(ws).findByRole('alert', {}, WAIT)
    expect(visible(alert)).toBe(`role_admin_required ${message}`)
    expect(within(ws).getByRole('form', { name: 'Rename this workspace' })).toBeTruthy()
    expect(visible(ws)).toContain('Engineering')
  })

  it('does not post an empty name', async () => {
    const calls = api(profile('admin'))
    const { WorkspaceSettingsScreen } = await load()
    render(<WorkspaceSettingsScreen part="settings" />)
    const ws = await card()
    fireEvent.click(await within(ws).findByRole('button', { name: 'Rename' }))
    fireEvent.change(within(ws).getByRole('textbox', { name: 'New name' }), { target: { value: '   ' } })
    const save = within(ws).getByRole('button', { name: 'Save name' }) as HTMLButtonElement
    expect(save.disabled).toBe(true)
    fireEvent.click(save)
    expect(calls.filter((c) => c.method === 'PUT')).toEqual([])
  })

  for (const role of ['member', 'viewer']) {
    it(`offers no Rename to a ${role}`, async () => {
      const calls = api(profile(role))
      const { WorkspaceSettingsScreen } = await load()
      render(<WorkspaceSettingsScreen part="settings" />)
      const ws = await card()
      await waitFor(() => expect(visible(ws)).toContain(role), WAIT)
      // MUTATION: draw Rename for everyone -> fails.
      expect(within(ws).queryByRole('button', { name: 'Rename' })).toBeNull()
      expect(calls.filter((c) => c.method === 'PUT')).toEqual([])
    })
  }

  it('offers Rename to a platform admin whose stored role is member, as the rename route admits them (VQ-044)', async () => {
    api(profile('member', { isAdmin: true }))
    const { WorkspaceSettingsScreen } = await load()
    render(<WorkspaceSettingsScreen part="settings" />)
    const ws = await card()
    // MUTATION: gate Rename on the stored role again -> fails.
    expect(await within(ws).findByRole('button', { name: 'Rename' }, WAIT)).toBeTruthy()
    expect(visible(within(ws).getByTestId('ws-your-role'))).toBe('admin (platform admin)')
  })

  it('offers no Rename in a personal workspace, which the switcher always calls Your workspace', async () => {
    api(profile('member', { acting: 'u-ana' }))
    const { WorkspaceSettingsScreen } = await load()
    render(<WorkspaceSettingsScreen part="settings" />)
    const ws = await card()
    await waitFor(() => expect(visible(ws)).toContain('u-ana'), WAIT)
    // MUTATION: drop the kind check -> fails.
    expect(within(ws).queryByRole('button', { name: 'Rename' })).toBeNull()
  })
})

describe('the shell reads the name again after a rename', () => {
  it('redraws the header’s workspace name, never blanking it while it reads', async () => {
    vi.stubEnv('VITE_LIVE', '1')
    vi.stubEnv('VITE_SWARM_ENV', 'dev')
    vi.resetModules()
    let name = 'Engineering'
    let mineReads = 0
    const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })
    globalThis.fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if ((init?.method ?? 'GET') === 'PUT' && url === '/v1/workspaces/eng/name') {
        name = 'Example Team'
        return Promise.resolve(json({ tenant_id: 'eng', display_name: name, previous_display_name: 'Engineering', changed: true }))
      }
      if (url.includes('/v1/tenants/mine')) {
        mineReads += 1
        return Promise.resolve(json([{ tenant_id: 'eng', display_name: 'eng@example.com', kind: 'group', via: 'member', role: { name: 'admin', source: 'override' } }]))
      }
      if (url.includes('/v1/tenants/me')) return Promise.resolve(json(me('eng', name)))
      return Promise.resolve(new Response('{}', { status: 503, headers: { 'content-type': 'application/json' } }))
    }) as unknown as typeof fetch
    const { SkyShell } = await import('../Spine')
    const { renameWorkspace } = await import('../api')
    const { container } = render(
      <SkyShell section="config" tab="workspace" title="Workspace Settings" go={() => {}} foot={null}>
        {null}
      </SkyShell>,
    )
    // The header prints the workspace's short key (`eng`, visual QA VQ-001);
    // its whole name is the label's title.
    const label = () => container.querySelector('.sk-pbar .sk-tn')?.getAttribute('title') ?? undefined
    await waitFor(() => expect(label()).toBe('Engineering'), WAIT)
    const before = mineReads
    // Every redraw from the rename on is watched: the name must never be gone.
    const drawn: (string | undefined)[] = []
    const watch = new MutationObserver(() => drawn.push(label()))
    watch.observe(container, { childList: true, subtree: true, characterData: true, attributes: true })
    await act(async () => {
      await renameWorkspace('eng', 'Example Team')
    })
    // MUTATION: drop the shell's listener -> the old name stays, fails.
    await waitFor(() => expect(label()).toBe('Example Team'), WAIT)
    watch.disconnect()
    // MUTATION: re-read with the loading reset -> the name is blank for a while, fails.
    expect(drawn.filter((d) => d === undefined)).toEqual([])
    expect(mineReads).toBeGreaterThan(before)
  })
})

describe('First admin (S6c): a platform admin’s field, on the group tab', () => {
  const WS_ID = 'w-4c1e0b'
  const created = (firstAdmin: Json | null): Json => ({
    workspace_id: WS_ID,
    kind: 'group',
    state: 'approved',
    principal: 'research@example.com',
    display_name: 'Research',
    steps: {},
    decision: { verdict: 'approved', reason: null, at: new Date().toISOString(), auto: true },
    ...(firstAdmin === null ? {} : { first_admin: firstAdmin }),
  })

  function admin(firstAdmin: Json | null): Calls {
    return serve((m, url) => {
      if (m === 'GET' && url === '/v1/profile') return { status: 200, body: profile('admin', { isAdmin: true }) }
      if (m === 'POST' && url === '/v1/admin/workspaces') return { status: 202, body: created(firstAdmin) }
      if (m === 'GET' && url === `/v1/admin/workspaces/${WS_ID}`) return { status: 200, body: { workspace: created(null) } }
      return null
    })
  }

  async function openForm() {
    const { WorkspaceSettingsScreen } = await load()
    render(<WorkspaceSettingsScreen part="new" />)
    // S6c (WS-SETTINGS-E-REST): the form is drawn on the page, in its New workspace card; no dialog to open.
    return (await screen.findByRole('heading', { name: 'New workspace' })).closest('section') as HTMLElement
  }

  it('posts first_admin with the group and says the role takes effect once they are in the group', async () => {
    const calls = admin({ email: 'frank@example.com', role: 'admin', granted: true, member: false })
    const dialog = await openForm()
    fireEvent.click(within(dialog).getByRole('tab', { name: 'Group' }))
    // MUTATION: drop the field -> fails.
    const field = within(dialog).getByRole('textbox', { name: 'First admin’s email address (optional)' })
    // MUTATION: drop the sentence before submit -> fails.
    expect(visible(within(dialog).getByTestId('new-workspace-first-admin-hint'))).toContain('takes effect once they are in the group')
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Group address' }), { target: { value: 'research@example.com' } })
    fireEvent.change(field, { target: { value: ' frank@example.com ' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create workspace' }))
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST')).toHaveLength(1), WAIT)
    // MUTATION: send the field under another name, or untrimmed -> fails.
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({
      kind: 'group',
      principal: 'research@example.com',
      display_name: null,
      first_admin: 'frank@example.com',
    })
    const said = await within(dialog).findByText(/is its first admin/, {}, WAIT)
    expect(visible(said)).toContain('frank@example.com is its first admin.')
    expect(visible(said)).toContain('not in research@example.com yet: the role takes effect once they are')
  })

  it('sends no first_admin when it is left empty, and offers none on the personal or service tab', async () => {
    const calls = admin(null)
    const dialog = await openForm()
    for (const tab of ['Personal', 'Service']) {
      fireEvent.click(within(dialog).getByRole('tab', { name: tab }))
      // MUTATION: draw the field on every tab -> fails.
      expect(within(dialog).queryByRole('textbox', { name: /First admin/ })).toBeNull()
    }
    fireEvent.click(within(dialog).getByRole('tab', { name: 'Group' }))
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Group address' }), { target: { value: 'research@example.com' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create workspace' }))
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST')).toHaveLength(1), WAIT)
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ kind: 'group', principal: 'research@example.com', display_name: null })
  })

  it('says so when the person was not made admin', async () => {
    admin({ email: 'frank@example.com', role: 'admin', granted: false, reason: 'workspace_exists' })
    const dialog = await openForm()
    fireEvent.click(within(dialog).getByRole('tab', { name: 'Group' }))
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Group address' }), { target: { value: 'research@example.com' } })
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'First admin’s email address (optional)' }), { target: { value: 'frank@example.com' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create workspace' }))
    const said = await within(dialog).findByText(/was not made its admin/, {}, WAIT)
    expect(visible(said)).toContain('this workspace already existed')
  })

  it('is not there for a member, who reaches no form at all', async () => {
    const calls = serve((m, url) => (m === 'GET' && url === '/v1/profile' ? { status: 200, body: profile('admin') } : null))
    const { WorkspaceSettingsScreen } = await load()
    render(<WorkspaceSettingsScreen part="new" />)
    await screen.findByText(/for platform admins/)
    // MUTATION: open the dialog for anyone -> the field is reachable, fails.
    expect(screen.queryByRole('button', { name: 'New workspace' })).toBeNull()
    expect(screen.queryByRole('textbox', { name: /First admin/ })).toBeNull()
    expect(calls.filter((c) => c.method === 'POST')).toEqual([])
  })
})
