// CONFIG › SECRETS, THE REST OF S4 (lane SECRETS-ACTIONS-MOUNT, part of #2158;
// docs/design/secrets-scopes.md §6.1 and §11, drawn as proposal E S4a-S4i,
// docs/web-ui/mockups/config-e.html). SSC-UI drew the page and SSC-UI-ACTIONS
// built the GitHub Actions pane; nothing mounted the pane. Held here:
//
//   * S4b2 / S4f: the repository's GitHub Actions destination draws
//     SecretsActions (repository secrets, environments, Create on GitHub,
//     Check rule), not a record and a "not drawn yet" line;
//   * an Actions one-time link opens SecretsActions' form, the token out of
//     the address before paint and sent once, with the write;
//   * S4c's Actions path: the environments with their rules, "checked, you
//     are" from `can_write`, GitHub's name rule (no GITHUB_, no leading
//     digit), "read by workflows in <env>", the terminal line, and the
//     hand-off to the Actions form;
//   * S4a: the lead counts the workspace TypeSafe key the setup read says is
//     missing, so the strip and the lead agree;
//   * S4d: "Set another in this place", and the size hint;
//   * S4h: a member copies a note for an admin naming the secret and scope;
//   * S4i: "Step 3 · Add your first secret" when nothing is set;
//   * S4b: Sign-in for QA has Set / Replace per row.
//
// Fixtures only; no network. Every value and token is built at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null

const fakeValue = () => ['act', 'mnt'].join('-') + 'Qp3'.repeat(12)
const linkToken = () => ['lnk', 'mnt'].join('') + 'Rb5'.repeat(10)
const AT = '2026-10-20T09:30:00+00:00'
const REPO_A = 'repo_0a1b2c3d4e5f6071'
const REPOSITORY = 'example-org/example-api'
const ACTIONS = `/v1/repositories/${REPO_A}/actions`
const SETTINGS = `https://github.com/${REPOSITORY}/settings/environments`

function registration(): Json {
  return { repo_id: REPO_A, tenant_id: 'eng', forge: 'github', owner: 'example-org', repo: 'example-api', default_branch: 'main', allowed_profiles: ['claude-code'], created_by: 'ana@example.com', created_at: AT }
}

function rule(environment: string, kind: string | null): Json {
  return {
    environment, exists: kind !== null, rule: kind, main_only: kind === 'main_only', branch: 'main',
    branches: kind === 'main_only' ? [{ name: 'main', type: 'branch' }] : [],
    create_url: kind === null ? `${SETTINGS}/new` : null, settings_url: SETTINGS,
  }
}

function actionsList(over: Json = {}): Json {
  return {
    tenant_id: 'eng', repo_id: REPO_A, repository: REPOSITORY,
    repository_secrets: [{ name: 'TEST_DB_PASSWORD', created_at: AT, updated_at: AT, set_by_swarmcloud: true, set_by: 'ana@example.com', set_at: AT }],
    environments: [
      { environment: 'repo-secrets', rule: rule('repo-secrets', 'main_only'),
        secrets: [{ name: 'MERGE_APP_PRIVATE_KEY', created_at: AT, updated_at: AT, set_by_swarmcloud: true, set_by: 'ana@example.com', set_at: AT }] },
      { environment: 'staging', rule: rule('staging', 'any_branch'), secrets: [] },
    ],
    environments_truncated: false,
    create_url: `${SETTINGS}/new`,
    can_write: true,
    cannot_write_because: null,
    checked_at: new Date(Date.now() - 60_000).toISOString(),
    ...over,
  }
}

interface Opts {
  overview?: (scope: string) => Json
  list?: Json
  token?: string
  write?: (m: string, url: string, body: unknown) => Reply
}

