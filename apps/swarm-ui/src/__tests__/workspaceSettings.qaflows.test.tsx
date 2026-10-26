/**
 * CONFIG › WORKSPACE SETTINGS › QA FLOWS PER STEP (lane QA-FLOWS-UI over
 * QA-FLOWS-CAP, part of #2136; BGD, 2026-10-26).
 *
 *   * The Settings part draws the workspace's `qa_flows_per_step`, read from
 *     `GET /v1/qa-settings`, with its 1-30 bound.
 *   * An admin (`can_edit`) changes it and saves it through
 *     `PUT /v1/workspaces/{tenant_id}/qa-settings`; a value outside 1-30 is
 *     never sent.
 *   * A member (`can_edit` false) sees it read-only: no field, no Save.
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

const settings = (value: number, canEdit: boolean, setBy: string | null = null): Json => ({
  tenant_id: 'eng',
  qa_flows_per_step: value,
  defaults: { qa_flows_per_step: 15 },
  bounds: { qa_flows_per_step: { min: 1, max: 30 } },
  set_at: setBy === null ? null : '2026-10-26T09:00:00+00:00',
  set_by: setBy,
  can_edit: canEdit,
})

function api(role: string, canEdit: boolean) {
  return serve((m, url, body) => {
    if (m === 'GET' && url === '/v1/profile') return { status: 200, body: profile(role) }
    if (m === 'GET' && url === '/v1/qa-settings') return { status: 200, body: settings(15, canEdit) }
    if (m === 'PUT' && url === '/v1/workspaces/eng/qa-settings') {
      return { status: 200, body: settings((body as Json).qa_flows_per_step as number, true, 'ana.b@example.com') }
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

const input = (card: HTMLElement) => card.querySelector<HTMLInputElement>('input[name="qa_flows_per_step"]')
const save = (card: HTMLElement) => Array.from(card.querySelectorAll<HTMLButtonElement>('button')).find((b) => visible(b) === 'Save')

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('Config › Workspace Settings: QA flows per step', () => {
  it('an admin saves it through the settings write', async () => {
    const calls = api('admin', true)
    const card = await mount()
    // MUTATION: drop the setting from the Settings part and this is null.
    await waitFor(() => expect(input(card)?.value).toBe('15'), WAIT)
    expect(input(card)!.min).toBe('1')
    expect(input(card)!.max).toBe('30')
    expect(visible(card)).toContain('QA flows per step')
    expect(visible(card)).toContain('1-30')
    fireEvent.change(input(card)!, { target: { value: '20' } })
    fireEvent.click(save(card)!)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PUT').length).toBe(1), WAIT)
    // MUTATION: PUT another route or body and this fails.
    const put = calls.find((c) => c.method === 'PUT')!
    expect(put.url).toBe('/v1/workspaces/eng/qa-settings')
    expect(put.body).toEqual({ qa_flows_per_step: 20 })
    await waitFor(() => expect(visible(card)).toContain('Saved.'), WAIT)
    expect(input(card)!.value).toBe('20')
  })

  it('an admin cannot save a value outside 1-30', async () => {
    const calls = api('admin', true)
    const card = await mount()
    await waitFor(() => expect(input(card)?.value).toBe('15'), WAIT)
    for (const bad of ['31', '0', '7.5']) {
      fireEvent.change(input(card)!, { target: { value: bad } })
      // MUTATION: leave Save enabled on an out-of-range value.
      expect(save(card)!.disabled, `Save with ${bad}`).toBe(true)
      // The page says why Save is dimmed: a red alert line naming the bound
      // (a hint recoloured by .ur-bad loses to .ur-hint's later colour).
      const alert = card.querySelector('form [role="alert"]')
      expect(alert, `alert with ${bad}`).not.toBeNull()
      expect(alert!.className).toContain('ur-bad')
      expect(alert!.textContent).toContain('1-30')
      fireEvent.click(save(card)!)
    }
    await new Promise((r) => setTimeout(r, 50))
    expect(calls.filter((c) => c.method === 'PUT').length).toBe(0)
    fireEvent.change(input(card)!, { target: { value: '20' } })
    expect(card.querySelector('form [role="alert"]'), 'no alert on an in-range value').toBeNull()
  })

  it('a member sees it read-only', async () => {
    const calls = api('member', false)
    const card = await mount()
    // MUTATION: draw the value nowhere for a member and this never appears.
    await waitFor(() => expect(card.querySelector('[data-qa-flows-per-step]')?.textContent).toContain('15'), WAIT)
    // MUTATION: draw the field and Save for a member too.
    expect(input(card)).toBeNull()
    expect(save(card)).toBeUndefined()
    expect(visible(card)).toContain('Only the workspace')
    expect(calls.filter((c) => c.method === 'PUT').length).toBe(0)
  })
})
