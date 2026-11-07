// CONFIG › PLUGINS (lane plugins-console-settings-page, part of #2688;
// docs/design/agent-plugins.md). The owner picked the settings table on
// 4 November 2026: a facts strip, the catalogue as a table whose row opens to
// its skills per mode, one Allowed here switch per plugin (the workspace
// allows whole plugins), and a save bar that signs the change through
// `PUT /v1/workspaces/{tenant_id}/plugins`.
//
// What no route serves is said as not known, never drawn as a value: what
// each skill does, the skills a plugin ships that no step can pick, the
// changes before the last one, and which steps name a plugin.
//
// MUTATIONS, each of which turns a case red: draw the switch live for
// `can_edit: false`; send anything but `{allowed}` on save; list steps the
// API never named; call an unverified empty list "Signed".
//
// Fixtures only; no network. Nothing here is credential-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>

const REASON = 'runs an interview with a person, which needs the question bridge; until it lands only unattended mode runs'

function catalogue(): Json[] {
  return [
    {
      name: 'gstack', version: '1.92.55.0', runner_profiles: ['claude-code', 'claude-code-large'],
      modes: {
        unattended: { skills: ['review', 'investigate', 'health'], available: true },
        planner: { skills: ['office-hours', 'spec'], available: false, reason: REASON },
        designer: { skills: ['design-html'], available: false, reason: REASON },
      },
    },
    {
      name: 'security-audit', version: 'c1c8a8c1471069fb0e188eeaff69b8e8db6564a8', runner_profiles: ['claude-code'],
      modes: { unattended: { skills: ['security-audit'], available: true } },
    },
  ]
}

function answer(over: Json = {}): Json {
  return {
    tenant_id: 'eng',
    plugins: { allowed: ['gstack'] },
    defaults: { plugins: { allowed: [] } },
    set_at: '2026-11-03T09:30:00+00:00',
    set_by: 'ana.b@example.com',
    can_edit: true,
    catalogue: catalogue(),
    ...over,
  }
}

function api(body: Json, write?: (m: string, url: string, body: unknown) => { status: number; body: unknown } | null) {
  return serve((m, url, b) => {
    if (m === 'GET' && url.startsWith('/v1/plugins')) return { status: 200, body }
    if (m !== 'GET' && write) return write(m, url, b)
    return null
  })
}

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../ConfigPlugins')
}