function api(o: Opts = {}) {
  const token = o.token ?? linkToken()
  const calls = serve((m, url, body) => {
    const u = decodeURIComponent(url)
    if (m === 'GET' && u.startsWith('/v1/secrets/overview')) {
      const q = new URLSearchParams(u.slice(u.indexOf('?') + 1))
      const scope = q.get('scope') ?? 'workspace'
      return { status: 200, body: o.overview?.(scope) ?? { tenant_id: 'eng', scope, repo_id: REPO_A, repository: REPOSITORY, personal_tenant_id: 'u-ana', can_manage: true, rows: [] } }
    }
    if (m === 'GET' && u.startsWith(`${ACTIONS}/secrets`)) return { status: 200, body: actionsList() }
    const ruled = m === 'GET' ? /\/actions\/environments\/([^/]+)\/rule/.exec(u) : null
    if (ruled !== null) return { status: 200, body: { repo_id: REPO_A, ...rule(ruled[1]!, ruled[1] === 'repo-secrets' ? 'main_only' : 'any_branch') } }
    if (m === 'POST' && u.startsWith(`${ACTIONS}/links`)) {
      const b = body as Json
      const path = `/config/secrets?scope=repository&repo=${REPO_A}&dest=actions&name=${String(b.name)}&t=${token}`
      return { status: 201, body: { url: null, path, name: b.name, environment: b.environment, expires_at: AT } }
    }
    if (m === 'PUT' && u.startsWith(`${ACTIONS}/secrets/`)) return { status: 200, body: { secret: { name: 'X' }, created: true, via_link: true, rule: null, note: 'n' } }
    if (m === 'GET' && u.startsWith('/v1/repositories') && !u.includes('/secrets') && !u.includes('/actions')) return { status: 200, body: { repositories: [registration()] } }
    if (m === 'GET' && u.startsWith('/v1/workspace/secrets/consumers')) return { status: 200, body: { consumers: [], kinds: ['api_key'], providers: ['other'] } }
    if (m === 'GET' && (u.startsWith('/v1/workspace/secrets') || /^\/v1\/repositories\/[^/]+\/secrets$/.test(u))) {
      return { status: 200, body: o.list ?? { tenant_id: 'eng', can_manage: true, secrets: [] } }
    }
    if (m === 'POST' && u.endsWith('/links')) return { status: 201, body: { url: null, path: `/config/secrets?action=add&t=${token}`, expires_at: AT } }
    return o.write?.(m, u, body) ?? null
  })
  return { calls, token }
}

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const page = await import('../Secrets')
  const setup = await import('../ConfigSetup')
  return { ...page, SetupContext: setup.SetupContext }
}

/** The onboarding read the shell hands Config pages, carrying `config_setup.secrets`. */
function setupDoc(items: Json[]): Json {
  return {
    config_setup: {
      secrets: { needed: items.length, done: items.length === 0, items, sources: { mcp_servers: { needed: 0, reason: null }, qa_targets: { needed: 0, reason: null }, provider_key: { needed: items.length, reason: null } } },
      people: { applies: true, done: true, holders: 1, admins: 1, reason: null },
    },
  }
}

const TYPESAFE_ITEM: Json = { kind: 'provider_key', name: 'typesafe', needed_by: 'jev', where: 'settings/keys' }

function leaks(needle: string): string[] {
  const where: string[] = []
  if (document.documentElement.outerHTML.includes(needle)) where.push('the document')
  if (window.location.href.includes(needle)) where.push('the URL')
  for (const el of Array.from(document.querySelectorAll('input'))) if (el.value.includes(needle)) where.push('an input')
  return where
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

describe('S4b2 / S4f: the GitHub Actions destination', () => {
  it('draws SecretsActions: repository secrets, environments, Create on GitHub and Check rule', async () => {
    api()
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={`scope=repository&repo=${REPO_A}&dest=actions`} />)
    const secrets = await screen.findByRole('table', { name: /repository secrets of example-org\/example-api/i })
    // MUTATION: draw ActionsRecord ("Written by SwarmCloud") in its place -> fails.
    expect(visible(secrets)).toMatch(/TEST_DB_PASSWORD/)
    const envs = screen.getByRole('region', { name: /^environments$/i })
    expect(visible(envs)).toMatch(/repo-secrets/)
    expect(within(envs).getByRole('link', { name: /create on github/i })).toBeTruthy()
    expect(within(envs).getByRole('button', { name: /check rule/i })).toBeTruthy()
    // The can_write banner (S4b2): GitHub, asked, calls this person admin.
    expect(visible(document.body)).toMatch(/you may write here: workspace admin, and github calls you admin of example-org\/example-api/i)
    // MUTATION: keep the old "does not draw yet" line -> fails.
    expect(visible(document.body)).not.toMatch(/does not draw yet|not drawn yet/i)
    expect(screen.queryByText(/written by swarmcloud/i)).toBeNull()
  })
})

