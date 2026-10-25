// CONFIG › MCP SERVERS (lane MCP-UI; docs/design/registered-mcp-servers.md
// §4.2, §4.9, §4.11; part of #1433). The workspace's registered MCP servers:
// the servers a task's agent may call, by name, through the worker's relay.
//
//   * every member reads the list; only a manager (`can_manage`, owner
//     decision Q1: a platform admin for a group workspace, the person for
//     their own) is offered the register form and the enable/disable control;
//   * the NAT addresses a server's owner allowlists are shown (§4.9), read
//     from the list's `egress_addresses`, and their absence is said in words
//     rather than drawn as an empty list;
//   * a registration has no credential, so neither does the form: no field
//     on it is a password, and a register POST carries none.
//
// MUTATIONS, each of which turns a case red: draw the form or the toggle for
// `can_manage: false`; drop the addresses; send a `credential` on register.
//
// Fixtures only; no network. Nothing here is credential-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>

// Documentation addresses (RFC 5737): never a real NAT.
const NAT = ['203.0.113.7', '198.51.100.24']

function server(name: string, over: Json = {}): Json {
  return {
    server_id: `mcp_${name}`,
    tenant_id: 'eng',
    name,
    url: `https://${name}.example.com/mcp`,
    transport: 'streamable-http',
    description: `the ${name} knowledge base`,
    auth: { mode: 'dispatch', header: 'Authorization', scheme: 'Bearer' },
    allowed_profiles: [],
    allowed_tools: [],
    limits: {},
    enabled: true,
    revision: 1,
    consumer: `mcp:${name}`,
    created_at: '2026-10-17T10:00:00Z',
    created_by: 'admin@example.com',
    updated_at: '2026-10-17T10:00:00Z',
    updated_by: 'admin@example.com',
    ...over,
  }
}

function list(opts: { canManage: boolean; servers?: Json[]; nat?: string[] | null }): Json {
  return {
    tenant_id: 'eng',
    can_manage: opts.canManage,
    servers: opts.servers ?? [server('kb'), server('docs', { enabled: false, auth: { mode: 'none', header: 'Authorization', scheme: 'Bearer' } })],
    note: 'swarm-api never calls a registered URL',
    ...(opts.nat === null ? {} : { egress_addresses: opts.nat ?? NAT }),
  }
}

function api(body: Json, write?: (m: string, url: string, body: unknown) => { status: number; body: unknown } | null) {
  return serve((m, url, b) => {
    if (m === 'GET' && url.startsWith('/v1/mcp-servers')) return { status: 200, body }
    if (m !== 'GET' && write) return write(m, url, b)
    return null
  })
}

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../McpServers')
}

