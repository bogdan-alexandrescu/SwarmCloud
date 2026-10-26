// CONFIG › SECRETS, THE PAGE (lane SSC-UI, part of #2158;
// docs/design/secrets-scopes.md §6.1 and §11, drawn as proposal E S4,
// docs/web-ui/mockups/config-e.html). Held to the §9 SSC-UI row's tests and
// the brief's two more:
//
//   * no value is rendered after save: a key's value reaches its one PUT body
//     and no element, attribute, field, URL or storage;
//   * the Repository scope lists only registered repositories, and a view
//     naming any other repository reads nothing for it;
//   * a shadowed row shows its winner, and the winner what it overrides (Q1);
//   * the wizard's steps run in E's order -- scope, destination, consumer or
//     grant, one-time form -- and land on that form, which writes to the
//     store step 1 chose and grants what step 3 ticked;
//   * a secret cannot be submitted from the wrong scope: Yours is never
//     written into a group workspace's store, and a repository's form writes
//     to that repository's route alone.
//
// Fixtures only; no network. Every value and token is built at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null

const fakeValue = () => ['sk', 'val'].join('-') + 'Mv8'.repeat(12)
const minted = () => ['own', 'ssc'].join('') + 'Jx6'.repeat(10)
const AT = '2026-10-20T09:30:00+00:00'
const REPO_A = 'repo_0a1b2c3d4e5f6071'
const REPO_B = 'repo_9f8e7d6c5b4a3921'

function registration(repo_id: string, repo: string): Json {
  return { repo_id, tenant_id: 'eng', forge: 'github', owner: 'example-org', repo, default_branch: 'main', allowed_profiles: ['claude-code'], created_by: 'ana@example.com', created_at: AT }
}

function storeRow(name: string, over: Json = {}): Json {
  return { section: 'agents', store: 'workspace_secrets', name, scope: 'workspace', kind: 'api_key', status: 'set', set_at: AT, set_by: 'ana@example.com', consumers: ['profile:claude-code'], shadowed_by: [], shadows: [], can_manage: true, ...over }
}

function record(name: string, over: Json = {}): Json {
  return { name, provider: 'brave', kind: 'api_key', current_version: '1', created_at: AT, created_by: 'ana@example.com', rotated_at: null, rotated_by: null, grants: [], ...over }
}

const CONSUMERS: Json = {
  consumers: [
    { consumer: 'profile:claude-code', kind: 'profile', name: 'claude-code', slots: [{ slot: 'files', kinds: ['api_key', 'opaque'] }], grantable: true, note: '' },
    { consumer: 'tool:jev-browser', kind: 'tool', name: 'jev-browser', slots: [{ slot: 'typesafe', kinds: ['api_key'] }], grantable: true, note: '' },
  ],
  kinds: ['api_key', 'opaque'],
  providers: ['brave', 'typesafe', 'other'],
}

interface Opts {
  overview?: (scope: string, repo: string | null) => Json
  repos?: Json[]
  list?: Json
  write?: (m: string, url: string, body: unknown) => Reply
}

function api(o: Opts = {}) {
  return serve((m, url, body) => {
    const u = decodeURIComponent(url)
    if (m === 'GET' && u.startsWith('/v1/secrets/overview')) {
      const q = new URLSearchParams(u.slice(u.indexOf('?') + 1))
      const scope = q.get('scope') ?? 'workspace'
      return { status: 200, body: o.overview?.(scope, q.get('repo_id')) ?? { tenant_id: 'eng', scope, personal_tenant_id: 'u-ana', can_manage: true, rows: [] } }
    }
    if (m === 'GET' && u.startsWith('/v1/repositories') && !u.includes('/secrets')) return { status: 200, body: { repositories: o.repos ?? [] } }
    if (m === 'GET' && u.startsWith('/v1/workspace/secrets/consumers')) return { status: 200, body: CONSUMERS }
    if (m === 'GET' && (u.startsWith('/v1/workspace/secrets') || /^\/v1\/repositories\/[^/]+\/secrets$/.test(u))) {
      return { status: 200, body: o.list ?? { tenant_id: 'eng', can_manage: true, secrets: [] } }
    }
    if (m === 'POST' && u.endsWith('/links')) return { status: 201, body: { url: null, path: `/config/secrets?action=add&t=${minted()}`, expires_at: AT } }
    return o.write?.(m, u, body) ?? null
  })
}

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../Secrets')
}

function leaks(needle: string): string[] {
  const where: string[] = []
  if (document.documentElement.outerHTML.includes(needle)) where.push('the document')
  if (window.location.href.includes(needle)) where.push('the URL')
  for (const store of [window.localStorage, window.sessionStorage]) {
    for (let i = 0; i < store.length; i++) {
      const k = store.key(i) ?? ''
      if (k.includes(needle) || (store.getItem(k) ?? '').includes(needle)) where.push('storage')
    }
  }
  for (const el of Array.from(document.querySelectorAll('input'))) if (el.value.includes(needle)) where.push('an input')
  return where
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
  localStorage.clear()
  sessionStorage.clear()
})

