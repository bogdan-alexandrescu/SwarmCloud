// CONFIG › MCP SERVERS' ROWS (MCP-ROWS; part of #2157, BGD's pick of proposal
// E, docs/web-ui/mockups/config-e.html S5a desktop, S5d phone).
//
// WHAT EACH CASE HOLDS:
//   * a server the setup read lists as missing its credential (an `mcp_server`
//     item of `config_setup.secrets`, the lead's own source) says "needed, not
//     set" in its Credential cell, and only that server's row does;
//   * "Register an MCP server" is a button in the card's head, and the form
//     opens from it rather than standing open under the table;
//   * "Addresses to allowlist" is folded under the table, its count in the
//     fold's head;
//   * nothing says "Settings › MCP servers" any more: the link is Config › MCP
//     servers, at the config/ address SECTIONS spells.
//
// MUTATIONS, each of which turns a case red: drop the cell's note; mark every
// workspace_secret row; draw the form open; open the fold; restore the old
// link words or address.
//
// Fixtures only; no network. Nothing here is credential-shaped.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>

function server(name: string, mode: string): Json {
  return {
    name, url: `https://${name}.example.com/mcp`, description: '', enabled: true, revision: 1,
    auth: { mode, header: 'Authorization', scheme: 'Bearer' }, allowed_profiles: [], allowed_tools: [],
    consumer: `mcp:${name}`, updated_at: null, updated_by: null,
  }
}

// Documentation addresses (RFC 5737): never a real NAT.
const NAT = ['203.0.113.7', '198.51.100.24']

function listOf(canManage: boolean): Json {
  return {
    tenant_id: 'eng', can_manage: canManage, egress_addresses: NAT,
    servers: [server('kb', 'workspace_secret'), server('wiki', 'workspace_secret'), server('docs-search', 'dispatch')],
  }
}

const KB_MISSING = { kind: 'mcp_server', name: 'kb', needed_by: 'mcp:kb', where: 'config/mcp-servers' }

async function draw(opts: { canManage?: boolean; items?: Json[] } = {}) {
  serve((m, url) => (m === 'GET' && url.startsWith('/v1/mcp-servers') ? { status: 200, body: listOf(opts.canManage ?? true) } : null))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { McpServersScreen } = await import('../McpServers')
  const { SetupContext } = await import('../ConfigSetup')
  const items = opts.items ?? [KB_MISSING]
  const data = {
    tenant_id: 'eng', steps: [],
    config_setup: { secrets: { needed: items.length, done: items.length === 0, items, sources: { mcp_servers: { needed: items.length, reason: null } } } },
  }
  render(
    <SetupContext.Provider value={{ doc: { status: 'ok', data: data as never, fetchedAt: Date.now() }, personal: false }}>
      <McpServersScreen />
    </SetupContext.Provider>,
  )
  return screen.findByRole('table', { name: /registered mcp servers/i })
}

function credentialCell(table: HTMLElement, name: string): HTMLElement {
  const row = within(table).getAllByRole('row').find((r) => visible(r.querySelector('th[scope="row"] code')) === name)!
  return row.querySelector('td[data-label="Credential"]') as HTMLElement
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

describe('Config › MCP servers: its rows (S5a, S5d)', () => {
  it('says "needed, not set" on the Credential cell of a server whose credential is not set, and on no other row', async () => {
    const table = await draw()
    expect(visible(credentialCell(table, 'kb'))).toContain('needed, not set')
    // wiki reads a workspace secret too, but the setup read does not list it as missing.
    // MUTATION: mark every workspace_secret row -> wiki says it and this fails.
    expect(visible(credentialCell(table, 'wiki'))).not.toContain('needed, not set')
    expect(visible(credentialCell(table, 'docs-search'))).not.toContain('needed, not set')
    expect(credentialCell(table, 'kb').closest('tr')!.classList.contains('is-need')).toBe(true)
  })

  it('says it on no row when nothing is missing', async () => {
    const table = await draw({ items: [] })
    expect(visible(table)).not.toContain('needed, not set')
  })

  it('puts "Register an MCP server" in the card head, and the form opens from it', async () => {
    await draw()
    const card = screen.getByRole('heading', { name: 'Registered MCP servers' }).closest('.c-card') as HTMLElement
    const head = screen.getByRole('heading', { name: 'Registered MCP servers' }).parentElement as HTMLElement
    const register = within(head).getByRole('button', { name: 'Register an MCP server' })
    expect(card.contains(register)).toBe(true)
    // MUTATION: draw the form open under the table -> this fails.
    expect(screen.queryByRole('form', { name: /register an mcp server/i })).toBeNull()
    expect(register.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(register)
    expect(await screen.findByRole('form', { name: /register an mcp server/i })).toBeTruthy()
    expect(register.getAttribute('aria-expanded')).toBe('true')
  })

  it('offers no Register to a member who cannot manage the workspace', async () => {
    await draw({ canManage: false })
    expect(screen.queryByRole('button', { name: 'Register an MCP server' })).toBeNull()
  })

  it('folds "Addresses to allowlist" under the table, its count in the head', async () => {
    const table = await draw()
    const region = screen.getByRole('region', { name: /addresses to allowlist/i })
    // Under the table, not above it.
    expect(table.compareDocumentPosition(region) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    const fold = region.querySelector('details')
    expect(fold, 'the allowlist is not a fold').not.toBeNull()
    // MUTATION: open the fold -> this fails.
    expect(fold!.open).toBe(false)
    expect(visible(fold!.querySelector('summary'))).toContain('2 addresses')
    for (const ip of NAT) expect(visible(fold)).toContain(ip)
  })
})

describe('the link to the page', () => {
  it('reads Config › MCP servers and points at its config/ address', async () => {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { McpServersLink, MCP_SERVERS_ADDRESS } = await import('../McpServers')
    expect(MCP_SERVERS_ADDRESS).toBe('config/mcp-servers')
    render(<McpServersLink />)
    const link = screen.getByRole('link')
    expect(visible(link)).toBe('Config › MCP servers')
    expect(link.getAttribute('href')).toBe('/config/mcp-servers')
  })

  it('leaves no "Settings › MCP servers" in the page\'s source', async () => {
    const src = readFileSync(join(__dirname, '..', 'McpServers.tsx'), 'utf8')
    expect(src).not.toContain('Settings › MCP servers')
    expect(src).not.toMatch(/'settings\/mcp-servers'/)
  })
})