describe('an Actions one-time link', () => {
  it('opens SecretsActions\' form, drops the token before paint and sends it once with the write', async () => {
    const value = fakeValue()
    const token = linkToken()
    const view = `scope=repository&repo=${REPO_A}&dest=actions&name=MERGE_APP_PRIVATE_KEY&env=repo-secrets`
    window.history.replaceState(null, '', `/config/secrets?${view}&t=${token}`)
    const { calls } = api({ token })
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={view} token={token} />)
    // MUTATION: drop the token in an effect after paint, or not at all -> fails.
    expect(window.location.search).not.toContain(token)
    const dialog = await screen.findByRole('dialog', { name: /set a github actions secret/i })
    const field = (await within(dialog).findByLabelText(/^value$/i)) as HTMLInputElement
    expect(visible(dialog)).toMatch(/opened from a one-time link/i)
    expect(visible(dialog)).toMatch(/MERGE_APP_PRIVATE_KEY/)
    // MUTATION: keep the "not drawn yet" banner -> fails.
    expect(visible(document.body)).not.toMatch(/does not draw yet/i)
    fireEvent.change(field, { target: { value } })
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: /^set secret$/i }))
    })
    await screen.findByText(/MERGE_APP_PRIVATE_KEY was set/i)
    const puts = calls.filter((c) => c.method === 'PUT')
    expect(puts).toHaveLength(1)
    // MUTATION: lose the token on its way to the pane, or mint a second link -> fails.
    expect(puts[0]!.body).toEqual({ value, environment: 'repo-secrets', link_token: token })
    expect(calls.some((c) => c.method === 'POST' && c.url.includes('/actions/links'))).toBe(false)
    expect(leaks(token)).toEqual([])
    expect(leaks(value)).toEqual([])
  })
})