describe('no value is rendered after save', () => {
  it('a runner key\'s value reaches its one POST body and nothing else, and the form is then used up', async () => {
    const value = fakeValue()
    let atPost: string | null = 'not posted'
    const calls = api({
      write: (m, u) => {
        if (m === 'POST' && u === '/v1/tenants/me/credentials') {
          atPost = (document.querySelector('input[type="password"]') as HTMLInputElement | null)?.value ?? null
          return { status: 201, body: { credential: { provider: 'anthropic' }, via_link: true, note: 'n' } }
        }
        return null
      },
    })
    const { SecretsScreen } = await load()
    render(<SecretsScreen view="scope=workspace&runner_key=anthropic" />)
    const form = await screen.findByRole('form', { name: /set the anthropic runner key/i })
    const field = within(form).getByLabelText(/anthropic api key/i) as HTMLInputElement
    // MUTATION: prefill the field, or make it type="text" -> fails.
    expect(field.value).toBe('')
    expect(field.type).toBe('password')
    fireEvent.change(field, { target: { value } })
    await act(async () => {
      fireEvent.click(within(form).getByRole('button', { name: /set secret/i }))
    })
    await screen.findByText(/anthropic runner key was written/i)
    // MUTATION: empty the field only after the answer -> it still held the value when the POST arrived.
    expect(atPost).toBe('')
    // MUTATION: draw "saved: <value>", keep the field, or store it -> fails.
    expect(leaks(value)).toEqual([])
    const carrying = calls.filter((c) => c.url.includes(value) || JSON.stringify(c.body ?? null).includes(value))
    expect(carrying).toHaveLength(1)
    // MUTATION: post without the form's own link -> fails.
    expect(carrying[0]!.body).toEqual({ provider: 'anthropic', api_key: value, link_token: minted() })
    expect(calls.find((c) => c.url === '/v1/tenants/me/credentials/links')?.body).toEqual({ provider: 'anthropic' })
    expect(screen.queryByRole('form', { name: /set the anthropic runner key/i })).toBeNull()
    expect(leaks(minted())).toEqual([])
  })

  it('a value the overview wrongly serves is never drawn: the rows are an allow-list', async () => {
    const value = fakeValue()
    api({
      overview: () => ({ tenant_id: 'eng', scope: 'workspace', can_manage: true, rows: [
        { section: 'github', store: 'git_tokens', name: 'swarm-tenant-eng-git', status: 'active', last4: value.slice(-4), value, consumers: [] },
        storeRow('search-api', { value, length: value.length }),
      ] }),
      list: { tenant_id: 'eng', can_manage: true, secrets: [record('search-api')] },
    })
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={null} />)
    await screen.findByRole('table', { name: /workspace secrets/i })
    await screen.findByText('swarm-tenant-eng-git', { exact: false }).catch(() => null)
    // MUTATION: spread a raw row into what is drawn, or draw `last4` -> fails.
    expect(leaks(value)).toEqual([])
    expect(visible(document.body)).not.toContain(value.slice(-4))
  })
})

describe('the Repository scope lists only registered repositories', () => {
  it('offers exactly the registrations GET /v1/repositories serves, and reads the chosen one\'s scope', async () => {
    const calls = api({ repos: [registration(REPO_A, 'example-api'), registration(REPO_B, 'example-web')] })
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={`scope=repository&repo=${REPO_B}`} />)
    const picker = await screen.findByLabelText(/^repository$/i) as HTMLSelectElement
    // MUTATION: list GitHub's readable repositories, or add a free-text entry -> fails.
    expect(Array.from(picker.options).map((o) => [o.value, o.textContent])).toEqual([
      [REPO_A, 'example-org/example-api'],
      [REPO_B, 'example-org/example-web'],
    ])
    expect(picker.value).toBe(REPO_B)
    await waitFor(() => expect(calls.some((c) => c.url.includes('/v1/secrets/overview') && c.url.includes(`repo_id=${REPO_B}`))).toBe(true))
  })

  it('reads nothing for a repository that is not registered here, and says so', async () => {
    const stranger = 'repo_00ff00ff00ff00ff'
    const calls = api({ repos: [registration(REPO_A, 'example-api')] })
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={`scope=repository&repo=${stranger}`} />)
    await screen.findByText(/not registered in this workspace/i)
    // MUTATION: read the overview for whatever id the address names -> fails.
    expect(calls.filter((c) => c.url.includes(stranger))).toEqual([])
    expect(calls.filter((c) => c.url.includes('/v1/secrets/overview'))).toEqual([])
  })
})

