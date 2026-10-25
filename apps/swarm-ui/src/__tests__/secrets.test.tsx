// CONFIG › SECRETS (lane SSC-UI, part of #2158; docs/design/secrets-scopes.md
// §6.1, drawn as Config proposal E's S4). What the page must keep, each held
// here against the API's shapes (`GET /v1/secrets/overview`,
// `GET /v1/repositories`, the store's routes and their one-time links):
//
//   * the old addresses redirect with their link tokens, and the token goes
//     back once, with the write it was minted for;
//   * no value is rendered after save: it is in one request's body and in no
//     element, attribute, field, URL or storage;
//   * the Platform secrets tab is absent for a non-admin;
//   * the repository scope lists only registered repositories;
//   * a shadowed row shows its winner;
//   * Add a secret's steps run in proposal E's order (scope, destination,
//     consumer or grant, one-time form), and a secret cannot be submitted
//     from the wrong scope.
//
// Fixtures only; no network. Every value and token is built at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null

/** A credential-shaped value, built at runtime: it must reach one POST body and nothing else. */
const fakeValue = () => ['sec', 'demo'].join('-') + 'Kx4'.repeat(12)
/** One-time link tokens, built at runtime. */
const linkToken = (n = 0) => ['lnk', 'tok'].join('') + `Pq${n}`.repeat(10)

const AT = '2026-10-20T09:30:00+00:00'
const REPO_A = 'repo_0a1b2c3d4e5f6071'
const REPO_B = 'repo_1b2c3d4e5f607182'
const NOT_REGISTERED = 'repo_ffffffffffffffff'

function registration(id: string, repo: string): Json {
  return { repo_id: id, tenant_id: 'eng', forge: 'github', owner: 'example-org', repo, repository_url: `https://github.com/example-org/${repo}`, default_branch: 'main', allowed_profiles: ['claude-code'], created_by: 'alice@example.com', created_at: AT, index: {} }
}

const REPOS: Json = { repositories: [registration(REPO_A, 'example-api'), registration(REPO_B, 'example-web')] }

function storeRow(name: string, over: Json = {}): Json {
  return {
    section: 'agents', store: 'workspace_secrets', name, scope: 'workspace', repo_id: null, repository: null,
    provider: 'brave', kind: 'api_key', status: 'set', set_at: AT, set_by: 'alice@example.com',
    consumers: ['profile:claude-code'], shadowed_by: [], shadows: [], can_manage: true, writer: '/v1/workspace/secrets', ...over,
  }
}

function overview(scope: string, rows: Json[], over: Json = {}): Json {
  return { tenant_id: 'eng', scope, scopes: ['workspace', 'personal', 'repository'], repo_id: null, repository: null, personal_tenant_id: null, can_manage: true, rows, not_listed: [], note: 'status only', ...over }
}

function record(name: string, over: Json = {}): Json {
  return {
    name, secret_id: `swarm-tenant-eng-wsec-${name.length}a1b2`, provider: 'brave', kind: 'api_key', scope: 'workspace',
    current_version: '1', created_at: AT, created_by: 'alice@example.com', rotated_at: null, rotated_by: null,
    grants: [{ consumer: 'profile:claude-code', slot: 'files', enabled: true }], ...over,
  }
}

const list = (secrets: Json[], over: Json = {}): Json => ({ tenant_id: 'eng', can_manage: true, secrets, note: 'n', ...over })

const CONSUMERS: Json = {
  kinds: ['api_key', 'bearer_token', 'basic_auth', 'oauth_client', 'opaque'],
  providers: ['brave', 'other'],
  consumers: [
    { consumer: 'profile:claude-code', kind: 'profile', name: 'claude-code', slots: [{ slot: 'files', kinds: ['api_key', 'opaque'] }], grantable: true, note: '' },
    { consumer: 'tool:jev-browser', kind: 'tool', name: 'jev-browser', slots: [{ slot: 'typesafe', kinds: ['api_key'] }], grantable: true, note: '' },
  ],
}

