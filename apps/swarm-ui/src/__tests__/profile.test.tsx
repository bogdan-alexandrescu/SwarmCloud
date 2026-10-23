// SETTINGS › PROFILE, THE SCREEN (lane PROFILE-PAGE, part of #1505;
// docs/design/user-profile.md §4.1, §4.2). Four sections from one read of
// GET /v1/profile: you and your workspaces, your connections (exactly GitHub
// and TypeSafe, Q6), your bell, your Claude accounts. Each section the API
// could not read says so; a save sends the whole set at the revision read.
//
// Fixtures only; no network. The planted secret-shaped fields hold a value
// built at runtime, so nothing here looks like a credential.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>

/** A value no screen may draw, built at runtime. */
const SENTINEL = 'tok-' + Array.from({ length: 16 }, (_, i) => ((i * 7 + 3) % 16).toString(16)).join('') + Date.now().toString(16)

function fixture(over: Json = {}): Json {
  return {
    identity: {
      email: 'ada@example.com',
      display_name: null,
      display_name_suggested: 'Ada Lovelace',
      domain: 'example.com',
      platform_role: null,
      is_admin: false,
      is_admin_unresolved: false,
      // Planted: never drawn.
      token: SENTINEL,
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
        is_default: false,
      },
      {
        tenant_id: 'team-x',
        display_name: 'team-x@example.com',
        kind: 'group',
        role: { name: 'maintainer', source: 'assigned', via: 'team-x@example.com' },
        state: null,
        selectable: true,
        is_acting: false,
        is_default: true,
        secret_name: SENTINEL,
      },
    ],
    default_workspace: { tenant_id: 'team-x', source: 'profile', ignored: null },
    connections: {
      github: [
        {
          tenant_id: 'u-ada',
          state: 'active',
          forge_login: 'ada',
          method: 'app_user',
          access_expires_at: new Date(Date.now() + 3 * 3600_000).toISOString(),
          refreshed_at: new Date(Date.now() - 600_000).toISOString(),
          failure: null,
          secret_name: SENTINEL,
          token_id: SENTINEL,
        },
        { tenant_id: 'team-x', state: 'not_connected' },
      ],
      typesafe: {
        personal: { set: true, set_at: new Date(Date.now() - 86_400_000).toISOString(), set_by: 'ada@example.com', version: 3, secret_id: SENTINEL },
        acting_workspace: { tenant_id: 'u-ada', set: false, secret_id: SENTINEL },
        uses: 'user',
      },
    },
    notifications: {
      preferences: { muted: ['finished'], reminder: 'every_4h' },
      topics: ['approval', 'waiting_step', 'question', 'failed', 'finished', 'account', 'admin'],
    },
    claude_accounts: {
      personal_workspace: 'u-ada',
      accounts: [
        {
          account_id: 'u-ada:main',
          label: 'main',
          relation: 'own',
          state: 'AVAILABLE',
          windows: {
            five_hour: { utilization: 0.42, resets_at: new Date(Date.now() + 3600_000).toISOString() },
            seven_day: { utilization: 0.18, resets_at: new Date(Date.now() + 4 * 86_400_000).toISOString() },
          },
          lent_to_count: 1,
          stale: false,
          refresh_token: SENTINEL,
        },
      ],
      unreadable_document_count: 0,
    },
    revision: 4,
    unavailable: [],
    value: SENTINEL,
    ...over,
  }
}

function saved(body: Json, revision: number): Json {
  return {
    identity: { display_name: body.display_name ?? null, display_name_suggested: null },
    default_workspace: { tenant_id: body.default_workspace ?? 'u-ada', source: body.default_workspace ? 'profile' : 'personal', ignored: null },
    notifications: { preferences: body.notifications, topics: ['approval', 'waiting_step', 'question', 'failed', 'finished', 'account', 'admin'] },
    revision,
  }
}

function api(start: Json, write?: (body: Json) => { status: number; body: unknown }) {
  let current = start
  const calls = serve((m, url, body) => {
    if (m === 'GET' && url === '/v1/profile') return { status: 200, body: current }
    if (m === 'PUT' && url === '/v1/profile/preferences' && write) return write(body as Json)
    return null
  })
  return {
    calls,
    set: (j: Json) => {
      current = j
    },
  }
}

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../Profile')
}