describe('a shadowed row shows its winner', () => {
  it('the loser names who wins and where; the winner says what it overrides', async () => {
    api({
      repos: [registration(REPO_A, 'example-api')],
      overview: (scope) => ({ tenant_id: 'eng', scope, repo_id: REPO_A, repository: 'example-org/example-api', can_manage: true, rows: [
        storeRow('search-api', {
          scope: 'repository',
          shadows: [{ scope: 'workspace', where: 'agents of this workspace', slot: 'files' }],
          shadowed_by: [{ scope: 'personal', where: 'agents of ana@example.com', person: 'ana@example.com', slot: 'files' }],
        }),
        storeRow('test-db-password', { scope: 'repository' }),
      ] }),
      list: { tenant_id: 'eng', can_manage: true, secrets: [record('search-api'), record('test-db-password', { kind: 'opaque' })] },
    })
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={`scope=repository&repo=${REPO_A}`} />)
    const table = await screen.findByRole('table', { name: /repository secrets/i })
    await waitFor(() => expect(visible(within(table).getByRole('row', { name: /search-api/ }))).toMatch(/shadowed/))
    const row = visible(within(table).getByRole('row', { name: /search-api/ }))
    // MUTATION: drop `shadowed_by` from the row -> fails.
    expect(row).toMatch(/shadowed: ana@example\.com’s wins on their agents/)
    // MUTATION: drop `shadows` -> fails.
    expect(row).toMatch(/overrides the workspace’s/)
    // MUTATION: mark every row -> fails.
    expect(visible(within(table).getByRole('row', { name: /test-db-password/ }))).not.toMatch(/shadowed|overrides/)
  })
})

describe('Add a secret: the wizard', () => {
  it('runs scope, destination, who reads it, one-time form, in that order, and the form writes where step 1 chose', async () => {
    const value = fakeValue()
    const calls = api({
      repos: [registration(REPO_A, 'example-api')],
      write: (m, u) => {
        if (m === 'POST' && u === `/v1/repositories/${REPO_A}/secrets`) return { status: 201, body: { secret: record('search-api'), via_link: true } }
        if (m === 'PUT' && u.startsWith(`/v1/repositories/${REPO_A}/secrets/search-api/grants/`)) return { status: 200, body: { secret: record('search-api') } }
        return null
      },
    })
    const { SecretsScreen } = await load()
    const { rerender } = render(<SecretsScreen view={null} />)
    await screen.findByRole('heading', { name: 'Secrets' })
    fireEvent.click(await screen.findByRole('button', { name: /^add a secret$/i }))
    const sheet = screen.getByRole('dialog', { name: /add a secret/i })
    const steps = within(sheet).getByRole('list', { name: /steps/i })
    // MUTATION: reorder the steps -> fails.
    expect(within(steps).getAllByRole('listitem').map((li) => visible(li).replace(/^[\d✓]+/, ''))).toEqual(['Scope', 'Destination', 'Who reads it', 'One-time form'])
    const current = () => visible(within(steps).getByRole('listitem', { current: 'step' })).replace(/^[\d✓]+/, '')
    const next = () => fireEvent.click(within(sheet).getByRole('button', { name: /^next$/i }))

    expect(current()).toBe('Scope')
    expect(visible(sheet)).toMatch(/where does it live/i)
    fireEvent.click(within(sheet).getByRole('radio', { name: /^repository/i }))
    next()
    expect(current()).toBe('Destination')
    expect(visible(sheet)).toMatch(/who is it for/i)
    // GitHub Actions is offered (SECRETS-ACTIONS-MOUNT, secrets.actionsMount.test.tsx); Agents is the default.
    expect((within(sheet).getByRole('radio', { name: /github actions/i }) as HTMLInputElement).checked).toBe(false)
    expect((within(sheet).getByRole('radio', { name: /^agents/i }) as HTMLInputElement).checked).toBe(true)
    next()
    expect(current()).toBe('Who reads it')
    const nextBtn = within(sheet).getByRole('button', { name: /^next$/i }) as HTMLButtonElement
    // MUTATION: let step 3 pass without a valid name -> fails.
    expect(nextBtn.disabled).toBe(true)
    fireEvent.change(within(sheet).getByLabelText(/^name$/i), { target: { value: 'search-api' } })
    fireEvent.change(within(sheet).getByLabelText(/^provider$/i), { target: { value: 'brave' } })
    fireEvent.click(within(sheet).getByRole('checkbox', { name: /profile:claude-code/ }))
    next()
    expect(current()).toBe('One-time form')
    // No value is asked for in the wizard: only the one-time form takes one.
    expect(sheet.querySelector('input[type="password"]')).toBeNull()
    fireEvent.click(within(sheet).getByRole('button', { name: /open the one-time form/i }))

    // MUTATION: open the form at another scope or repository -> fails.
    expect(window.location.search).toBe(`?scope=repository&repo=${REPO_A}&action=add`)
    rerender(<SecretsScreen view={window.location.search.slice(1)} />)
    const form = await screen.findByRole('form', { name: /add a secret/i })
    // The wizard's choices are fixed in the form; it asks for the value alone.
    expect(within(form).queryByLabelText(/^name/i)).toBeNull()
    expect(visible(form)).toMatch(/search-api/)
    fireEvent.change(within(form).getByLabelText(/^value/i), { target: { value } })
    await act(async () => {
      fireEvent.click(within(form).getByRole('button', { name: /set secret/i }))
    })
    await screen.findByText(/search-api was written/i)
    const posted = calls.filter((c) => c.method === 'POST' && c.url.endsWith('/secrets'))
    expect(posted).toHaveLength(1)
    expect(posted[0]!.url).toBe(`/v1/repositories/${REPO_A}/secrets`)
    expect(posted[0]!.body).toEqual({ name: 'search-api', provider: 'brave', kind: 'api_key', value, link_token: minted() })
    // Step 3's tick, written after the value, one grant.
    // MUTATION: drop the grants the wizard collected -> fails.
    expect(calls.filter((c) => c.method === 'PUT').map((c) => decodeURIComponent(c.url))).toEqual([`/v1/repositories/${REPO_A}/secrets/search-api/grants/profile:claude-code`])
    expect(leaks(value)).toEqual([])
  })
})

