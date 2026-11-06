// CONFIG › SECRETS, VISUAL QA PASS 2 (QA2-FIX-22; part of #2694). The findings
// this lane fixes in Secrets.tsx, each held to what the reader sees:
//
//   * config2-05: the TypeSafe key row says it is not the TypeSafe secret For
//     agents lists, so two set dates no longer read as one key set twice;
//   * config2-14: the keys table's hint names the buttons it draws, with no
//     "Was Settings › API keys" history;
//   * config2-15: storing an MCP server's credential has one label, "Set kb’s
//     credential", in the lead and in the keys table, as MCP servers says;
//   * config2-16: a shadow note whose winner is the signed-in person says
//     "your own wins on your agents", not their address in the third person.
//
// Fixtures only; no network.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>

const NOW = Date.parse('2026-10-29T10:00:00Z')
const daysAgo = (d: number) => new Date(NOW - d * 86_400_000).toISOString()

const SHADOWED = {
  section: 'agents', store: 'workspace_secrets', name: 'typesafe-key', scope: 'workspace', kind: 'api_key', status: 'set', set_at: daysAgo(2), set_by: 'ana@example.com',
  consumers: [], can_manage: true, shadows: [],
  shadowed_by: [{ scope: 'personal', where: 'agents of ana@example.com', person: 'ana@example.com', slot: 'typesafe' }],
}
const TYPESAFE_KEY = { section: 'keys', store: 'provider_keys', name: 'typesafe', status: 'set', set_at: daysAgo(5), set_by: 'ana@example.com', consumers: [], can_manage: true }
const RUNNER_KEY = { section: 'keys', store: 'runner_provider_keys', name: 'anthropic', status: 'set', set_at: null, set_by: null, consumers: ['profile:claude-code'], can_manage: true }
const KB = { section: 'keys', store: 'mcp_servers', name: 'kb', consumer: 'mcp:kb', secrets: [], status: 'unset', set_at: null, set_by: null, consumers: ['mcp:kb'], can_manage: true }
const SECRET = { name: 'typesafe-key', provider: 'typesafe', kind: 'api_key', current_version: '1', created_at: daysAgo(2), created_by: 'ana@example.com', rotated_at: null, rotated_by: null, grants: [] }

function api(rows: Json[], me: string | null = null) {
  return serve((m, url) => {
    const u = decodeURIComponent(url)
    if (m === 'GET' && u.startsWith('/v1/tenants/me') && me !== null) {
      return { status: 200, body: { tenant_id: 'eng', kind: 'group', principal: { email: me }, role: { name: 'admin' } } }
    }
    if (m === 'GET' && u.startsWith('/v1/secrets/overview')) {
      return { status: 200, body: { tenant_id: 'eng', scope: 'workspace', personal_tenant_id: null, can_manage: true, rows } }
    }
    if (m === 'GET' && u.startsWith('/v1/repositories')) return { status: 200, body: { repositories: [] } }
    if (m === 'GET' && u.startsWith('/v1/workspace/secrets/consumers')) return { status: 200, body: { consumers: [], kinds: ['api_key'], providers: ['typesafe'] } }
    if (m === 'GET' && u.startsWith('/v1/workspace/secrets')) return { status: 200, body: { tenant_id: 'eng', can_manage: true, secrets: [SECRET] } }
    return null
  })
}

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../Secrets')
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.restoreAllMocks()
})

/** What the (i) named `About <label>` says, read through its aria-describedby (no-prose rule, 5 November 2026). */
function tip(scope: HTMLElement, label: string): string {
  const button = within(scope).getByRole('button', { name: `About ${label}` })
  return document.getElementById(button.getAttribute('aria-describedby') ?? '')?.textContent ?? ''
}

/** The words a reader sees with every (i)'s card shut. */
function shown(el: Element): string {
  const clone = el.cloneNode(true) as Element
  for (const n of clone.querySelectorAll('[data-help-description]')) n.remove()
  return visible(clone)
}

async function keysCard(rows: Json[], me: string | null = null): Promise<HTMLElement> {
  api(rows, me)
  const { SecretsScreen } = await load()
  render(<SecretsScreen view={null} />)
  const table = await screen.findByRole('table', { name: 'Keys for agents and MCP' })
  return table.closest('section') as HTMLElement
}

describe('config2-05: the TypeSafe key and the TypeSafe secret are told apart', () => {
  it('the key row says it is not the For agents secret', async () => {
    const card = await keysCard([SHADOWED, TYPESAFE_KEY, RUNNER_KEY])
    const row = within(card).getByRole('rowheader', { name: /TypeSafe key/ })
    // MUTATION: drop the `twin` note -> the two set dates read as one key's.
    expect(tip(row, 'TypeSafe key')).toBe('Not the typesafe-key secret in For agents.')
    // MUTATION: draw the note as a line under the name again -> prose on the surface: red.
    expect(shown(row)).not.toMatch(/For agents/)
  })

  it('says nothing of a twin when For agents holds no TypeSafe secret', async () => {
    const card = await keysCard([TYPESAFE_KEY, RUNNER_KEY])
    const row = within(card).getByRole('rowheader', { name: /TypeSafe key/ })
    // MUTATION: draw the note unconditionally -> it names a secret that is not there.
    expect(visible(row)).not.toMatch(/For agents/)
  })
})

