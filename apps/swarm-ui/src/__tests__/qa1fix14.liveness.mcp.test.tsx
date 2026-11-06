// QA1-FIX-14 (part of #2596, the console visual QA pass 1 epic, and #2391):
// the findings whose main file is Liveness.tsx or McpServers.tsx.
//
//   overview-agents-22  a worker silent for days said `No event for 12809m`
//                       and `silent 12809m ago`: an age past the hour is the
//                       shared formatter's words, `8d 21h`.
//   config-25           Config › MCP servers' lead opened on a dangling dash
//                       ("— whether an MCP server is missing ...") and said
//                       "not known" while Config › Secrets said kb's
//                       credential was not set: it now reads that overview
//                       before falling back, and the sentence starts with a
//                       word, the dash moved to the kicker's figure.
//   config-26           the State column broke 'enable / d', and Edit stacked
//                       over Disable: the State cell and the action row keep
//                       their line at 1440.
//   config-27           Register an MCP server opened its form below the fold
//                       and left focus on the button: it scrolls into view and
//                       Name takes focus.
//
// Fixtures only; no network. Nothing here is credential-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { serve, visible } from './repofixture'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { nowrap } from './tablefit'
import { livenessOf } from '../Liveness'
import type { Task } from '../types'

type Json = Record<string, unknown>

const WIDE: CascadeEnv = { width: 1440 }
const MIN = 60_000

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

describe('overview-agents-22: the liveness age is a duration a person reads', () => {
  const NOW = Date.UTC(2026, 9, 28, 9, 0, 0)
  const task = { state: 'RUNNING' } as unknown as Task
  const at = (minsAgo: number) => [{ at: new Date(NOW - minsAgo * MIN).toISOString() }] as never

  it('says 8d 21h, never 12809m, for a worker silent for days', () => {
    const l = livenessOf(task, at(12809), NOW)
    expect(l.kind).toBe('silent')
    // MUTATION: put back `${mins}m` in Liveness.tsx -> `12809m ago`: red.
    expect(l.copy).toBe('8d 21h ago')
    expect(l.say).toMatch(/^No event for 8d 21h\. /)
    expect(`${l.copy} ${l.say}`).not.toMatch(/\d{3,}m/)
  })

  it('says hours and minutes past the hour, and whole minutes below it', () => {
    // MUTATION: format every age with spanText -> `20m 0s ago`: red.
    expect(livenessOf(task, at(20), NOW).copy).toBe('20m ago')
    expect(livenessOf(task, at(5), NOW).say).toMatch(/^No event for 5m\. Heartbeat/)
    // MUTATION: keep minutes up to a day -> `150m ago`: red.
    expect(livenessOf(task, at(150), NOW).copy).toBe('2h 30m ago')
  })
})

function server(name: string, mode: string, enabled = true): Json {
  return {
    name, url: `https://${name}.example.com/a/rather/long/path/to/the/mcp/endpoint`, description: '', enabled, revision: 1,
    auth: { mode, header: 'Authorization', scheme: 'Bearer' }, allowed_profiles: [], allowed_tools: [],
    consumer: `mcp:${name}`, updated_at: null, updated_by: null, last_attached_at: null,
  }
}

const LIST: Json = {
  tenant_id: 'eng', can_manage: true, egress_addresses: [], last_attached_complete: true,
  servers: [server('kb', 'workspace_secret'), server('status-board', 'none', false)],
}

function overview(kbStatus: 'set' | 'unset'): Json {
  return {
    tenant_id: 'eng', scope: 'workspace', repo_id: null, repository: null, personal_tenant_id: null, can_manage: true,
    rows: [
      { section: 'keys', store: 'mcp_servers', name: 'kb', consumer: 'mcp:kb', secrets: [], status: kbStatus, set_at: null, set_by: null, consumers: ['mcp:kb'], can_manage: true },
      { section: 'keys', store: 'provider_keys', name: 'typesafe', status: 'unset', set_at: null, set_by: null, consumers: [], can_manage: true },
    ],
  }
}