describe('a secret cannot be submitted from the wrong scope', () => {
  const groupYours = () => ({ tenant_id: 'eng', scope: 'personal', personal_tenant_id: 'u-ana', can_manage: true, rows: [storeRow('mine', { scope: 'personal', opted_in: true })] })

  it('Yours from a group workspace: the wizard will not open a form, and the form view refuses', async () => {
    const calls = api({ overview: groupYours })
    const { SecretsScreen } = await load()
    const { rerender } = render(<SecretsScreen view="scope=personal" />)
    fireEvent.click(await screen.findByRole('button', { name: /^add a secret$/i }))
    const sheet = screen.getByRole('dialog', { name: /add a secret/i })
    expect((within(sheet).getByRole('radio', { name: /^yours/i }) as HTMLInputElement).checked).toBe(true)
    fireEvent.click(within(sheet).getByRole('button', { name: /^next$/i }))
    fireEvent.click(within(sheet).getByRole('button', { name: /^next$/i }))
    fireEvent.change(within(sheet).getByLabelText(/^name$/i), { target: { value: 'mine-too' } })
    fireEvent.click(within(sheet).getByRole('button', { name: /^next$/i }))
    await within(sheet).findByText(/yours is set in your personal workspace/i)
    // MUTATION: drop the personal-workspace check -> the form opens on the group's store and this fails.
    expect((within(sheet).getByRole('button', { name: /open the one-time form/i }) as HTMLButtonElement).disabled).toBe(true)

    // And an address that asks for the form anyway.
    rerender(<SecretsScreen view="scope=personal&action=add" />)
    await screen.findAllByText(/yours is set in your personal workspace/i)
    expect(screen.queryByRole('form', { name: /add a secret/i })).toBeNull()
    // MUTATION: mint or write on the selected (group) workspace's store -> fails.
    expect(calls.filter((c) => c.method !== 'GET' && c.url.startsWith('/v1/workspace/secrets'))).toEqual([])
  })

  it('a repository\'s form writes to that repository\'s route, never the workspace\'s', async () => {
    const calls = api({
      repos: [registration(REPO_A, 'example-api')],
      write: (m, u) => (m === 'POST' && u.endsWith('/secrets') ? { status: 201, body: { secret: record('repo-only') } } : null),
    })
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={`scope=repository&repo=${REPO_A}&action=add`} />)
    const form = await screen.findByRole('form', { name: /add a secret/i })
    fireEvent.change(within(form).getByLabelText(/^name/i), { target: { value: 'repo-only' } })
    fireEvent.change(within(form).getByLabelText(/^value/i), { target: { value: fakeValue() } })
    await act(async () => {
      fireEvent.click(within(form).getByRole('button', { name: /^add/i }))
    })
    await screen.findByText(/repo-only was written/i)
    const sent = calls.filter((c) => c.method !== 'GET').map((c) => c.url)
    // MUTATION: write a repository form through the workspace store -> fails.
    expect(sent).toEqual([`/v1/repositories/${REPO_A}/secrets/links`, `/v1/repositories/${REPO_A}/secrets`])
  })
})