async function drawn(start: Json = fixture(), write?: (body: Json) => { status: number; body: unknown }) {
  const server = api(start, write)
  const { ProfileScreen } = await load()
  render(<ProfileScreen />)
  await screen.findByText('ada@example.com')
  return server
}

const region = (name: string) => screen.getByRole('region', { name })
const headings = (el: HTMLElement) => within(el).queryAllByRole('heading', { level: 2 }).map((h) => visible(h))

afterEach(() => {
  vi.unstubAllEnvs()
  window.localStorage.clear()
  window.sessionStorage.clear()
  window.history.replaceState(null, '', '/')
})

describe('Settings › Profile', () => {
  it('draws its four sections from one read', async () => {
    const { calls } = await drawn()
    expect(document.querySelectorAll('[data-profile-section]').length).toBe(4)
    expect(headings(region('You and your workspaces'))).toEqual(['You'])
    expect(headings(region('Your connections'))).toEqual(['GitHub', 'TypeSafe key'])
    expect(headings(region('Notifications'))).toEqual(['Notifications'])
    expect(headings(region('Claude accounts'))).toEqual(['Claude accounts'])
    expect(visible(region('Claude accounts'))).toMatch(/main · yours · AVAILABLE · 5\u2011hour 42%/)
    expect(calls.filter((c) => c.url === '/v1/profile')).toHaveLength(1)
  })

  it('has exactly a GitHub card and a TypeSafe card for connections, and no SwarmCloud API keys (Q6)', async () => {
    await drawn()
    const connections = region('Your connections')
    expect(connections.querySelectorAll('section.c-card')).toHaveLength(2)
    expect(visible(document.body)).not.toMatch(/swarmcloud api key/i)
    // The one link to Settings › API keys is the TypeSafe card's own: it is that key's page.
    const keyLinks = Array.from(document.querySelectorAll('a[href^="/settings/keys"]'))
    expect(keyLinks).toHaveLength(1)
    expect(keyLinks[0]?.closest('section')?.querySelector('h2')?.textContent).toBe('TypeSafe key')
  })

  it('has one GitHub row per workspace (Q10), and never reads an unconnected one as an error', async () => {
    await drawn()
    const rows = within(region('Your connections')).getByRole('list', { name: /github/i }).querySelectorAll('li')
    expect(Array.from(rows).map((r) => r.getAttribute('data-tenant'))).toEqual(['u-ada', 'team-x'])
    expect(visible(rows[0] ?? null)).toMatch(/Connected · @ada · app_user/)
    expect(visible(rows[1] ?? null)).toMatch(/Not connected/)
  })

  it('draws a role it has never heard of verbatim, with its via (Q7)', async () => {
    await drawn()
    const team = within(region('You and your workspaces')).getByRole('list', { name: 'Your workspaces' }).querySelector('[data-tenant="team-x"]')
    expect(visible(team)).toMatch(/maintainer via team-x@example\.com/)
    const own = within(region('You and your workspaces')).getByRole('list', { name: 'Your workspaces' }).querySelector('[data-tenant="u-ada"]')
    expect(visible(own)).toMatch(/owner/)
    expect(visible(own)).not.toMatch(/via/)
  })

  it('offers the suggested name first, and shows a display_name_taken refusal in the API’s words', async () => {
    const message = 'That name is already used on SwarmCloud; choose another.'
    await drawn(fixture(), () => ({ status: 422, body: { code: 'display_name_taken', message } }))
    const field = screen.getByLabelText('Display name') as HTMLInputElement
    expect(field.value).toBe('Ada Lovelace')
    fireEvent.click(screen.getByRole('button', { name: 'Save name' }))
    expect(visible(await screen.findByRole('alert'))).toContain(message)
  })

  it('draws no field the API was not meant to send', async () => {
    await drawn()
    expect(document.body.innerHTML).not.toContain(SENTINEL)
  })

  it('links each summary to the page that owns it', async () => {
    await drawn()
    const link = (name: string) => within(region(name)).getAllByRole('link').map((a) => a.getAttribute('href'))
    expect(link('You and your workspaces')).toContain('/setup')
    expect(link('Your connections')).toEqual(expect.arrayContaining(['/access', '/settings/keys']))
    expect(link('Claude accounts')).toContain('/capacity/accounts')
  })

  it('switches workspace before opening Access for a workspace that is not the acting one', async () => {
    await drawn()
    const row = within(region('Your connections')).getByRole('list', { name: /github/i }).querySelector('[data-tenant="team-x"]')!
    const open = within(row as HTMLElement).getByRole('link')
    expect(visible(open)).toMatch(/in team-x@example\.com/)
    // App's router takes the click (App.tsx, the document click handler); jsdom cannot navigate.
    const route = (e: Event) => e.preventDefault()
    document.addEventListener('click', route)
    fireEvent.click(open)
    document.removeEventListener('click', route)
    expect(window.localStorage.getItem('swarm.tenant') ?? window.sessionStorage.getItem('swarm.tenant')).toBe('team-x')
  })

  it('saves the whole set with the revision it read', async () => {
    const seen: Json[] = []
    await drawn(fixture(), (body) => {
      seen.push(body)
      return { status: 200, body: saved(body, 5) }
    })
    fireEvent.click(screen.getByRole('checkbox', { name: /failed runs/i }))
    fireEvent.click(screen.getByRole('button', { name: 'Save notifications' }))
    await waitFor(() => expect(seen).toHaveLength(1))
    expect(seen[0]).toEqual({
      display_name: null,
      default_workspace: 'team-x',
      notifications: { muted: ['failed', 'finished'], reminder: 'every_4h' },
      revision: 4,
    })
    await waitFor(() => expect(visible(document.body)).toMatch(/Saved\. It applies to new sessions within a minute/))
    // The next save carries the revision the first one produced.
    fireEvent.change(screen.getByLabelText('Display name'), { target: { value: 'Ada King' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save name' }))
    await waitFor(() => expect(seen).toHaveLength(2))
    expect(seen[1]).toMatchObject({ display_name: 'Ada King', revision: 5 })
  })

  it('reads again when another tab saved first (409)', async () => {
    const server = await drawn(fixture(), () => ({ status: 409, body: { code: 'preferences_changed', message: 'changed' } }))
    server.set(fixture({ revision: 9, notifications: { preferences: { muted: [], reminder: 'daily' }, topics: ['approval'] } }))
    fireEvent.click(screen.getByRole('checkbox', { name: /finished runs/i }))
    fireEvent.click(screen.getByRole('button', { name: 'Save notifications' }))
    await waitFor(() => expect(server.calls.filter((c) => c.method === 'GET' && c.url === '/v1/profile')).toHaveLength(2))
    expect(visible(document.body)).toMatch(/changed in another tab/i)
    await waitFor(() => expect((screen.getByLabelText(/remind me again/i) as HTMLSelectElement).value).toBe('daily'))
  })

  it('says a section could not be read rather than drawing it empty', async () => {
    const { connections, claude_accounts: _accounts, ...rest } = fixture() as Json & { connections: Json }
    void _accounts
    await drawn({ ...rest, connections: { typesafe: connections.typesafe }, unavailable: ['github', 'claude_accounts'] })
    expect(visible(region('Your connections'))).toMatch(/GitHub connections could not be read/)
    expect(visible(region('Your connections'))).not.toMatch(/not connected/i)
    expect(visible(region('Claude accounts'))).toMatch(/could not be read/)
    expect((screen.getByLabelText('Display name') as HTMLInputElement).disabled).toBe(false)
  })

  it('disables every editor when the preferences could not be read', async () => {
    const { notifications: _n, ...rest } = fixture()
    void _n
    const { calls } = await drawn({ ...rest, revision: null, unavailable: ['preferences'] })
    expect(visible(document.body)).toMatch(/your preferences could not be read; retry shortly/i)
    expect((screen.getByLabelText('Display name') as HTMLInputElement).disabled).toBe(true)
    expect((screen.getByLabelText('Default workspace') as HTMLInputElement).disabled).toBe(true)
    expect((screen.getByLabelText(/remind me again/i) as HTMLInputElement).disabled).toBe(true)
    for (const box of screen.getAllByRole('checkbox')) expect((box as HTMLInputElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Save name' }))
    expect(calls.filter((c) => c.method !== 'GET')).toEqual([])
  })
})