describe('S4c: the wizard\'s GitHub Actions path', () => {
  async function toStep2() {
    const { calls } = api()
    const { SecretsScreen } = await load()
    const utils = render(<SecretsScreen view={`scope=repository&repo=${REPO_A}`} />)
    await screen.findByRole('heading', { name: 'Secrets' })
    fireEvent.click(await screen.findByRole('button', { name: /^add a secret$/i }))
    const sheet = screen.getByRole('dialog', { name: /add a secret/i })
    expect((within(sheet).getByRole('radio', { name: /^repository/i }) as HTMLInputElement).checked).toBe(true)
    fireEvent.click(within(sheet).getByRole('button', { name: /^next$/i }))
    return { sheet, calls, SecretsScreen, ...utils }
  }

  it('offers the environments with their rules, Create on GitHub and Check rule, and says GitHub admin is checked', async () => {
    const { sheet, calls } = await toStep2()
    const actions = within(sheet).getByRole('radio', { name: /github actions/i }) as HTMLInputElement
    // MUTATION: leave GitHub Actions disabled -> fails.
    expect(actions.disabled).toBe(false)
    fireEvent.click(actions)
    const envs = await within(sheet).findByRole('group', { name: /^environment$/i })
    const names = within(envs).getAllByRole('radio').map((r) => (r.closest('label')?.textContent ?? '').replace(/\s+/g, ' '))
    // MUTATION: drop repository level, or an environment's rule -> fails.
    expect(names[0]).toMatch(/^Repository level/)
    expect(names[1]).toMatch(/^repo-secrets · main only/)
    expect(names[2]).toMatch(/^staging · any branch/)
    expect(within(sheet).getByRole('link', { name: /create on github/i }).getAttribute('href')).toBe(`${SETTINGS}/new`)
    // MUTATION: say "checked, you are" without can_write -> the any-reader fixture below fails.
    expect(visible(sheet)).toMatch(/GitHub admin of the repository: checked, you are/)
    // Next waits for an environment choice.
    expect((within(sheet).getByRole('button', { name: /^next$/i }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(within(envs).getByRole('radio', { name: /^staging/ }))
    await act(async () => {
      fireEvent.click(within(sheet).getByRole('button', { name: /check rule/i }))
    })
    // Check rule is a GET and writes nothing (§11 Q4).
    await waitFor(() => expect(calls.some((c) => c.method === 'GET' && c.url.includes('/actions/environments/staging/rule'))).toBe(true))
    expect(calls.filter((c) => c.method !== 'GET')).toEqual([])
  })

  it('refuses a GITHUB_ or a digit-led name, says who reads it, and hands off to the Actions one-time form', async () => {
    const { sheet, calls, rerender, SecretsScreen } = await toStep2()
    fireEvent.click(within(sheet).getByRole('radio', { name: /github actions/i }))
    fireEvent.click(await within(sheet).findByRole('radio', { name: /^repo-secrets/ }))
    fireEvent.click(within(sheet).getByRole('button', { name: /^next$/i }))
    // S4c3: the environment's rule decides who reads it.
    expect(visible(sheet)).toMatch(/read by workflows that run in repo-secrets, on main only/i)
    const name = within(sheet).getByLabelText(/^name$/i)
    const next = () => within(sheet).getByRole('button', { name: /^next$/i }) as HTMLButtonElement
    // MUTATION: use the Agents path's rule (lower-case, hyphens) or none -> fails.
    for (const bad of ['GITHUB_TOKEN_X', '1PASSWORD', 'has-hyphen']) {
      fireEvent.change(name, { target: { value: bad } })
      expect(next().disabled, bad).toBe(true)
    }
    fireEvent.change(name, { target: { value: 'GITHUB_TOKEN_X' } })
    expect(visible(sheet)).toMatch(/GITHUB_/)
    fireEvent.change(name, { target: { value: 'MERGE_APP_PRIVATE_KEY' } })
    expect(next().disabled).toBe(false)
    fireEvent.click(next())
    // S4c4: the terminal line names the destination and its environment.
    expect(visible(sheet)).toContain('/sc secrets link repository MERGE_APP_PRIVATE_KEY --repo example-org/example-api --actions --env repo-secrets')
    expect(sheet.querySelector('input[type="password"]')).toBeNull()
    await act(async () => {
      fireEvent.click(within(sheet).getByRole('button', { name: /open the one-time form/i }))
    })
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url.startsWith(`${ACTIONS}/links`))).toBe(true))
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ name: 'MERGE_APP_PRIVATE_KEY', environment: 'repo-secrets' })
    // The hand-off: the Actions destination, its form open, no token in the address.
    await waitFor(() => expect(window.location.search).toBe(`?scope=repository&repo=${REPO_A}&dest=actions&name=MERGE_APP_PRIVATE_KEY&env=repo-secrets`))
    rerender(<SecretsScreen view={window.location.search.slice(1)} />)
    const form = await screen.findByRole('dialog', { name: /set a github actions secret/i })
    expect(await within(form).findByLabelText(/^value$/i)).toBeTruthy()
    expect(leaks(linkToken())).toEqual([])
  })

  it('does not say "checked, you are" when GitHub was not asked or said no', async () => {
    const list = actionsList({ can_write: false, cannot_write_because: 'GitHub does not call you admin of example-org/example-api' })
    serve((m, url) => {
      const u = decodeURIComponent(url)
      if (m !== 'GET') return null
      if (u.startsWith(`${ACTIONS}/secrets`)) return { status: 200, body: list }
      if (u.startsWith('/v1/secrets/overview')) return { status: 200, body: { tenant_id: 'eng', scope: 'repository', repo_id: REPO_A, repository: REPOSITORY, personal_tenant_id: 'u-ana', can_manage: true, rows: [] } }
      if (u.startsWith('/v1/repositories') && !u.includes('/secrets')) return { status: 200, body: { repositories: [registration()] } }
      if (u.startsWith('/v1/workspace/secrets/consumers')) return { status: 200, body: { consumers: [], kinds: ['api_key'], providers: ['other'] } }
      return { status: 200, body: { tenant_id: 'eng', can_manage: true, secrets: [] } }
    })
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={`scope=repository&repo=${REPO_A}`} />)
    fireEvent.click(await screen.findByRole('button', { name: /^add a secret$/i }))
    const sheet = screen.getByRole('dialog', { name: /add a secret/i })
    fireEvent.click(within(sheet).getByRole('button', { name: /^next$/i }))
    fireEvent.click(within(sheet).getByRole('radio', { name: /github actions/i }))
    await within(sheet).findByRole('group', { name: /^environment$/i })
    expect(visible(sheet)).not.toMatch(/checked, you are/)
    expect(visible(sheet)).toMatch(/GitHub does not call you admin of example-org\/example-api/)
  })
})