/** The page's reads, and every write handed to `write`. */
function api(opts: { overviews?: Record<string, Json>; lists?: Record<string, Json>; write?: (m: string, url: string, body: unknown) => Reply } = {}) {
  const overviews = opts.overviews ?? { workspace: overview('workspace', [storeRow('search-api')]) }
  const lists = opts.lists ?? { workspace: list([record('search-api')]) }
  return serve((m, url, body) => {
    if (m === 'GET' && url.startsWith('/v1/secrets/overview')) {
      const q = new URLSearchParams(url.slice(url.indexOf('?') + 1))
      const key = q.get('scope') === 'repository' ? `repository:${q.get('repo_id')}` : q.get('scope') ?? ''
      const o = overviews[key]
      return o === undefined ? { status: 404, body: { detail: 'Not Found' } } : { status: 200, body: o }
    }
    if (m === 'GET' && url === '/v1/repositories') return { status: 200, body: REPOS }
    if (m === 'GET' && url.startsWith('/v1/workspace/secrets/consumers')) return { status: 200, body: CONSUMERS }
    if (m === 'GET' && url === '/v1/workspace/secrets') return lists.workspace ? { status: 200, body: lists.workspace } : null
    const repoList = /^\/v1\/repositories\/(repo_[0-9a-f]+)\/secrets$/.exec(url)
    if (m === 'GET' && repoList !== null) {
      const l = lists[`repository:${repoList[1]}`]
      return l === undefined ? null : { status: 200, body: l }
    }
    if (m === 'GET' && url.startsWith('/v1/provider-keys/')) {
      return { status: 200, body: { provider: 'typesafe', user: { available: true, set: false }, tenant: { tenant_id: 'eng', set: false }, resolution: { tenant_id: 'eng', uses: 'none' }, consumed_by: [] } }
    }
    if (m === 'GET' && url.startsWith('/v1/tenants/me')) {
      return { status: 200, body: { tenant: { tenant_id: 'eng', kind: 'group' }, principal: { email: 'alice@example.com', is_admin: false }, role: { name: 'admin' } } }
    }
    if (m !== 'GET' && opts.write) return opts.write(m, url, body)
    return null
  })
}

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../Secrets')
}

/**
 * The page as the router drives it: `go` re-renders it with the view its
 * address names, as App does. `tokens` is App's capture of the bar.
 */
async function page(view: string, tokens: { link: string | null; key: string | null } | null = null) {
  const { SecretsScreen } = await load()
  const visits: string[] = []
  let r: ReturnType<typeof render> | null = null
  const go = (to: string) => {
    visits.push(to)
    const q = to.indexOf('?')
    r!.rerender(<SecretsScreen view={q === -1 ? null : to.slice(q + 1)} tokens={null} go={go} />)
  }
  r = render(<SecretsScreen view={view} tokens={tokens} go={go} />)
  return { visits, go }
}

function leaks(needle: string): string[] {
  const where: string[] = []
  if (document.documentElement.outerHTML.includes(needle)) where.push('the document')
  if (visible(document.body).includes(needle)) where.push('the text')
  if (window.location.href.includes(needle)) where.push('the URL')
  for (const store of [window.localStorage, window.sessionStorage]) {
    for (let i = 0; i < store.length; i++) {
      const k = store.key(i) ?? ''
      if (k.includes(needle) || (store.getItem(k) ?? '').includes(needle)) where.push('storage')
    }
  }
  for (const el of Array.from(document.querySelectorAll('*'))) {
    for (const a of Array.from(el.attributes)) if (a.value.includes(needle)) where.push(`an attribute ${a.name}`)
    if (el instanceof HTMLInputElement && el.value.includes(needle)) where.push('an input')
  }
  return where
}

