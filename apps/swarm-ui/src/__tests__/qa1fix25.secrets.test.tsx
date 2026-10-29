// CONFIG › SECRETS, VISUAL QA PASS 1 (QA1-FIX-25; part of #2596). The
// findings this lane fixes in Secrets.tsx and styles/secrets.css, each held
// to what the reader sees:
//
//   * config-21: a GitHub token whose expiry has passed reads "expired …",
//     in the warn tone, and its State no longer reads "active" in the ok tone
//     (since QA2-FIX-09 it reads GitHub access's "active · token renews on
//     next use");
//   * config-23: the shadow note is a plain line of small text under the
//     name, not a dashed pill, and Rotate and Delete never wrap apart;
//   * config-24: the page head keeps no bottom margin of its own, so the tab
//     row sits where GitHub access and Repositories draw theirs;
//   * chrome-C21: a runner key, whose set time and setter the API does not
//     record, says so with a dash and its reason instead of a bare "set".
//
// Fixtures only; no network.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>

const NOW = Date.parse('2026-10-28T10:00:00Z')
const daysAgo = (d: number) => new Date(NOW - d * 86_400_000).toISOString()

function api(rows: Json[], secrets: Json[] = []) {
  return serve((m, url) => {
    const u = decodeURIComponent(url)
    if (m === 'GET' && u.startsWith('/v1/secrets/overview')) {
      return { status: 200, body: { tenant_id: 'eng', scope: 'workspace', personal_tenant_id: null, can_manage: true, rows } }
    }
    if (m === 'GET' && u.startsWith('/v1/repositories')) return { status: 200, body: { repositories: [] } }
    if (m === 'GET' && u.startsWith('/v1/workspace/secrets/consumers')) return { status: 200, body: { consumers: [], kinds: ['api_key'], providers: ['typesafe'] } }
    if (m === 'GET' && u.startsWith('/v1/workspace/secrets')) return { status: 200, body: { tenant_id: 'eng', can_manage: true, secrets } }
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

const token = (expires_at: string) => ({
  section: 'github', store: 'git_tokens', name: 'swarm-tenant-eng-git', scope: 'tenant', kind: 'app_user',
  status: 'active', forge_login: 'ana-example', verified_at: daysAgo(1), expires_at,
  set_at: daysAgo(3), set_by: 'ana@example.com', consumers: [], can_manage: false, page: '/repositories/tokens',
})

describe('config-21: a GitHub token past its expiry', () => {
  it('reads expired, in the warn tone, and is not drawn as active', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(NOW)
    api([token(daysAgo(9))])
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={null} />)
    const card = (await screen.findByRole('heading', { name: 'GitHub token' })).closest('section') as HTMLElement
    await waitFor(() => expect(visible(card)).toMatch(/Expires/))
    const expires = within(card).getByText(/expired 9d ago/)
    // MUTATION: render a past expiry with `when()` alone ("9d ago") -> fails.
    expect(expires.className).toMatch(/is-warn/)
    // config2-02 (QA2-FIX-09): the State word is GitHub access's, from
    // `githubTokenState`, not a separate `expired`.
    const state = within(card).getByText(/^active · token renews on next use$/)
    // MUTATION: keep `r.status` with is-ok for an expired token, or put back
    // Secrets' own `expired` word -> fails.
    expect(state.className).not.toMatch(/is-ok/)
    expect(state.className).toMatch(/is-warn/)
  })

  it('a token still in date keeps its state and its "in …" expiry', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(NOW)
    api([token(new Date(NOW + 7 * 3_600_000).toISOString())])
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={null} />)
    const card = (await screen.findByRole('heading', { name: 'GitHub token' })).closest('section') as HTMLElement
    await waitFor(() => expect(visible(card)).toMatch(/in 7 h/))
    // MUTATION: mark every token expired -> fails.
    expect(within(card).getByText(/^active$/).className).toMatch(/is-ok/)
    expect(visible(card)).not.toMatch(/expired/)
  })
})

describe('config-23: the shadow note is a line of small text', () => {
  it('names the winner under the name, without a pill', async () => {
    api(
      [{ section: 'agents', store: 'workspace_secrets', name: 'typesafe-key', scope: 'workspace', kind: 'api_key', status: 'set', set_at: daysAgo(2), set_by: 'ana@example.com',
        consumers: [], can_manage: true, shadows: [],
        shadowed_by: [{ scope: 'personal', where: 'agents of ana@example.com', person: 'ana@example.com', slot: 'typesafe' }] }],
      [{ name: 'typesafe-key', provider: 'typesafe', kind: 'api_key', current_version: '1', created_at: daysAgo(2), created_by: 'ana@example.com', rotated_at: null, rotated_by: null, grants: [] }],
    )
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={null} />)
    const table = await screen.findByRole('table', { name: /workspace secrets/i })
    const note = await within(table).findByText(/shadowed: ana@example\.com’s wins on their agents/)
    // MUTATION: draw it as `sec-chip is-shadowed` again -> fails.
    expect(note.className).not.toMatch(/sec-chip/)
    expect(note.tagName).toBe('SMALL')
  })

  it('holds Rotate and Delete on one line', () => {
    const css = readFileSync(join(__dirname, '..', 'styles', 'secrets.css'), 'utf8')
    // MUTATION: delete the rule -> fails.
    expect(css).toMatch(/\.sec-page \.mcp-servers td\[data-label='Actions'\] \.pk-form-row \{[^}]*flex-wrap: nowrap/)
  })
})

describe('config-24: the head keeps the Config pages\' height', () => {
  it('drops the head\'s own bottom margin, as `.ur-page` does', () => {
    const css = readFileSync(join(__dirname, '..', 'styles', 'secrets.css'), 'utf8')
    // MUTATION: delete the rule -> fails (the tab row sits 12px lower).
    expect(css).toMatch(/\.sec-page > \.c-phead \{ margin-bottom: 0; \}/)
  })
})

describe('chrome-C21: a runner key says what is not recorded', () => {
  it('reads set with a dash whose reason names the missing time and setter', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(NOW)
    api([
      { section: 'keys', store: 'provider_keys', name: 'typesafe', status: 'set', set_at: daysAgo(5), set_by: 'ana@example.com', consumers: [], can_manage: true },
      { section: 'keys', store: 'runner_provider_keys', name: 'anthropic', status: 'set', set_at: null, set_by: null, consumers: ['profile:claude-code'], can_manage: true },
    ])
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={null} />)
    const table = await screen.findByRole('table', { name: /keys for agents and mcp/i })
    const row = await within(table).findByRole('row', { name: /anthropic runner key/ })
    // MUTATION: a bare "set" for a runner key -> fails.
    expect(within(row).getByRole('img', { name: /not recorded/i })).toBeTruthy()
    expect(visible(row)).toMatch(/set/)
    // The TypeSafe key, which records both, keeps its words.
    expect(visible(within(table).getByRole('row', { name: /TypeSafe key/ }))).toMatch(/set 5d ago by ana@example\.com/)
  })
})
