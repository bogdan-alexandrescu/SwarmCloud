// SETTINGS › PROFILE: A GROUP WORKSPACE IS NAMED, NOT ADDRESSED (lane
// ROLE-EFFECTIVE-CELL, part of #2391; BGD 2026-10-26, VQ-056).
//
// `GET /v1/profile` names a group workspace with no stored name by its group
// email ('eng@…'), so Profile read 'eng@… · admin (platform admin)' where the
// switcher read 'Engineering'. The page now uses the switcher's rule
// (`nameTenants` / `displayNameOf`): `/v1/tenants/mine`'s stored name, an
// address counting as no name, else the workspace's id. The personal
// workspace keeps its own name, and the platform admin's reach entries follow
// the same rule.
//
// Fixtures only; no network; nothing token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>

function group(tenant_id: string, over: Json = {}): Json {
  return {
    tenant_id,
    display_name: `${tenant_id}@example.com`,
    kind: 'group',
    role: { name: 'member', source: 'derived', via: null },
    state: null,
    selectable: true,
    is_acting: false,
    is_default: false,
    ...over,
  }
}

const PROFILE: Json = {
  identity: {
    email: 'ops@example.com',
    display_name: 'Ops',
    display_name_suggested: null,
    domain: 'example.com',
    platform_role: 'admin',
    is_admin: true,
    is_admin_unresolved: false,
  },
  workspaces: [
    {
      tenant_id: 'u-ops',
      display_name: 'Your workspace (u-ops)',
      kind: 'user',
      role: { name: 'admin', source: 'derived', via: null },
      state: 'ready',
      selectable: true,
      is_acting: false,
      is_default: false,
    },
    group('eng', { is_acting: true }),
    group('smoke'),
  ],
  default_workspace: { tenant_id: 'eng', source: 'profile', ignored: null },
  connections: {
    github: [{ tenant_id: 'eng', state: 'not_connected' }],
    typesafe: { personal: null, acting_workspace: { tenant_id: 'eng', set: false }, uses: 'none' },
  },
  notifications: { preferences: { muted: [], reminder: 'as_set' }, topics: ['approval'] },
  claude_accounts: { personal_workspace: 'u-ops', accounts: [], unreadable_document_count: 0 },
  revision: 1,
  unavailable: [],
}

// The switcher's read: eng has a stored name, smoke none, labs is reached only as a platform admin.
const MINE = [
  { tenant_id: 'u-ops', display_name: 'Your workspace (u-ops)', kind: 'user', via: 'personal' },
  { tenant_id: 'eng', display_name: 'Engineering', group: 'eng@example.com', kind: 'group', via: 'member', role: { name: 'admin', source: 'platform_admin' } },
  { tenant_id: 'smoke', display_name: 'smoke@example.com', kind: 'group', via: 'member' },
  { tenant_id: 'labs', display_name: 'labs@example.com', kind: 'group', via: 'platform_admin' },
]

async function drawn() {
  serve((m, url) => {
    if (m === 'GET' && url === '/v1/profile') return { status: 200, body: PROFILE }
    if (m === 'GET' && url === '/v1/tenants/mine') return { status: 200, body: MINE }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { ProfileScreen } = await import('../Profile')
  render(<ProfileScreen />)
  await screen.findByText('ops@example.com')
}

const row = (tenant: string) => document.querySelector<HTMLElement>(`ul[aria-label="Your workspaces"] li[data-tenant="${tenant}"]`)

afterEach(() => {
  vi.unstubAllEnvs()
  window.localStorage.clear()
  window.sessionStorage.clear()
})

describe('Profile names each group workspace as the switcher does (VQ-056)', () => {
  it('draws eng by its stored name and smoke, which has none, by its id', async () => {
    await drawn()
    // MUTATION: draw `display_name` as /v1/profile served it -> 'eng@example.com', and this fails.
    await waitFor(() => expect(row('eng')?.querySelector('b')?.textContent).toBe('Engineering'))
    expect(visible(row('eng'))).toContain('Engineering · admin (platform admin)')
    expect(row('smoke')?.querySelector('b')?.textContent).toBe('smoke')
    expect(row('u-ops')?.querySelector('b')?.textContent).toBe('Your workspace (u-ops)')
  })

  it("names a platform admin's reach entry by its id, never its address", async () => {
    await drawn()
    await waitFor(() => expect(row('labs')).not.toBeNull())
    expect(row('labs')?.querySelector('b')?.textContent).toBe('labs')
  })

  it('names the workspace the same way in the default picker and the GitHub card', async () => {
    await drawn()
    await waitFor(() => expect(row('eng')?.querySelector('b')?.textContent).toBe('Engineering'))
    const options = [...document.querySelectorAll<HTMLOptionElement>('form[aria-label="Change your default workspace"] option')].map((o) => o.textContent)
    expect(options).toContain('Engineering')
    expect(options).not.toContain('eng@example.com')
    const github = document.querySelector<HTMLElement>('[data-profile-section="connections"] li[data-tenant="eng"]')
    expect(github?.querySelector('b')?.textContent).toBe('Engineering')
    expect(visible(document.body)).not.toContain('eng@example.com')
  })
})