afterEach(() => {
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

describe('Config › MCP servers', () => {
  it('lists every registration by name and URL, enabled or not', async () => {
    api(list({ canManage: true }))
    const { McpServersScreen } = await load()
    render(<McpServersScreen />)
    const table = await screen.findByRole('table', { name: /registered mcp servers/i })
    const text = visible(table)
    expect(text).toContain('kb')
    expect(text).toContain('https://kb.example.com/mcp')
    expect(text).toContain('docs')
    expect(text).toMatch(/disabled/i)
  })

  it('shows the NAT addresses a server owner allowlists', async () => {
    api(list({ canManage: false }))
    const { McpServersScreen } = await load()
    render(<McpServersScreen />)
    const region = await screen.findByRole('region', { name: /addresses to allowlist/i })
    for (const ip of NAT) expect(visible(region)).toContain(ip)
  })

  it('says the addresses are not served rather than drawing an empty list', async () => {
    api(list({ canManage: true, nat: null }))
    const { McpServersScreen } = await load()
    render(<McpServersScreen />)
    const region = await screen.findByRole('region', { name: /addresses to allowlist/i })
    expect(visible(region)).toMatch(/not served/i)
    expect(within(region).queryAllByRole('listitem')).toHaveLength(0)
  })

  it('is read-only for a member who cannot manage the workspace', async () => {
    api(list({ canManage: false }))
    const { McpServersScreen } = await load()
    render(<McpServersScreen />)
    await screen.findByRole('table', { name: /registered mcp servers/i })
    expect(screen.queryByRole('form', { name: /register an mcp server/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^(enable|disable|edit)\b/i })).toBeNull()
    expect(visible(document.body)).toMatch(/only a platform admin/i)
  })

  it('offers the register form and the enable/disable control to a manager', async () => {
    api(list({ canManage: true }))
    const { McpServersScreen } = await load()
    render(<McpServersScreen />)
    // The form opens from the card head's Register (MCP-ROWS, S5a).
    fireEvent.click(await screen.findByRole('button', { name: 'Register an MCP server' }))
    await screen.findByRole('form', { name: /register an mcp server/i })
    expect(screen.getByRole('button', { name: 'Disable kb' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Enable docs' })).toBeTruthy()
  })

  it('disables a server with one PATCH of `enabled` alone', async () => {
    const seen: Array<{ url: string; body: unknown }> = []
    api(list({ canManage: true }), (m, url, body) => {
      if (m === 'PATCH') {
        seen.push({ url, body })
        return { status: 200, body: { server: server('kb', { enabled: false }) } }
      }
      return null
    })
    const { McpServersScreen } = await load()
    render(<McpServersScreen />)
    fireEvent.click(await screen.findByRole('button', { name: 'Disable kb' }))
    await waitFor(() => expect(seen).toHaveLength(1))
    expect(seen[0]).toEqual({ url: '/v1/mcp-servers/kb', body: { enabled: false } })
  })

  it('registers a server with no credential field and no credential in the body', async () => {
    const posts: Json[] = []
    api(list({ canManage: true, servers: [] }), (m, url, body) => {
      if (m === 'POST' && url === '/v1/mcp-servers') {
        posts.push(body as Json)
        return { status: 201, body: { server: server('kb') } }
      }
      return null
    })
    const { McpServersScreen } = await load()
    const r = render(<McpServersScreen />)
    fireEvent.click(await screen.findByRole('button', { name: 'Register an MCP server' }))
    const form = await screen.findByRole('form', { name: /register an mcp server/i })
    expect(form.querySelector('input[type="password"]')).toBeNull()
    fireEvent.change(within(form).getByLabelText(/^name$/i), { target: { value: 'kb' } })
    fireEvent.change(within(form).getByLabelText(/^url$/i), { target: { value: 'https://kb.example.com/mcp' } })
    fireEvent.click(within(form).getByRole('radio', { name: /per-run credential/i }))
    fireEvent.click(within(form).getByRole('button', { name: /^register$/i }))
    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0]).toMatchObject({ name: 'kb', url: 'https://kb.example.com/mcp', auth: { mode: 'dispatch' } })
    expect(Object.keys(posts[0]!).sort()).toEqual(['allowed_profiles', 'allowed_tools', 'auth', 'description', 'name', 'url'])
    expect(r.container.textContent).not.toMatch(/credential value/i)
  })

  // MCP-UI-WF (§4.11 "last used"): MCP-API-B serves `last_attached_at` on
  // every server, the newest attach of THIS workspace's tasks, or null when
  // there is none. Null is a fact -- no task has used it -- so it reads
  // "never", not a dash that reads as "unknown".
  it('draws a "Last used" column: when a server was last attached, and "never" for null', async () => {
    const at = new Date(Date.now() - 3 * 3600_000).toISOString()
    api(list({ canManage: false, servers: [server('kb', { last_attached_at: at }), server('docs', { last_attached_at: null })] }))
    const { McpServersScreen } = await load()
    const { timeAgo } = await import('../types')
    render(<McpServersScreen />)
    const table = await screen.findByRole('table', { name: /registered mcp servers/i })
    const heads = within(table).getAllByRole('columnheader').map((h) => visible(h))
    expect(heads).toContain('Last used')
    const col = heads.indexOf('Last used')
    const cell = (name: string) => {
      const row = within(table).getAllByRole('row').find((r) => visible(r.querySelector('th[scope="row"] code')) === name)!
      return row.children[col] as HTMLElement
    }
    expect(visible(cell('kb'))).toBe(timeAgo(at))
    expect(visible(cell('docs'))).toBe('never')
  })

  it('does not say "never" when the API says its read of recent attaches was cut off', async () => {
    api({ ...list({ canManage: false, servers: [server('docs', { last_attached_at: null })] }), last_attached_complete: false })
    const { McpServersScreen } = await load()
    render(<McpServersScreen />)
    const table = await screen.findByRole('table', { name: /registered mcp servers/i })
    const col = within(table).getAllByRole('columnheader').map((h) => visible(h)).indexOf('Last used')
    const row = within(table).getAllByRole('row')[1]!
    expect(visible(row.children[col] as HTMLElement)).toBe('not recently')
  })

  it('says the API does not serve it rather than "never" when `last_attached_at` is absent', async () => {
    api(list({ canManage: false, servers: [server('docs')] }))
    const { McpServersScreen } = await load()
    render(<McpServersScreen />)
    const table = await screen.findByRole('table', { name: /registered mcp servers/i })
    const col = within(table).getAllByRole('columnheader').map((h) => visible(h)).indexOf('Last used')
    expect(col).toBeGreaterThan(-1)
    const row = within(table).getAllByRole('row')[1]!
    expect(visible(row.children[col] as HTMLElement)).toBe('not served')
  })

  it('says the feature is off when the API answers a write with mcp_unavailable', async () => {
    api(list({ canManage: true }), (m) =>
      m === 'PATCH' ? { status: 503, body: { code: 'mcp_unavailable', message: 'Registered MCP servers are not enabled on this platform.' } } : null,
    )
    const { McpServersScreen } = await load()
    render(<McpServersScreen />)
    fireEvent.click(await screen.findByRole('button', { name: 'Disable kb' }))
    const alert = await screen.findByRole('alert')
    expect(visible(alert)).toMatch(/not enabled on this platform/i)
  })
})