const sheet = () => screen.getByRole('dialog', { name: /add a secret/i })
const next = () => fireEvent.click(within(sheet()).getByRole('button', { name: /^next$/i }))
const stepOn = () => visible(sheet().querySelector('[aria-current="step"]'))

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
  window.localStorage.clear()
  window.sessionStorage.clear()
})

describe('the scope picker', () => {
  it('draws Workspace, Yours and Repository, and each moves the address', async () => {
    api()
    const { visits } = await page('scope=workspace')
    const picker = await screen.findByRole('group', { name: /^scope$/i })
    // MUTATION: render today's store page (WorkspaceSecretsScreen) at /config/secrets -> no picker and this fails.
    expect(within(picker).getAllByRole('button').map((b) => b.textContent)).toEqual(['Workspace', 'Yours', 'Repository'])
    fireEvent.click(within(picker).getByRole('button', { name: 'Yours' }))
    expect(visits.at(-1)).toBe('config/secrets?scope=personal')
  })

  it('draws the workspace scope’s sections in §6.1’s order: For agents, Keys for agents and MCP, GitHub', async () => {
    api({ overviews: { workspace: overview('workspace', [storeRow('search-api'), { section: 'keys', store: 'provider_keys', name: 'typesafe', status: 'unset', can_manage: true, consumers: [] }]) } })
    await page('scope=workspace')
    await screen.findByRole('table', { name: /workspace secrets/i })
    const heads = Array.from(document.querySelectorAll('.c-card-h h2')).map((h) => h.textContent)
    const order = ['For agents', 'Keys for agents and MCP', 'GitHub token'].map((t) => heads.indexOf(t))
    // MUTATION: drop the Keys section (Settings › API keys not moved) -> -1 and this fails.
    expect(order.every((i) => i >= 0)).toBe(true)
    expect([...order].sort((a, b) => a - b)).toEqual(order)
  })
})

describe('the repository scope lists only registered repositories', () => {
  it('offers the registrations GET /v1/repositories serves, and reads the chosen one', async () => {
    const calls = api({
      overviews: { [`repository:${REPO_A}`]: overview('repository', [], { repo_id: REPO_A, repository: 'example-org/example-api' }) },
      lists: { [`repository:${REPO_A}`]: list([], { repository: 'example-org/example-api' }) },
    })
    await page(`scope=repository&repo=${REPO_A}`)
    const select = (await screen.findByLabelText(/^repository$/i)) as HTMLSelectElement
    // MUTATION: list a repository from anywhere else (an access grant, a chosen one) -> fails.
    expect(Array.from(select.options).map((o) => o.textContent)).toEqual(['example-org/example-api', 'example-org/example-web'])
    expect((await screen.findAllByText(/released only to agents that clone example-org\/example-api/i)).length).toBeGreaterThan(0)
    expect(calls.some((c) => c.url === `/v1/secrets/overview?scope=repository&repo_id=${REPO_A}`)).toBe(true)
  })

  it('says a repository is not registered, and reads nothing for it', async () => {
    const calls = api({ overviews: {} })
    await page(`scope=repository&repo=${NOT_REGISTERED}`)
    // MUTATION: read the overview for whatever `repo` the address names -> the read below happens and this fails.
    await screen.findByText(/not registered in this workspace/i)
    expect(calls.some((c) => c.url.includes(NOT_REGISTERED))).toBe(false)
  })
})

describe('a shadowed row shows its winner', () => {
  it('names the winning secret and on whose agents, and what a winner hides', async () => {
    api({
      overviews: {
        workspace: overview('workspace', [
          storeRow('search-api', { shadowed_by: [{ scope: 'personal', person: 'dana@example.com', where: 'agents of dana@example.com', slot: 'files' }] }),
          storeRow('docs-bearer', { shadows: [{ scope: 'workspace', where: 'agents of this workspace', slot: 'files' }] }),
        ]),
      },
      lists: { workspace: list([record('search-api'), record('docs-bearer')]) },
    })
    await page('scope=workspace')
    const table = await screen.findByRole('table', { name: /workspace secrets/i })
    await waitFor(() => expect(visible(within(table).getByRole('row', { name: /search-api/ }))).toMatch(/dana@example\.com’s own wins on their agents/))
    // MUTATION: draw the row without the overview's `shadowed_by` -> fails.
    expect(visible(within(table).getByRole('row', { name: /search-api/ }))).toMatch(/dana@example\.com’s own wins/)
    expect(visible(within(table).getByRole('row', { name: /docs-bearer/ }))).toMatch(/overrides the workspace’s/)
  })
})