afterEach(() => {
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

describe('Config › Plugins', () => {
  it('lists the catalogue as a table: name, pinned version, runner profiles and skills per mode', async () => {
    api(answer())
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    const table = await screen.findByRole('table', { name: 'The plugin catalogue' })
    const rows = within(table).getAllByRole('row')
    const gstack = rows.find((r) => visible(r).includes('gstack'))!
    expect(visible(gstack)).toContain('1.92.55.0')
    expect(visible(gstack)).toContain('built into the images')
    expect(visible(gstack)).toContain('claude-code-large')
    expect(visible(gstack)).toMatch(/Unattended 3/)
    expect(visible(gstack)).toMatch(/Planner 2 · not yet/)
    expect(visible(table)).toContain('security-audit')
  })

  it('says what the workspace may use, the default, who changed it last and that you can change it', async () => {
    api(answer())
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    const strip = await screen.findByRole('list', { name: 'The setting' })
    const text = visible(strip)
    expect(text).toMatch(/This workspace may use\s*gstack/)
    expect(text).toMatch(/When nobody has chosen\s*No plugin allowed/)
    // A non-empty served list was read through the signature check.
    expect(text).toMatch(/Setting\s*Signed/)
    expect(text).toContain('ana.b@example.com')
    expect(text).toMatch(/You\s*Can change it/)
  })

  it('does not call an empty list signed: an unsigned list also reads as none', async () => {
    api(answer({ plugins: { allowed: [] } }))
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    const strip = await screen.findByRole('list', { name: 'The setting' })
    expect(visible(strip)).toMatch(/This workspace may use\s*No plugin/)
    expect(visible(strip)).toMatch(/Signed: not known/)
  })

  it('says a never-changed setting is the default, not a change by nobody', async () => {
    api(answer({ plugins: { allowed: [] }, set_at: null, set_by: null }))
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    const strip = await screen.findByRole('list', { name: 'The setting' })
    expect(visible(strip)).toMatch(/Last change\s*Never changed/)
    expect(visible(strip)).toMatch(/Setting\s*Not set: the default applies/)
  })

  it('opens the first row to its three modes, saying why planner and designer do not run, and what is not served', async () => {
    api(answer())
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    // The first plugin's row is open on arrival, as the picked frame draws it.
    expect((await screen.findByRole('button', { name: 'Close gstack' })).getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByRole('button', { name: 'Open security-audit' }).getAttribute('aria-expanded')).toBe('false')
    const unattended = screen.getByRole('region', { name: 'gstack, Unattended mode' })
    expect(visible(unattended)).toContain('Runs today')
    expect(visible(unattended)).toContain('investigate')
    // What each skill does is not served: said as not known, never invented.
    expect(visible(unattended)).toMatch(/what each skill does: not known/i)
    const planner = screen.getByRole('region', { name: 'gstack, Planner mode' })
    expect(visible(planner)).toContain('Not yet')
    expect(visible(planner)).toContain(`gstack ${REASON}.`)
    const never = screen.getByRole('region', { name: 'gstack, never offered' })
    expect(visible(never)).toMatch(/not known/i)
    expect(visible(document.body)).toMatch(/earlier changes: not served/i)
  })

  it('says a mode a plugin offers nothing in, in words', async () => {
    api(answer())
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open security-audit' }))
    const designer = screen.getByRole('region', { name: 'security-audit, Designer mode' })
    expect(visible(designer)).toContain('Offers no skill in this mode')
  })

  it('is read only for a member: the switches are inert and no save bar is drawn', async () => {
    api(answer({ can_edit: false }))
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    const sw = await screen.findByRole('switch', { name: 'Allow gstack in this workspace' })
    expect((sw as HTMLInputElement).disabled).toBe(true)
    expect(visible(document.body)).toMatch(/only this workspace’s owner or an admin/i)
    expect(visible(screen.getByRole('list', { name: 'The setting' }))).toMatch(/You\s*Read only/)
    fireEvent.click(sw)
    expect(screen.queryByRole('region', { name: 'Changes not saved' })).toBeNull()
  })

  it('names a switch-off in the save bar, in check_step’s words, without listing steps no route names', async () => {
    api(answer())
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    fireEvent.click(await screen.findByRole('switch', { name: 'Allow gstack in this workspace' }))
    const bar = screen.getByRole('region', { name: 'Changes not saved' })
    const text = visible(bar)
    expect(text).toContain('1 change not saved')
    // The arrow is drawn; a screen reader hears "to".
    expect(text).toMatch(/gstack: allowed here → to not allowed/)
    expect(text).toContain('steps that name gstack are refused')
    expect(text).toContain('“this workspace has not allowed gstack: its owner or admin allows it in the workspace’s plugins setting”')
    expect(text).toMatch(/which steps name it is not known here/i)
    fireEvent.click(within(bar).getByRole('button', { name: 'Discard' }))
    expect(screen.queryByRole('region', { name: 'Changes not saved' })).toBeNull()
  })

  it('saves and signs with one PUT of `{allowed}` to the workspace’s own route, then says it is signed', async () => {
    const seen: Array<{ m: string; url: string; body: unknown }> = []
    api(answer(), (m, url, body) => {
      seen.push({ m, url, body })
      return { status: 200, body: answer({ plugins: { allowed: ['gstack', 'security-audit'] }, set_at: '2026-11-04T10:00:00+00:00', set_by: 'ana.b@example.com' }) }
    })
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    fireEvent.click(await screen.findByRole('switch', { name: 'Allow security-audit in this workspace' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save and sign' }))
    await waitFor(() => expect(seen).toHaveLength(1))
    expect(seen[0]).toEqual({ m: 'PUT', url: '/v1/workspaces/eng/plugins', body: { allowed: ['gstack', 'security-audit'] } })
    await screen.findByText('Saved and signed')
    expect(screen.queryByRole('region', { name: 'Changes not saved' })).toBeNull()
    expect(visible(screen.getByRole('list', { name: 'The setting' }))).toMatch(/This workspace may use\s*gstack\s*security-audit/)
  })

  it('says a save the API stored but could not sign allows nothing', async () => {
    api(answer({ plugins: { allowed: [] } }), () => ({ status: 200, body: answer({ plugins: { allowed: [] } }) }))
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    fireEvent.click(await screen.findByRole('switch', { name: 'Allow gstack in this workspace' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save and sign' }))
    const banner = await screen.findByText('Saved, not signed')
    expect(visible(banner.closest('[role]') ?? document.body)).toMatch(/allows nothing/)
  })

  it('says a refused save in the API’s words and keeps the change on the page', async () => {
    api(answer(), () => ({ status: 403, body: { code: 'role_admin_required', message: 'Only the workspace’s owner or admin, or a platform admin, may change its plugins. Nothing was changed.' } }))
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    fireEvent.click(await screen.findByRole('switch', { name: 'Allow gstack in this workspace' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save and sign' }))
    await waitFor(() => expect(visible(document.body)).toMatch(/Nothing was changed/))
    expect(screen.getByRole('region', { name: 'Changes not saved' })).toBeTruthy()
  })
})
