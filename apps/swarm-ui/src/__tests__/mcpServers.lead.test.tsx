// CONFIG › MCP SERVERS' LEAD (CONFIG-S5-LEADS; part of #2157, BGD's pick of
// proposal E, docs/web-ui/mockups/config-e.html S5a, S5d).
//
// WHAT EACH CASE HOLDS:
//   * the lead names the MCP server whose credential is missing, read from
//     the setup read's `config_setup.secrets` items of kind `mcp_server`
//     (CONFIG-SETUP-READ), with ONE button, to Config › Secrets' Add a secret
//     form with that server's consumer picked (MCP-LEAD-FORM; never
//     `?action=add`, the one-time link's address);
//   * every credential present reads the green "nothing waiting" lead, with
//     no button;
//   * a source that could not be read is "—" and its reason, never green;
//   * the page's body is as built: the table is still drawn under the lead.
//
// MUTATIONS, each of which turns a case red: drop the lead; read green while
// an item is listed; read green when `sources.mcp_servers.needed` is null.
//
// Fixtures only; no network. Nothing here is credential-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>

function server(name: string, mode: string): Json {
  return {
    name, url: `https://${name}.example.com/mcp`, description: '', enabled: true, revision: 1,
    auth: { mode, header: 'Authorization', scheme: 'Bearer' }, allowed_profiles: [], allowed_tools: [],
    consumer: `mcp:${name}`, updated_at: null, updated_by: null,
  }
}

const LIST: Json = {
  tenant_id: 'eng', can_manage: true, egress_addresses: [],
  servers: [server('kb', 'workspace_secret'), server('docs-search', 'dispatch')],
}

function secrets(items: Json[], mcp: Json = { needed: items.length, reason: null }): Json {
  const needed = mcp.needed === null ? null : items.length + 2
  return {
    needed, done: needed === null ? null : needed === 0, items: [
      ...items,
      ...(needed === null || needed === 0 ? [] : [
        { kind: 'qa_target', name: 'Studio', needed_by: 'QA runs of Studio (production)', where: 'config/repositories?page=qa-targets' },
        { kind: 'provider_key', name: 'typesafe', needed_by: 'jev', where: 'settings/keys' },
      ]),
    ],
    sources: { mcp_servers: mcp, qa_targets: { needed: 1, reason: null }, provider_key: { needed: 1, reason: null } },
  }
}

const KB_MISSING = { kind: 'mcp_server', name: 'kb', needed_by: 'mcp:kb', where: 'config/mcp-servers' }

async function draw(configSetup: Json | undefined) {
  serve((m, url) => (m === 'GET' && url.startsWith('/v1/mcp-servers') ? { status: 200, body: LIST } : null))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { McpServersScreen } = await import('../McpServers')
  const { SetupContext } = await import('../ConfigSetup')
  const data = { tenant_id: 'eng', steps: [], ...(configSetup === undefined ? {} : { config_setup: configSetup }) }
  render(
    <SetupContext.Provider value={{ doc: { status: 'ok', data: data as never, fetchedAt: Date.now() }, personal: false }}>
      <McpServersScreen />
    </SetupContext.Provider>,
  )
  await screen.findByRole('table', { name: /registered mcp servers/i })
  const lead = document.querySelector('.ws-lead')
  expect(lead, 'the page has no lead').not.toBeNull()
  return lead as HTMLElement
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

describe('Config › MCP servers: its lead (S5a)', () => {
  it('names the one server whose credential is missing, with one button that opens setting it', async () => {
    const lead = await draw({ secrets: secrets([KB_MISSING]) })
    expect(visible(lead)).toContain('MCP server kb has no credential stored')
    expect(visible(lead)).not.toContain('docs-search')
    const buttons = within(lead).getAllByRole('link')
    expect(buttons).toHaveLength(1)
    // Add a secret on Config › Secrets, mcp:kb picked (MCP-LEAD-FORM, BGD
    // 2026-10-25). Never `?action=add`: that is the /sc one-time link's
    // address, and the page then says it was opened from one.
    // MUTATION: point the button at Config › Secrets alone -> no form opens and this fails.
    const href = buttons[0]!.getAttribute('href')!
    expect(href).not.toMatch(/action=/)
    expect(href.split('?')[0]).toBe('/config/secrets')
    expect(Object.fromEntries(new URLSearchParams(href.split('?')[1] ?? ''))).toEqual({ add: 'secret', consumer: 'mcp:kb' })
    expect(lead.classList.contains('is-ok')).toBe(false)
  })

  it('counts the missing against the whole step only when some are elsewhere', async () => {
    const elsewhere = await draw({ secrets: secrets([KB_MISSING]) })
    expect(visible(elsewhere)).toContain('Secrets set · 1 of the 3 missing is here')
    cleanup()
    const only = await draw({ secrets: { needed: 1, done: false, items: [KB_MISSING], sources: { mcp_servers: { needed: 1, reason: null } } } })
    expect(visible(only)).toContain('Secrets set · 1 missing')
    expect(visible(only)).not.toContain('of the 1')
  })

  it('opens the form with nothing picked when several servers are missing theirs: one secret is one server\'s credential', async () => {
    const lead = await draw({ secrets: secrets([KB_MISSING, { ...KB_MISSING, name: 'wiki', needed_by: 'mcp:wiki' }]) })
    expect(visible(lead)).toContain('MCP servers kb and wiki have no credential stored')
    const href = within(lead).getByRole('link').getAttribute('href')!
    // MUTATION: pick the first server for all of them -> fails.
    expect(href).toBe('/config/secrets?add=secret')
  })

  it('reads the green "nothing waiting" lead, with no button, when every credential is present', async () => {
    const lead = await draw({ secrets: secrets([]) })
    expect(lead.classList.contains('is-ok')).toBe(true)
    expect(visible(lead)).toMatch(/nothing waiting/i)
    expect(within(lead).queryAllByRole('link')).toHaveLength(0)
    expect(within(lead).queryAllByRole('button')).toHaveLength(0)
  })

  it('is "—" with its reason when the MCP servers source could not be read', async () => {
    const reason = 'the MCP servers and the secrets granted to them could not be read'
    const lead = await draw({ secrets: secrets([], { needed: null, reason }) })
    expect(lead.classList.contains('is-ok')).toBe(false)
    expect(visible(lead)).toContain('—')
    expect(visible(lead)).toContain(reason)
  })

  it('is "—" with its reason when the setup read carries no config_setup', async () => {
    const lead = await draw(undefined)
    expect(lead.classList.contains('is-ok')).toBe(false)
    expect(visible(lead)).toContain('—')
    expect(visible(lead)).toMatch(/does not serve/i)
  })
})