describe('config2-14: the hint names the buttons the table draws', () => {
  it('no "Was Settings" history, and no Set the table does not draw', async () => {
    const card = await keysCard([TYPESAFE_KEY, RUNNER_KEY, KB])
    // MUTATION: restore "Was Settings › API keys. Set and Replace open…" -> fails.
    expect(tip(card, 'Keys for agents and MCP')).toBe('Replace and Set kb’s credential open the one-time form; no value is shown again.')
    // MUTATION: put the hint back under the table -> prose on the surface: red.
    expect(card.querySelector('.sec-hint')).toBeNull()
    expect(shown(card)).not.toMatch(/one-time form/)
    expect(within(card).queryByRole('button', { name: /^Set$/ })).toBeNull()
  })
})

describe('config2-15: one label for storing an MCP server’s credential', () => {
  it('the lead and the keys table both read "Set kb’s credential"', async () => {
    const card = await keysCard([TYPESAFE_KEY, KB])
    // MUTATION: put back "Add one" in the keys table -> fails.
    expect(within(card).getByRole('button', { name: 'Set kb’s credential' })).not.toBeNull()
    expect(screen.queryByRole('button', { name: /^Add one$|^Add a secret for kb$/ })).toBeNull()
    // MUTATION: put back "Add a secret for kb" in the lead -> two buttons are not found.
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Set kb’s credential' })).toHaveLength(2))
  })
})

describe('config2-16: a shadow note in the reader’s words', () => {
  it('the signed-in person’s own secret is "your own"', async () => {
    api([SHADOWED, TYPESAFE_KEY], 'ana@example.com')
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={null} />)
    const table = await screen.findByRole('table', { name: /workspace secrets/i })
    // MUTATION: drop the `me` comparison in `winnerWords` -> "ana@example.com’s wins on their agents".
    await within(table).findByRole('button', { name: 'About typesafe-key shadowed' })
    expect(tip(table, 'typesafe-key shadowed')).toBe('your own wins on your agents')
    expect(visible(table)).not.toMatch(/ana@example\.com’s wins/)
    // MUTATION: draw the reason beside the word again -> prose on the surface: red.
    expect(shown(table)).not.toMatch(/wins on/)
  })

  it('someone else’s secret keeps their name', async () => {
    api([SHADOWED, TYPESAFE_KEY], 'bob@example.com')
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={null} />)
    const table = await screen.findByRole('table', { name: /workspace secrets/i })
    // MUTATION: say "your own" for every personal winner -> fails.
    await within(table).findByRole('button', { name: 'About typesafe-key shadowed' })
    expect(tip(table, 'typesafe-key shadowed')).toBe('ana@example.com’s wins on their agents')
  })
})

// THE NO-PROSE RULE (owner's standing rule, 5 November 2026): the page's other
// explanations are the (i)s of the labels they explained, and a way out is a button.
describe('Config › Secrets carries no explanatory prose on its surface', () => {
  it('moves the scope order, the write-only note and the token note behind their (i)s', async () => {
    api([SHADOWED, TYPESAFE_KEY, { section: 'github', store: 'git_tokens', name: 'swarm-tenant-eng-git', kind: 'app_user', status: 'active', set_at: daysAgo(1), set_by: null, consumers: [], can_manage: true, forge_login: 'ana', verified_at: daysAgo(1), expires_at: null, token_scope: 'user' }])
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={null} />)
    await screen.findByRole('table', { name: /workspace secrets/i })
    // MUTATION: put any of the three paragraphs back -> it is on the surface: red.
    await waitFor(() => expect(shown(document.body)).not.toMatch(/Same name in several scopes|stored write-only|no part of a token/))
    expect(tip(document.body, 'Scope')).toBe('Same name in several scopes: yours wins, then the repository’s, then the workspace’s.')
    expect(tip(document.body, 'Granted to')).toMatch(/A value is stored write-only: no page and no API route shows it/)
    expect(tip(document.body, 'GitHub token')).toBe('Status only: no part of a token is ever shown.')
    // MUTATION: put the "Connect or disconnect on GitHub access" text link back -> no button: red.
    const out = screen.getByRole('link', { name: 'Connect or disconnect on GitHub access' })
    expect(out.getAttribute('href')).toBe('/config/github')
    expect(out.className).toMatch(/\bc-btn\b/)
  })

  it('gives one missing credential its reason behind an (i) on the lead', async () => {
    api([TYPESAFE_KEY, KB])
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={null} />)
    const lead = await screen.findByRole('region', { name: 'Needed and not set' })
    // MUTATION: draw `why` as the lead's line again -> prose on the surface: red.
    expect(shown(lead)).not.toMatch(/fails until/)
    expect(tip(lead, 'MCP server kb’s credential is not set')).toBe('Every call to kb fails until a secret is granted to mcp:kb.')
  })
})