describe('S4a: the lead counts a missing workspace TypeSafe key', () => {
  it('names the TypeSafe key the setup read lists, and Set opens its form', async () => {
    api({ overview: () => ({ tenant_id: 'eng', scope: 'workspace', personal_tenant_id: 'u-ana', can_manage: true, rows: [
      { section: 'keys', store: 'mcp_servers', name: 'kb', status: 'unset', consumer: 'mcp:kb', secrets: [], consumers: [], can_manage: true, set_at: null, set_by: null, shadowed_by: [], shadows: [] },
      { section: 'keys', store: 'provider_keys', name: 'typesafe', status: 'unset', consumers: [], can_manage: true, set_at: null, set_by: null, shadowed_by: [], shadows: [] },
    ] }) })
    const { SecretsScreen, SetupContext } = await load()
    const wrap = (children: ReactNode) => (
      <SetupContext.Provider value={{ doc: { status: 'ok', data: setupDoc([{ kind: 'mcp_server', name: 'kb', needed_by: 'mcp:kb', where: 'config/mcp-servers' }, TYPESAFE_ITEM]) as never, fetchedAt: Date.now() }, personal: false }}>
        {children}
      </SetupContext.Provider>
    )
    render(wrap(<SecretsScreen view={null} />))
    const lead = await screen.findByRole('region', { name: /needed and not set/i })
    // MUTATION: count only MCP and QA rows -> the lead says 1 while the strip says 2, and this fails.
    expect(visible(lead)).toMatch(/2 workspace secrets are needed and not set/i)
    expect(visible(lead)).toMatch(/TypeSafe key for the workspace/i)
    fireEvent.click(within(lead).getByRole('button', { name: /set typesafe key/i }))
    expect(window.location.search).toBe('?key=typesafe')
  })

  it('does not count the key when the setup read does not list it (Jev does not judge with TypeSafe)', async () => {
    api({ overview: () => ({ tenant_id: 'eng', scope: 'workspace', personal_tenant_id: 'u-ana', can_manage: true, rows: [
      { section: 'keys', store: 'provider_keys', name: 'typesafe', status: 'unset', consumers: [], can_manage: true, set_at: null, set_by: null, shadowed_by: [], shadows: [] },
    ] }), list: { tenant_id: 'eng', can_manage: true, secrets: [{ name: 'search-api', provider: 'brave', kind: 'api_key', current_version: '1', created_at: AT, created_by: 'a', rotated_at: null, rotated_by: null, grants: [] }] } })
    const { SecretsScreen, SetupContext } = await load()
    render(
      <SetupContext.Provider value={{ doc: { status: 'ok', data: setupDoc([]) as never, fetchedAt: Date.now() }, personal: false }}>
        <SecretsScreen view={null} />
      </SetupContext.Provider>,
    )
    await screen.findByRole('table', { name: /keys for agents and mcp/i })
    expect(screen.queryByRole('region', { name: /needed and not set/i })).toBeNull()
  })
})

describe('S4h: a member copies a note for an admin', () => {
  it('copies a note naming the secret and the scope', async () => {
    const writeText = vi.fn(async () => undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    api({ overview: () => ({ tenant_id: 'eng', scope: 'workspace', personal_tenant_id: 'u-ana', can_manage: false, rows: [
      { section: 'keys', store: 'mcp_servers', name: 'kb', status: 'unset', consumer: 'mcp:kb', secrets: [], consumers: [], can_manage: false, set_at: null, set_by: null, shadowed_by: [], shadows: [] },
    ] }), list: { tenant_id: 'eng', can_manage: false, secrets: [] } })
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={null} />)
    const lead = await screen.findByRole('region', { name: /needed and not set/i })
    expect(visible(lead)).toMatch(/a workspace admin sets/i)
    await act(async () => {
      fireEvent.click(within(lead).getByRole('button', { name: /copy a note for an admin/i }))
    })
    // MUTATION: copy nothing, or a note without the secret or the scope -> fails.
    expect(writeText).toHaveBeenCalledTimes(1)
    const note = String((writeText.mock.calls[0] as unknown[])[0])
    expect(note).toMatch(/MCP server kb/)
    expect(note).toMatch(/workspace eng/i)
    expect(note).toMatch(/Config › Secrets/)
    await within(lead).findByText(/note copied/i)
  })
})

