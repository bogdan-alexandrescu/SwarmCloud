/**
 * CONFIG › WORKSPACE SETTINGS › VISUAL QA: WRITING FLOWS (part of #2688; BGD, 2026-11-05, "declared writes plus a
 * switch, on by default").
 *
 *   * The Visual QA card draws the workspace's `writing_flows` as one toggle, on by default, its reason in the (i)'s
 *     tooltip and no visible prose.
 *   * An admin (`can_edit`) turns it off through `PUT /v1/workspaces/{tenant_id}/qa-settings` `{writing_flows}`.
 *   * A member sees it disabled and sends nothing.
 *
 * Fixtures only; no network.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>

const WAIT = { timeout: 4000 }

const profile = (role: string): Json => ({
  identity: { email: 'ana.b@example.com', display_name: 'Ana', platform_role: null, is_admin: false },
  workspaces: [{ tenant_id: 'eng', display_name: 'Engineering', kind: 'group', role: { name: role, source: 'override' }, is_acting: true }],
  default_workspace: { tenant_id: 'eng', source: 'profile', ignored: null },
  connections: {},
  revision: 1,
  unavailable: [],
})

const settings = (writing: boolean, canEdit: boolean): Json => ({
  tenant_id: 'eng',
  qa_flows_per_step: 15,
  writing_flows: writing,
  defaults: { qa_flows_per_step: 15, writing_flows: true },
  bounds: { qa_flows_per_step: { min: 1, max: 30 } },
  set_at: null,
  set_by: null,
  can_edit: canEdit,
})

function api(canEdit: boolean) {
  return serve((m, url, body) => {
    if (m === 'GET' && url === '/v1/profile') return { status: 200, body: profile(canEdit ? 'admin' : 'member') }
    if (m === 'GET' && url === '/v1/qa-settings') return { status: 200, body: settings(true, canEdit) }
    if (m === 'PUT' && url === '/v1/workspaces/eng/qa-settings') {
      return { status: 200, body: settings((body as Json).writing_flows as boolean, true) }
    }
    return null
  })
}

async function mount() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { WorkspaceSettingsScreen } = await import('../WorkspaceSettings')
  render(<WorkspaceSettingsScreen part="settings" />)
  const heading = await screen.findByRole('heading', { name: 'Visual QA' }, WAIT)
  return heading.closest('section') as HTMLElement
}

const toggle = (card: HTMLElement) => card.querySelector<HTMLInputElement>('input[name="writing_flows"]')

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('Config › Workspace Settings: writing flows', () => {
  it('is one toggle, on by default, its reason in a tooltip, and an admin turns it off', async () => {
    const calls = api(true)
    const card = await mount()
    // MUTATION: drop QaWritingFlows from the Visual QA card and this is null.
    await waitFor(() => expect(toggle(card)?.checked).toBe(true), WAIT)
    expect(toggle(card)!.getAttribute('role')).toBe('switch')
    expect(toggle(card)!.disabled).toBe(false)
    const about = card.querySelector<HTMLButtonElement>('button[aria-label="About writing flows"]')
    expect(about).not.toBeNull()
    const tip = document.getElementById(about!.getAttribute('aria-describedby') ?? '')
    expect(tip?.textContent).toContain('only reads')
    fireEvent.click(toggle(card)!)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PUT').length).toBe(1), WAIT)
    const put = calls.find((c) => c.method === 'PUT')!
    // MUTATION: send the batch size with it, or another route, and this fails.
    expect(put.url).toBe('/v1/workspaces/eng/qa-settings')
    expect(put.body).toEqual({ writing_flows: false })
    await waitFor(() => expect(toggle(card)?.checked).toBe(false), WAIT)
    expect(visible(card)).toContain('Writing flows')
  })

  it('a member sees it disabled and sends nothing', async () => {
    const calls = api(false)
    const card = await mount()
    await waitFor(() => expect(toggle(card)?.checked).toBe(true), WAIT)
    // MUTATION: leave the toggle enabled for a member.
    expect(toggle(card)!.disabled).toBe(true)
    fireEvent.click(toggle(card)!)
    await new Promise((r) => setTimeout(r, 50))
    expect(calls.filter((c) => c.method === 'PUT').length).toBe(0)
  })
})
