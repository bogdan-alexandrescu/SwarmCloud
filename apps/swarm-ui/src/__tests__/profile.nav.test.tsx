// SETTINGS › PROFILE'S ADDRESS (lane PROFILE-NAV, part of #1505;
// docs/design/user-profile.md §2.1, §6). PROFILE-PAGE built the screen; this
// lane gives it `/settings/profile`, beside API keys, Workspace and MCP
// servers. The old `#settings/<anything else>` hashes still land on Pool
// limits: only this exact address is the new page.
//
// MUTATION: drop the `settings/profile` branch from App's `fromAddress` and
// every case goes red -- the address falls through to LEGACY's Pool limits,
// and the path to "No page at /settings/profile".
//
// Fixtures only; no network.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import { serve } from './repofixture'
import { addressToPath, pathToAddress } from '../paths'
import { canonical, fromAddress } from '../App'

const PATH = '/settings/profile'

function profileDoc() {
  return {
    identity: {
      email: 'ada@example.com',
      display_name: 'Ada Lovelace',
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
    connections: {
      github: [{ tenant_id: 'u-ada', state: 'not_connected' }],
      typesafe: { personal: { set: false }, acting_workspace: { tenant_id: 'u-ada', set: false }, uses: 'none' },
    },
    notifications: {
      preferences: { muted: [], reminder: 'as_set' },
      topics: ['approval', 'waiting_step', 'question', 'failed', 'finished', 'account', 'admin'],
    },
    claude_accounts: { personal_workspace: 'u-ada', accounts: [], unreadable_document_count: 0 },
    revision: 1,
    unavailable: [],
  }
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

describe('Settings › Profile', () => {
  it('is /settings/profile, both ways', () => {
    const p = pathToAddress(PATH)
    expect(p).not.toBeNull()
    const r = fromAddress(p!.address)
    expect(r.sectionId).toBe('settings')
    expect(r.tab).toBe('profile')
    expect(addressToPath(canonical(r))).toBe(PATH)
  })

  it('leaves API keys and the old hashes where they were; Workspace and MCP servers are Config\'s (CONFIG-NAV)', () => {
    expect(fromAddress('settings/keys')).toMatchObject({ sectionId: 'settings', tab: 'keys' })
    expect(fromAddress('settings/workspace')).toMatchObject({ sectionId: 'config', tab: 'workspace' })
    expect(fromAddress('settings/mcp-servers')).toMatchObject({ sectionId: 'config', tab: 'mcp-servers' })
    expect(fromAddress('settings/anything-else')).toMatchObject({ sectionId: 'admin', tab: 'limits' })
  })

  it('opens the Profile screen, under its own heading, at /settings/profile', async () => {
    window.history.replaceState(null, '', PATH)
    const calls = serve((m, url) => (m === 'GET' && url === '/v1/profile' ? { status: 200, body: profileDoc() } : null))
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { App } = await import('../App')
    render(<App />)
    const page = await screen.findByText('ada@example.com', { exact: false })
    const screenRoot = page.closest('.pf-page')
    expect(screenRoot, 'the Profile screen is not what /settings/profile drew').not.toBeNull()
    expect(within(screenRoot as HTMLElement).getByRole('heading', { level: 1, name: 'Profile' })).toBeTruthy()
    expect(screen.queryByText(/No page at/)).toBeNull()
    expect(window.location.pathname).toBe(PATH)
    expect(calls.filter((c) => c.method === 'GET' && c.url === '/v1/profile').length).toBeGreaterThan(0)
    expect(document.title).toBe('Profile · SwarmCloud')
  })
})