describe('S4i: the empty workspace', () => {
  it('leads with "Step 3 · Add your first secret" when nothing is set, and its button opens the sheet', async () => {
    api()
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={null} />)
    const lead = await screen.findByRole('region', { name: /add your first secret/i })
    // MUTATION: drop the lead, or draw it over a workspace that has secrets -> fails (here and below).
    expect(visible(lead)).toMatch(/^Step 3 · next/)
    expect(visible(lead)).toMatch(/Add your first secret/)
    expect(visible(lead)).toMatch(/nothing is stored for eng yet/i)
    // The header does not draw Add a secret a second time (SECHD0).
    expect(screen.getAllByRole('button', { name: /^add a secret$/i })).toHaveLength(1)
    fireEvent.click(within(lead).getByRole('button', { name: /^add a secret$/i }))
    expect(screen.getByRole('dialog', { name: /add a secret/i })).toBeTruthy()
  })

  it('is not drawn once a secret is stored', async () => {
    api({ overview: () => ({ tenant_id: 'eng', scope: 'workspace', personal_tenant_id: 'u-ana', can_manage: true, rows: [
      { section: 'agents', store: 'workspace_secrets', name: 'search-api', scope: 'workspace', kind: 'api_key', status: 'set', set_at: AT, set_by: 'a', consumers: [], shadowed_by: [], shadows: [], can_manage: true },
    ] }) })
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={null} />)
    await screen.findByRole('heading', { name: 'Secrets' })
    await screen.findByRole('button', { name: /^add a secret$/i })
    expect(screen.queryByRole('region', { name: /add your first secret/i })).toBeNull()
  })
})

describe('S4d: the one-time form', () => {
  it('says the size limit, and "Set another in this place" opens a fresh form for the same store', async () => {
    let posts = 0
    const { calls } = api({
      write: (m, u) => {
        if (m === 'POST' && u === '/v1/workspace/secrets') {
          posts += 1
          return { status: 201, body: { secret: { name: `s${posts}` }, via_link: true } }
        }
        return null
      },
    })
    const { SecretsScreen } = await load()
    render(<SecretsScreen view="action=add" />)
    const form = await screen.findByRole('form', { name: /add a secret/i })
    // MUTATION: drop the hint -> fails.
    expect(visible(document.body)).toMatch(/8 bytes to 8 KiB/)
    fireEvent.change(within(form).getByLabelText(/^name/i), { target: { value: 'first-one' } })
    fireEvent.change(within(form).getByLabelText(/^value/i), { target: { value: fakeValue() } })
    await act(async () => {
      fireEvent.click(within(form).getByRole('button', { name: /^add/i }))
    })
    await screen.findByText(/first-one was written/i)
    const mints = () => calls.filter((c) => c.method === 'POST' && c.url === '/v1/workspace/secrets/links').length
    expect(mints()).toBe(1)
    // MUTATION: offer only Back to Secrets -> fails.
    fireEvent.click(screen.getByRole('button', { name: /set another in this place/i }))
    const again = await screen.findByRole('form', { name: /add a secret/i })
    expect((within(again).getByLabelText(/^name/i) as HTMLInputElement).value).toBe('')
    // A fresh form mints its own link: the first one is used up.
    await waitFor(() => expect(mints()).toBe(2))
  })
})

describe('S4b: Sign-in for QA', () => {
  it('has Set on a row that is not set and Replace on one that is, each to QA targets', async () => {
    api({ overview: () => ({ tenant_id: 'eng', scope: 'repository', repo_id: REPO_A, repository: REPOSITORY, personal_tenant_id: 'u-ana', can_manage: true, rows: [
      { section: 'qa_sign_in', store: 'qa_targets', name: 'Studio', target_id: 'qt1', environment: 'staging', field: 'credential', status: 'set', set_at: AT, set_by: 'a', consumers: [], shadowed_by: [], shadows: [], can_manage: true },
      { section: 'qa_sign_in', store: 'qa_targets', name: 'Studio', target_id: 'qt1', environment: 'production', field: 'credential', status: 'unset', set_at: null, set_by: null, consumers: [], shadowed_by: [], shadows: [], can_manage: true },
    ] }) })
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={`scope=repository&repo=${REPO_A}`} />)
    const table = await screen.findByRole('table', { name: /sign-in for qa/i })
    // MUTATION: draw the rows without their own action -> fails.
    const replace = within(within(table).getByRole('row', { name: /staging/ })).getByRole('link', { name: /replace/i })
    const set = within(within(table).getByRole('row', { name: /production/ })).getByRole('link', { name: /^set/i })
    expect(replace.getAttribute('href')).toBe('/config/repositories?page=qa-targets')
    expect(set.getAttribute('aria-label')).toMatch(/Studio · production/)
  })
})
