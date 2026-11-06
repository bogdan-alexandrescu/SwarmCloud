// QA2B-G (visual QA pass 2b, part of #2694): the pass-2 patches for
// WorkspaceSettings.tsx, applied.
//
//   * admin-shell-21 (QA2-FIX-04): This workspace named a personal workspace
//     by its stored display name ('Ana', the email) where the switcher says
//     `Your workspace`. It now says `Your workspace`, the id on its own row.
//   * config2-03 (QA2-FIX-15): the Settings lead said '3 of 4 done' beside a
//     strip counting its own steps. It names the list it counts: 'Setup
//     checklist: 3 of 4 done'.
//
// Fixtures only; no network. Nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>

const step = (name: string, state: string, evidence: Json = {}): Json => ({ step: name, state, code: null, copy: null, checked_at: null, evidence, issues: [] })

const HELD: Json = {
  tenant_id: 'u-ana',
  user: 'ana.b@example.com',
  user_hash: 'h',
  derived_at: new Date().toISOString(),
  steps: [
    step('signed_in', 'done'),
    step('github_connected', 'done', { login: 'octo-dev' }),
    step('repos_chosen', 'done', { repositories: ['a', 'b'] }),
    step('orgs_enabled', 'failed'),
  ],
  next_step: 'orgs_enabled',
  complete: false,
  dismissed: false,
  source: 'derived',
}

const PROFILE: Json = {
  identity: { email: 'ana.b@example.com', display_name: 'Ana', platform_role: null, is_admin: false },
  workspaces: [
    { tenant_id: 'u-ana', display_name: 'ana.b@example.com', kind: 'user', role: { name: 'admin', source: 'personal' }, is_acting: true },
    { tenant_id: 'eng', display_name: 'Engineering', kind: 'group', role: { name: 'member', source: 'override' }, is_acting: false },
  ],
  default_workspace: { tenant_id: 'u-ana', source: 'profile', ignored: null },
  connections: {},
  revision: 1,
  unavailable: [],
}

const ME: Json = {
  tenant: { tenant_id: 'u-ana', kind: 'user', display_name: 'Ana' },
  principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: false },
  environment: 'dev',
  environment_declared: true,
}

async function settings() {
  serve((m, url) => {
    if (m !== 'GET') return null
    if (url === '/v1/profile') return { status: 200, body: PROFILE }
    if (url === '/v1/onboarding') return { status: 200, body: HELD }
    if (url.startsWith('/v1/tenants/me')) return { status: 200, body: ME }
    if (url === '/v1/workspace/roles') return { status: 404, body: { detail: 'Not Found' } }
    if (url === '/v1/code-intelligence') return { status: 404, body: { detail: 'Not Found' } }
    if (url === '/v1/judgment') return { status: 404, body: { detail: 'Not Found' } }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const [{ WorkspaceSettingsScreen }, { SetupContext }] = await Promise.all([import('../WorkspaceSettings'), import('../ConfigSetup')])
  render(
    <SetupContext.Provider value={{ doc: { status: 'ok', fetchedAt: Date.now(), data: HELD as never }, personal: true }}>
      <WorkspaceSettingsScreen part="settings" />
    </SetupContext.Provider>,
  )
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

describe('admin-shell-21: This workspace names a personal workspace `Your workspace`', () => {
  it('says the switcher’s name, never the stored one, with the id on its own row', async () => {
    await settings()
    const ws = (await screen.findByRole('heading', { name: 'This workspace' })).closest('section') as HTMLElement
    const name = () => visible(within(ws).getByText('Name').nextElementSibling)
    // The `/v1/tenants/me` read names it 'Ana'; once it lands the name still holds.
    await waitFor(() => expect(visible(ws)).toContain('u-ana'))
    await new Promise((r) => setTimeout(r, 50))
    // MUTATION: `renamed ?? stored ?? ws?.display_name` for a personal one again -> 'Ana' or the email: red.
    expect(name()).toBe('Your workspace')
    expect(visible(within(ws).getByText('Id').nextElementSibling)).toBe('u-ana')
  })
})

describe('config2-03: the Settings lead names the checklist it counts', () => {
  it('says "Setup checklist: 3 of 4 done", not a bare count beside the strip’s', async () => {
    await settings()
    const lead = (await screen.findByRole('region', { name: 'Setup’s next step' })) as HTMLElement
    // MUTATION: `body={countLabel(d)}` again -> '3 of 4 done' alone: red.
    expect(visible(lead)).toContain('Setup checklist: 3 of 4 done')
  })
})