/** The page with a setup read that carries no `config_setup` (its lead is unknown); `secrets` null: the overview fails. */
async function draw(secrets: Json | null) {
  const calls = serve((m, url) => {
    if (m === 'GET' && url.startsWith('/v1/mcp-servers')) return { status: 200, body: LIST }
    if (m === 'GET' && url.startsWith('/v1/secrets/overview')) return secrets === null ? { status: 500, body: { detail: 'down' } } : { status: 200, body: secrets }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { McpServersScreen } = await import('../McpServers')
  const { SetupContext } = await import('../ConfigSetup')
  render(
    <SetupContext.Provider value={{ doc: { status: 'ok', data: { tenant_id: 'eng', steps: [] } as never, fetchedAt: Date.now() }, personal: false }}>
      <McpServersScreen />
    </SetupContext.Provider>,
  )
  const table = (await screen.findByRole('table', { name: 'Registered MCP servers' })) as HTMLTableElement
  return { table, calls }
}

const lead = () => document.querySelector<HTMLElement>('.ws-lead')

describe('config-25: the MCP servers lead says what the console knows, in a sentence', () => {
  it('names kb, as Config › Secrets does, when the setup read cannot say', async () => {
    const { table, calls } = await draw(overview('unset'))
    // MUTATION: drop the overview fallback (`mcpLeadWith` returns `lead`) -> "not known": red.
    await waitFor(() => expect(visible(lead())).toContain('MCP server kb has no credential stored'))
    expect(calls.some((c) => c.url.startsWith('/v1/secrets/overview?scope=workspace'))).toBe(true)
    // The row the lead names says so, as it does when the setup read names it.
    const kb = [...table.querySelectorAll('tbody tr')].find((tr) => tr.querySelector('th')?.textContent?.startsWith('kb'))!
    expect(visible(kb.querySelector('td[data-label="Credential"]'))).toContain('needed, not set')
  })

  it('stays unknown, never green, when the overview lists no MCP server unset', async () => {
    await draw(overview('set'))
    await waitFor(() => expect(visible(lead())).toMatch(/not known/))
    expect(lead()!.classList.contains('is-ok')).toBe(false)
  })

  it('starts the unknown sentence with a word, the dash being the kicker\'s figure', async () => {
    await draw(null)
    await waitFor(() => expect(visible(lead())).toMatch(/not known/))
    const sentence = visible(lead()!.querySelector('b'))
    // MUTATION: put back `<b>— whether ...` -> the sentence opens on a dash: red.
    expect(sentence).toMatch(/^Whether an MCP server is missing its credential is not known: /)
    expect(visible(lead()!.querySelector('.ws-lead-k'))).toBe('Secrets set · —')
  })

  it('reads the overview only when the setup read cannot say', async () => {
    const calls = serve((m, url) => (m === 'GET' && url.startsWith('/v1/mcp-servers') ? { status: 200, body: LIST } : null))
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { McpServersScreen } = await import('../McpServers')
    const { SetupContext } = await import('../ConfigSetup')
    const setup = { needed: 0, done: true, items: [], sources: { mcp_servers: { needed: 0, reason: null } } }
    render(
      <SetupContext.Provider value={{ doc: { status: 'ok', data: { tenant_id: 'eng', steps: [], config_setup: { secrets: setup } } as never, fetchedAt: Date.now() }, personal: false }}>
        <McpServersScreen />
      </SetupContext.Provider>,
    )
    await screen.findByRole('table', { name: 'Registered MCP servers' })
    expect(visible(lead())).toMatch(/nothing waiting/i)
    // MUTATION: read the overview unconditionally -> a second read on every visit: red.
    expect(calls.some((c) => c.url.startsWith('/v1/secrets/overview'))).toBe(false)
  })
})

describe('config-26: State never breaks mid-word, and the row actions keep one line', () => {
  it('holds the State cell and the action row on their line at 1440', async () => {
    const { table } = await draw(null)
    const state = table.querySelector('tbody td[data-label="State"]')!
    expect(state.textContent).toBe('enabled')
    // MUTATION: drop keys.css' `td[data-label='State'] { white-space: nowrap }` -> 'enable / d': red.
    expect(nowrap(state, WIDE)).toBe(true)
    expect(painted(state, 'overflow-wrap', WIDE)).toBe('normal')
    const actions = table.querySelector('tbody td[data-label="Actions"] > .pk-form-row')!
    // MUTATION: drop keys.css' `.pk-form-row { flex-wrap: nowrap }` for the Actions cell -> Edit over Disable: red.
    expect(painted(actions, 'flex-wrap', WIDE)).toBe('nowrap')
    // The URL still gives up the width, at its '/' and '.' breaks (QA2 config2-19), never mid-word.
    expect(painted(table.querySelector('tbody td[data-label="URL"]')!, 'overflow-wrap', WIDE)).toBe('break-word')
  })
})

describe('config-27: Register an MCP server brings its form to the reader', () => {
  it('scrolls the form into view and puts focus in Name', async () => {
    const scrolled: Element[] = []
    Element.prototype.scrollIntoView = function scrollIntoView(this: Element) {
      scrolled.push(this)
    }
    try {
      await draw(null)
      fireEvent.click(screen.getByRole('button', { name: 'Register an MCP server' }))
      const form = await screen.findByRole('form', { name: 'Register an MCP server' })
      // MUTATION: drop ServerForm's mount effect -> focus stays on the button, nothing scrolls: red.
      expect(document.activeElement).toBe(screen.getByLabelText('Name'))
      expect(scrolled.some((el) => el.contains(form))).toBe(true)
    } finally {
      delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView
    }
  })
})