describe('Add a secret', () => {
  it('runs its steps in proposal E’s order: scope, destination, who reads it, one-time form', async () => {
    api()
    await page('scope=workspace')
    await screen.findByRole('table', { name: /workspace secrets/i })
    fireEvent.click(screen.getByRole('button', { name: /^add a secret$/i }))
    const steps = Array.from(sheet().querySelectorAll('.sec-steps li')).map((li) => visible(li).replace(/^[\d✓]+/, ''))
    // MUTATION: reorder STEPS -> fails.
    expect(steps).toEqual(['Scope', 'Destination', 'Who reads it', 'One-time form'])
    expect(stepOn()).toMatch(/Scope/)
    expect(within(sheet()).getByRole('radio', { name: /^workspace/i })).toBeTruthy()
    next()
    expect(stepOn()).toMatch(/Destination/)
    expect(within(sheet()).getByRole('radio', { name: /^agents/i })).toBeTruthy()
    next()
    expect(stepOn()).toMatch(/Who reads it/)
    // The name decides whether the third step may move on.
    expect((within(sheet()).getByRole('button', { name: /^next$/i }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(within(sheet()).getByLabelText(/^name$/i), { target: { value: 'search-api' } })
    await within(sheet()).findByRole('checkbox', { name: /profile:claude-code/ })
    next()
    expect(stepOn()).toMatch(/One-time form/)
    // The form is not open until its link is minted: no value field yet.
    expect(within(sheet()).queryByLabelText(/^value$/i)).toBeNull()
    expect(within(sheet()).getByRole('button', { name: /open the one-time form/i })).toBeTruthy()
  })

  it('mints a link, writes the value once with it, grants what was ticked, and renders no value after save', async () => {
    const value = fakeValue()
    const t = linkToken(1)
    const calls = api({
      write: (m, url) => {
        if (m === 'POST' && url === '/v1/workspace/secrets/links') return { status: 201, body: { url: null, path: `/settings/workspace/secrets?action=add&t=${t}`, action: 'add', name: null, expires_at: AT } }
        if (m === 'POST' && url === '/v1/workspace/secrets') return { status: 201, body: { secret: record('search-key'), via_link: true } }
        if (m === 'PUT' && url.startsWith('/v1/workspace/secrets/search-key/grants/')) return { status: 200, body: { secret: record('search-key') } }
        return null
      },
    })
    await page('scope=workspace')
    await screen.findByRole('table', { name: /workspace secrets/i })
    fireEvent.click(screen.getByRole('button', { name: /^add a secret$/i }))
    next()
    next()
    fireEvent.change(within(sheet()).getByLabelText(/^name$/i), { target: { value: 'search-key' } })
    fireEvent.click(await within(sheet()).findByRole('checkbox', { name: /profile:claude-code/ }))
    next()
    await act(async () => {
      fireEvent.click(within(sheet()).getByRole('button', { name: /open the one-time form/i }))
    })
    const field = (await within(sheet()).findByLabelText(/^value$/i)) as HTMLInputElement
    // Never prefilled, and masked.
    expect(field.value).toBe('')
    expect(field.type).toBe('password')
    fireEvent.change(field, { target: { value } })
    await act(async () => {
      fireEvent.click(within(sheet()).getByRole('button', { name: /^set secret$/i }))
    })
    await within(sheet()).findByText(/search-key was added/i)

    const post = calls.find((c) => c.method === 'POST' && c.url === '/v1/workspace/secrets')
    expect(post?.body).toMatchObject({ name: 'search-key', kind: 'api_key', value, link_token: t })
    // MUTATION: send the value anywhere else too (the grant, the URL) -> fails.
    const carrying = calls.filter((c) => c.url.includes(value) || JSON.stringify(c.body ?? null).includes(value))
    expect(carrying).toHaveLength(1)
    expect(calls.some((c) => c.method === 'PUT' && c.url === '/v1/workspace/secrets/search-key/grants/profile%3Aclaude-code')).toBe(true)
    // MUTATION: echo the value in the confirmation, or keep it in the field -> fails.
    expect(leaks(value)).toEqual([])
    // The token is in no element or the address either.
    expect(leaks(t)).toEqual([])
  })

  it('cannot submit from the wrong scope: going back and changing scope drops the link minted for the first', async () => {
    const value = fakeValue()
    const repoToken = linkToken(2)
    const wsToken = linkToken(3)
    const calls = api({
      overviews: {
        workspace: overview('workspace', [storeRow('search-api')]),
      },
      write: (m, url) => {
        if (m === 'POST' && url === `/v1/repositories/${REPO_A}/secrets/links`) return { status: 201, body: { path: `/config/secrets?scope=repository&repo=${REPO_A}&action=add&t=${repoToken}`, expires_at: AT } }
        if (m === 'POST' && url === '/v1/workspace/secrets/links') return { status: 201, body: { path: `/settings/workspace/secrets?action=add&t=${wsToken}`, expires_at: AT } }
        if (m === 'POST' && url === '/v1/workspace/secrets') return { status: 201, body: { secret: record('search-key') } }
        return null
      },
    })
    await page('scope=workspace')
    await screen.findByRole('table', { name: /workspace secrets/i })
    fireEvent.click(screen.getByRole('button', { name: /^add a secret$/i }))
    // Repository A first, as far as its one-time form.
    fireEvent.click(within(sheet()).getByRole('radio', { name: /^repository/i }))
    await waitFor(() => expect((within(sheet()).getByLabelText(/^repository$/i) as HTMLSelectElement).value).toBe(REPO_A))
    next()
    next()
    fireEvent.change(within(sheet()).getByLabelText(/^name$/i), { target: { value: 'search-key' } })
    next()
    await act(async () => {
      fireEvent.click(within(sheet()).getByRole('button', { name: /open the one-time form/i }))
    })
    await within(sheet()).findByLabelText(/^value$/i)
    // Back to step 1, and the workspace instead.
    fireEvent.click(within(sheet()).getByRole('button', { name: /^back$/i }))
    fireEvent.click(within(sheet()).getByRole('button', { name: /^back$/i }))
    fireEvent.click(within(sheet()).getByRole('button', { name: /^back$/i }))
    fireEvent.click(within(sheet()).getByRole('radio', { name: /^workspace/i }))
    next()
    next()
    next()
    // MUTATION: keep the repository's form open across a scope change -> a value field is here at once and this fails.
    expect(within(sheet()).queryByLabelText(/^value$/i)).toBeNull()
    await act(async () => {
      fireEvent.click(within(sheet()).getByRole('button', { name: /open the one-time form/i }))
    })
    fireEvent.change(await within(sheet()).findByLabelText(/^value$/i), { target: { value } })
    await act(async () => {
      fireEvent.click(within(sheet()).getByRole('button', { name: /^set secret$/i }))
    })
    await within(sheet()).findByText(/search-key was added/i)
    const writes = calls.filter((c) => c.method === 'POST' && /\/secrets$/.test(c.url))
    // Written once, into the workspace, with the workspace's link and never the repository's.
    expect(writes.map((c) => c.url)).toEqual(['/v1/workspace/secrets'])
    expect(writes[0]!.body).toMatchObject({ link_token: wsToken })
    expect(JSON.stringify(calls.map((c) => c.body))).not.toContain(`"link_token":"${repoToken}"`)
  })

  it('a link opened for one repository is not offered once the page moves to another scope', async () => {
    const t = linkToken(4)
    api({
      overviews: {
        [`repository:${REPO_A}`]: overview('repository', [], { repo_id: REPO_A, repository: 'example-org/example-api' }),
        workspace: overview('workspace', [storeRow('search-api')]),
      },
      lists: { [`repository:${REPO_A}`]: list([], { repository: 'example-org/example-api' }), workspace: list([record('search-api')]) },
    })
    const { go } = await page(`scope=repository&repo=${REPO_A}&action=add`, { link: t, key: null })
    // The link's own form, in its own scope.
    await screen.findByRole('form', { name: /add a secret/i })
    expect(window.location.href).not.toContain(t)
    act(() => go('config/secrets?scope=workspace&action=add'))
    await screen.findByRole('table', { name: /workspace secrets/i })
    // MUTATION: offer the held token to whatever form the view names -> an add form opens here and this fails.
    expect(screen.queryByRole('form', { name: /add a secret/i })).toBeNull()
  })
})

describe('every Set opens a one-time form', () => {
  it('Rotate mints the store link for that name first, and the rotate carries its token', async () => {
    const value = fakeValue()
    const t = linkToken(7)
    const calls = api({
      write: (m, url, body) => {
        if (m === 'POST' && url === '/v1/workspace/secrets/links') {
          expect(body).toEqual({ action: 'rotate', name: 'search-api' })
          return { status: 201, body: { path: `/settings/workspace/secrets?action=rotate&name=search-api&t=${t}`, expires_at: AT } }
        }
        if (m === 'PUT' && url === '/v1/workspace/secrets/search-api/value') return { status: 200, body: { secret: record('search-api', { current_version: '2' }) } }
        return null
      },
    })
    await page('scope=workspace')
    const table = await screen.findByRole('table', { name: /workspace secrets/i })
    // MUTATION: open Rotate's form inline without a link (today's Settings page) -> no mint and this fails.
    expect(screen.queryByRole('form', { name: /rotate search-api/i })).toBeNull()
    await act(async () => {
      fireEvent.click(within(table).getByRole('button', { name: /rotate search-api/i }))
    })
    const form = await screen.findByRole('form', { name: /rotate search-api/i })
    expect(calls.filter((c) => c.method === 'POST' && c.url === '/v1/workspace/secrets/links')).toHaveLength(1)
    const field = within(form).getByLabelText(/^new value$/i) as HTMLInputElement
    expect(field.value).toBe('')
    fireEvent.change(field, { target: { value } })
    await act(async () => {
      fireEvent.click(within(form).getByRole('button', { name: /^rotate$/i }))
    })
    await screen.findByText(/search-api was rotated/i)
    const put = calls.find((c) => c.method === 'PUT')!
    expect(put.body).toEqual({ value, link_token: t })
    expect(leaks(value)).toEqual([])
    expect(leaks(t)).toEqual([])
  })
})

describe('the old addresses redirect with their link tokens', () => {
  it('opens the runner-key link’s form and sends its token once, with that key', async () => {
    const value = fakeValue()
    const t = linkToken(5)
    const calls = api({
      overviews: { workspace: overview('workspace', [{ section: 'keys', store: 'runner_provider_keys', name: 'anthropic', status: 'set', can_manage: true, consumers: ['profile:claude-code'] }]) },
      write: (m, url) => (m === 'POST' && url === '/v1/tenants/me/credentials' ? { status: 201, body: { provider: 'anthropic', written: true } } : null),
    })
    window.history.replaceState(null, '', `/config/secrets?scope=workspace&runner_key=anthropic&t=${t}`)
    await page('scope=workspace&runner_key=anthropic')
    const form = await screen.findByRole('form', { name: /set the anthropic key/i })
    // The token is out of the bar before anything paints (a layout effect).
    expect(window.location.search).toBe('?scope=workspace&runner_key=anthropic')
    const field = within(form).getByLabelText(/^value$/i) as HTMLInputElement
    expect(field.value).toBe('')
    fireEvent.change(field, { target: { value } })
    await act(async () => {
      fireEvent.click(within(form).getByRole('button', { name: /^set key$/i }))
    })
    await screen.findByText(/the anthropic key was set/i)
    const posts = calls.filter((c) => c.method === 'POST' && c.url === '/v1/tenants/me/credentials')
    // MUTATION: drop the link token from the write -> fails.
    expect(posts).toHaveLength(1)
    expect(posts[0]!.body).toEqual({ provider: 'anthropic', api_key: value, link_token: t })
    expect(leaks(value)).toEqual([])
    expect(leaks(t)).toEqual([])
  })

  it('opens the TypeSafe link’s form from /settings/keys/typesafe, and its token goes with that save', async () => {
    const value = fakeValue()
    const t = linkToken(6)
    const calls = api({
      overviews: { personal: overview('personal', [{ section: 'keys', store: 'provider_keys', name: 'typesafe', status: 'unset', can_manage: true, consumers: [] }], { personal_tenant_id: 'u-alice' }) },
      write: (m, url) => (m === 'PUT' && url === '/v1/provider-keys/typesafe/user' ? { status: 200, body: { set: true } } : null),
    })
    // App read the token off the old path before rewriting the bar.
    await page('scope=personal&key=typesafe', { link: null, key: t })
    const form = await screen.findByRole('form', { name: /save your key/i })
    fireEvent.change(within(form).getByLabelText(/^typesafe key$/i), { target: { value } })
    await act(async () => {
      fireEvent.click(within(form).getByRole('button', { name: /^save$/i }))
    })
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    const put = calls.find((c) => c.method === 'PUT')!
    // MUTATION: open the key form without the captured token -> fails.
    expect(put.body).toEqual({ api_key: value, link_token: t })
    expect(leaks(value)).toEqual([])
  })
})

describe('the Platform secrets tab is absent for a non-admin', () => {
  it('is in a platform admin’s Admin panel and in nobody else’s', async () => {
    vi.resetModules()
    const { adminPagesFor } = await import('../Spine')
    const keys = (pages: readonly { key: string }[]) => pages.map((p) => p.key)
    // MUTATION: leave the row out of PANEL_PAGES.admin -> a platform admin cannot reach it and this fails.
    expect(keys(adminPagesFor(true, false))).toContain('platform-secrets')
    // MUTATION: hand a workspace admin every Admin page -> fails.
    expect(keys(adminPagesFor(false, true))).not.toContain('platform-secrets')
    expect(keys(adminPagesFor(false, false))).toEqual([])
  })

  it('sends a viewer from /admin/platform-secrets to Overview, and reads nothing of it', async () => {
    const ME = { tenant: { tenant_id: 'eng', kind: 'group', display_name: 'eng@example.com' }, principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: false }, environment: 'dev', environment_declared: true }
    const VIEWER = [{ tenant_id: 'eng', display_name: 'eng@example.com', kind: 'group', via: 'member', role: { name: 'viewer', source: 'override' } }]
    const calls = serve((_m, url) => (url.includes('/v1/tenants/mine') ? { status: 200, body: VIEWER } : url.includes('/v1/tenants/me') ? { status: 200, body: ME } : null))
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const mod = await import('../App')
    window.history.replaceState(null, '', '/admin/platform-secrets')
    render(<mod.App />)
    // MUTATION: route the tab without the admin check -> the viewer stays and this fails.
    await waitFor(() => expect(window.location.pathname).toBe('/overview'))
    expect(document.querySelector('.sk-spine a[data-sec="admin"]')).toBeNull()
    expect(calls.some((c) => c.url.startsWith('/v1/admin/platform-secrets') && c.method !== 'GET')).toBe(false)
  })
})
