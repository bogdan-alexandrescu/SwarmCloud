// CONFIG › SECRETS › REPOSITORY › GITHUB ACTIONS (lane SSC-UI-ACTIONS, part of
// #2158; docs/design/secrets-scopes.md §3 and §6.1, as BGD's answers in §11
// changed them; mock-up config-e.html S4b2, S4f and S4c's Actions branch).
//
// The §9 row's tests, adjusted by §11:
//
//   * Q5: the value is sealed in swarm-api, so it travels ONLY as `value` in
//     the one-time form's PUT, with that form's `link_token`, and the field
//     is empty before the request leaves. No `encrypted_value`, no key read.
//   * Q4: writing into an environment whose rule is not main-only needs the
//     environment's name typed, and the PUT carries it as
//     `confirm_environment`; a main-only one does not ask.
//   * the App's missing Secrets permission is said, with the review link
//     SSC-ACTIONS-API answers.
//   * Q4: no control creates an environment. The only ways on are the GitHub
//     link and Check rule, which is a GET.
//
// Fixtures only; no network. Every value and token is built at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

/** A credential-shaped value, built at runtime: it must reach the one PUT body and nothing else. */
const fakeValue = () => ['act', 'demo'].join('-') + 'Zq4'.repeat(12)
/** A one-time link token, built at runtime. */
const linkToken = () => ['lnk', 'act'].join('') + 'Wd7'.repeat(10)

const REPO_ID = 'rep_ex1'
const REPOSITORY = 'example-org/example-api'
const BASE = `/v1/repositories/${REPO_ID}/actions`
const AT = '2026-10-20T09:30:00+00:00'
const SETTINGS = `https://github.com/${REPOSITORY}/settings/environments`
const REVIEW = 'https://github.com/organizations/example-org/settings/installations/42/permissions/update'

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null

function rule(environment: string, kind: string | null): Json {
  return {
    environment, exists: kind !== null, rule: kind, main_only: kind === 'main_only', branch: 'main',
    branches: kind === 'main_only' ? [{ name: 'main', type: 'branch' }] : [],
    create_url: kind === null ? `${SETTINGS}/new` : null, settings_url: SETTINGS,
  }
}

function listBody(): Json {
  return {
    tenant_id: 'eng',
    repo_id: REPO_ID,
    // SSC-ACTIONS-API-2: `repository` is 'owner/repo' and the list is `repository_secrets`.
    repository: REPOSITORY,
    repository_secrets: [
      { name: 'TEST_DB_PASSWORD', created_at: AT, updated_at: AT, set_by_swarmcloud: true, set_by: 'operator@example.com', set_at: AT },
      { name: 'SENTRY_DSN', created_at: AT, updated_at: AT, set_by_swarmcloud: false, set_by: null, set_at: null },
    ],
    environments: [
      { environment: 'repo-secrets', rule: rule('repo-secrets', 'main_only'),
        secrets: [{ name: 'MERGE_APP_PRIVATE_KEY', created_at: AT, updated_at: AT, set_by_swarmcloud: true, set_by: 'operator@example.com', set_at: AT }] },
      { environment: 'staging', rule: rule('staging', 'any_branch'), secrets: [] },
    ],
    environments_truncated: false,
    create_url: `${SETTINGS}/new`,
    can_write: true,
    cannot_write_because: null,
    checked_at: new Date(Date.now() - 60_000).toISOString(),
  }
}

function api(opts: { list?: () => Reply; write?: (m: string, url: string, body: unknown) => Reply; token?: string } = {}) {
  const token = opts.token ?? linkToken()
  const calls = serve((m, url, body) => {
    if (m === 'GET' && url.startsWith(`${BASE}/secrets`)) return opts.list ? opts.list() : { status: 200, body: listBody() }
    const ruled = m === 'GET' ? /\/actions\/environments\/([^/]+)\/rule/.exec(url) : null
    if (ruled !== null) {
      const env = decodeURIComponent(ruled[1]!)
      return { status: 200, body: { repo_id: REPO_ID, ...rule(env, env === 'repo-secrets' ? 'main_only' : env === 'staging' ? 'any_branch' : null) } }
    }
    if (m === 'POST' && url.startsWith(`${BASE}/links`)) {
      const b = body as Json
      const path = `/config/secrets?scope=repository&repo=${REPO_ID}&dest=actions&name=${String(b.name)}&t=${token}`
      return { status: 201, body: { url: `https://console.example.com${path}`, path, name: b.name, environment: b.environment, expires_at: AT } }
    }
    if (opts.write) return opts.write(m, url, body)
    if (m === 'PUT') return { status: 200, body: { secret: { name: 'X' }, created: true, via_link: true, rule: null, note: 'kept only the name' } }
    if (m === 'DELETE') return { status: 200, body: { secret: { name: 'X' }, deleted: true } }
    return null
  })
  return { calls, token }
}

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../SecretsActions')
}

async function mount(props: Json = {}) {
  const { SecretsActions } = await load()
  render(<SecretsActions repoId={REPO_ID} repository={REPOSITORY} {...props} />)
  await screen.findByRole('table', { name: /repository secrets/i })
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
  for (const el of Array.from(document.querySelectorAll('*'))) {
    for (const a of Array.from(el.attributes)) if (a.value.includes(needle)) where.push(`an attribute ${a.name}`)
    if (el instanceof HTMLInputElement && el.value.includes(needle)) where.push('an input')
  }
  return where
}

const dialog = () => screen.getByRole('dialog')
const next = () => fireEvent.click(within(dialog()).getByRole('button', { name: /^next$/i }))

/** Through the sheet to its one-time form: destination, name, then the link. */
async function toForm(destination: RegExp, name: string) {
  fireEvent.click(screen.getByRole('button', { name: /^set a secret$/i }))
  fireEvent.click(within(dialog()).getByRole('radio', { name: destination }))
  next()
  fireEvent.change(within(dialog()).getByLabelText(/^name$/i), { target: { value: name } })
  next()
  fireEvent.click(within(dialog()).getByRole('button', { name: /open the one-time form/i }))
  return within(dialog()).findByLabelText(/^value$/i) as Promise<HTMLInputElement>
}

afterEach(() => {
  window.history.replaceState(null, '', '/')
  window.localStorage.clear()
  window.sessionStorage.clear()
})

describe('the GitHub Actions destination lists what GitHub serves', () => {
  it('draws the repository secrets and each environment with its rule and its secrets', async () => {
    api()
    await mount()
    const repo = screen.getByRole('table', { name: /repository secrets/i })
    expect(visible(repo)).toContain('TEST_DB_PASSWORD')
    expect(visible(repo)).toContain('SwarmCloud')
    expect(visible(repo)).toContain('on GitHub')
    const envs = screen.getByRole('region', { name: /environments/i })
    expect(visible(envs)).toContain('repo-secrets')
    expect(visible(envs)).toContain('main only')
    expect(visible(envs)).toContain('MERGE_APP_PRIVATE_KEY')
    expect(visible(envs)).toContain('staging')
    expect(visible(envs)).toContain('any branch')
  })
})

describe('SSC-ACTIONS-API-2: the list says who may write, and the pane follows it', () => {
  it('draws the mock-up’s sentence for a caller who may write, with when GitHub was asked', async () => {
    api()
    await mount()
    expect(visible(document.body)).toContain(
      `You may write here: workspace admin, and GitHub calls you admin of ${REPOSITORY} (checked 1m ago).`)
    expect(screen.getByRole('button', { name: /^set a secret$/i })).toBeTruthy()
    expect(screen.getAllByRole('button', { name: /^delete /i }).length).toBeGreaterThan(0)
  })

  it('hides Set and Delete from a caller who may not write, and says why', async () => {
    const because = 'GitHub does not call you an admin of this repository; ask a workspace admin, or a repository admin on GitHub'
    api({ list: () => ({ status: 200, body: { ...listBody(), can_write: false, cannot_write_because: because } }) })
    await mount({ canManage: true })
    expect(visible(document.body)).toContain(because)
    expect(visible(document.body)).not.toContain('You may write here')
    expect(screen.queryByRole('button', { name: /^set a secret/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^set /i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^delete /i })).toBeNull()
    // The typed-name step is a writer's; a reader is told who reads, not how to write.
    const envs = screen.getByRole('region', { name: /environments/i })
    expect(visible(envs)).not.toContain('Writing here asks you to type')
    expect(visible(envs)).toContain('A workflow on any branch can read it.')
    // The names are still read.
    expect(visible(screen.getByRole('table', { name: /repository secrets/i }))).toContain('TEST_DB_PASSWORD')
  })

  it('opens GitHub’s create page from the list’s create_url, on a repository with no environment yet', async () => {
    const created = `https://github.com/${REPOSITORY}/settings/environments/new`
    api({ list: () => ({ status: 200, body: { ...listBody(), environments: [], create_url: created } }) })
    await mount()
    const envs = screen.getByRole('region', { name: /environments/i })
    const link = within(envs).getByRole('link', { name: /create on github/i }) as HTMLAnchorElement
    expect(link.href).toBe(created)
  })

  it('reads the rows from repository_secrets and the name from repository, linking only GitHub', async () => {
    const { normActionsSecretList } = await import('../api')
    const listed = normActionsSecretList({ ...listBody(), create_url: 'https://evil.example.com/new' })
    expect(listed.repository).toBe(REPOSITORY)
    expect(listed.secrets.map((x) => x.name)).toEqual(['TEST_DB_PASSWORD', 'SENTRY_DSN'])
    expect(listed.create_url).toBeNull()
    expect(listed.can_write).toBe(true)
    // An answer that says nothing about writing is not known, never a yes.
    expect(normActionsSecretList({ repository_secrets: [] }).can_write).toBeNull()
  })
})

describe('§11 Q5: the value goes only to the one-time form’s route, sealed by swarm-api', () => {
  it('sends the value once, in the PUT with the form’s link token, and empties the field before the request leaves', async () => {
    const value = fakeValue()
    let fieldAtSend: string | null = null
    const { calls, token } = api({
      write: (m) => {
        if (m !== 'PUT') return null
        fieldAtSend = (document.querySelector('input[type="password"]') as HTMLInputElement | null)?.value ?? '(gone)'
        return { status: 200, body: { secret: { name: 'DEPLOY_KEY' }, created: true, via_link: true, rule: null, note: 'kept only the name' } }
      },
    })
    await mount()
    const field = await toForm(/repository level/i, 'DEPLOY_KEY')
    // The link was minted for exactly this destination and name.
    const mint = calls.find((c) => c.method === 'POST')!
    expect(mint.url).toBe(`${BASE}/links`)
    expect(mint.body).toEqual({ name: 'DEPLOY_KEY', environment: null })
    // The token is held, never drawn.
    expect(leaks(token)).toEqual([])

    fireEvent.change(field, { target: { value } })
    await act(async () => {
      fireEvent.click(within(dialog()).getByRole('button', { name: /^set secret$/i }))
    })
    await screen.findByText(/DEPLOY_KEY was set/i)

    const carrying = calls.filter((c) => JSON.stringify(c.body ?? null).includes(value))
    expect(carrying.map((c) => `${c.method} ${c.url}`)).toEqual([`PUT ${BASE}/secrets/DEPLOY_KEY`])
    expect(carrying[0]!.body).toEqual({ value, link_token: token })
    // Never sealed in the browser (§11 Q5): no ciphertext, no key read.
    expect(calls.some((c) => /public-key/.test(c.url))).toBe(false)
    expect(JSON.stringify(calls.map((c) => c.body))).not.toMatch(/encrypted_value|key_id/)
    expect(fieldAtSend).toBe('')
    expect(leaks(value)).toEqual([])
    expect(leaks(token)).toEqual([])
  })

  it('offers no value field until the page has minted its one-time link', async () => {
    const { calls } = api()
    await mount()
    fireEvent.click(screen.getByRole('button', { name: /^set a secret$/i }))
    fireEvent.click(within(dialog()).getByRole('radio', { name: /repository level/i }))
    next()
    fireEvent.change(within(dialog()).getByLabelText(/^name$/i), { target: { value: 'DEPLOY_KEY' } })
    next()
    // Before the link is minted there is no value field to type into.
    expect(within(dialog()).queryByLabelText(/^value$/i)).toBeNull()
    expect(calls.some((c) => c.method === 'PUT')).toBe(false)
  })

  it('a refused write empties the field all the same and asks for a new link', async () => {
    const value = fakeValue()
    const { calls } = api({
      write: (m) => (m === 'PUT' ? { status: 403, body: { code: 'SECRET_LINK_INVALID', message: 'this GitHub Actions secret link is unknown, already used, expired' } } : null),
    })
    await mount()
    const field = await toForm(/repository level/i, 'DEPLOY_KEY')
    fireEvent.change(field, { target: { value } })
    await act(async () => {
      fireEvent.click(within(dialog()).getByRole('button', { name: /^set secret$/i }))
    })
    await within(dialog()).findByText(/not set/i)
    expect(within(dialog()).queryByLabelText(/^value$/i)).toBeNull()
    expect(within(dialog()).getByRole('button', { name: /open the one-time form/i })).toBeTruthy()
    expect(calls.filter((c) => c.method === 'PUT')).toHaveLength(1)
    expect(leaks(value)).toEqual([])
  })
})

describe('§11 Q4: an environment any branch can deploy needs its name typed', () => {
  it('holds Set until the environment’s name is typed, and sends it as confirm_environment', async () => {
    const value = fakeValue()
    const { calls } = api()
    await mount()
    const field = await toForm(/staging/i, 'STAGING_KEY')
    fireEvent.change(field, { target: { value } })
    const set = within(dialog()).getByRole('button', { name: /^set secret$/i }) as HTMLButtonElement
    expect(set.disabled).toBe(true)
    const confirm = within(dialog()).getByLabelText(/type staging/i)
    fireEvent.change(confirm, { target: { value: 'stagin' } })
    expect(set.disabled).toBe(true)
    fireEvent.change(confirm, { target: { value: 'staging' } })
    expect(set.disabled).toBe(false)
    await act(async () => {
      fireEvent.click(set)
    })
    await screen.findByText(/STAGING_KEY was set/i)
    const put = calls.find((c) => c.method === 'PUT')!
    expect(put.url).toBe(`${BASE}/secrets/STAGING_KEY`)
    expect((put.body as Json).environment).toBe('staging')
    expect((put.body as Json).confirm_environment).toBe('staging')
  })

  it('does not ask for a main-only environment, and sends no confirmation', async () => {
    const value = fakeValue()
    const { calls } = api()
    await mount()
    const field = await toForm(/repo-secrets/i, 'MERGE_APP_PRIVATE_KEY')
    expect(within(dialog()).queryByLabelText(/type repo-secrets/i)).toBeNull()
    fireEvent.change(field, { target: { value } })
    await act(async () => {
      fireEvent.click(within(dialog()).getByRole('button', { name: /^set secret$/i }))
    })
    await screen.findByText(/MERGE_APP_PRIVATE_KEY was set/i)
    const put = calls.find((c) => c.method === 'PUT')!
    expect((put.body as Json).environment).toBe('repo-secrets')
    expect('confirm_environment' in (put.body as Json)).toBe(false)
  })
})

describe('the App’s missing Secrets permission', () => {
  it('says so and links to the installation’s review page the API answered', async () => {
    api({
      list: () => ({
        status: 409,
        body: {
          code: 'conflict',
          message: "the SwarmCloud GitHub App's installation has not granted the permission this needs (Secrets and Environments: Read and write)",
          detail: { reason: 'secrets_not_granted', repository: REPOSITORY, review_url: REVIEW, requires: [], runbook: 'docs/runbooks/github-app.md' },
        },
      }),
    })
    const { SecretsActions } = await load()
    render(<SecretsActions repoId={REPO_ID} repository={REPOSITORY} />)
    const link = (await screen.findByRole('link', { name: /review .*permission/i })) as HTMLAnchorElement
    expect(link.href).toBe(REVIEW)
    expect(visible(document.body)).toMatch(/lacks Secrets permission/i)
    expect(screen.queryByRole('table', { name: /repository secrets/i })).toBeNull()
  })

  it('links nowhere but GitHub, whatever the answer carried', async () => {
    api({
      list: () => ({ status: 409, body: { code: 'conflict', message: 'not granted', detail: { reason: 'secrets_not_granted', review_url: 'https://evil.example.com/x' } } }),
    })
    const { SecretsActions } = await load()
    render(<SecretsActions repoId={REPO_ID} repository={REPOSITORY} />)
    await screen.findByText(/lacks Secrets permission/i)
    for (const a of Array.from(document.querySelectorAll('a'))) expect(a.getAttribute('href') ?? '').not.toContain('evil.example.com')
  })
})

describe('§11 Q4: no control creates an environment', () => {
  it('offers Create on GitHub as a link to GitHub, and Check rule as a read; nothing else', async () => {
    const { calls } = api()
    await mount()
    const envs = screen.getByRole('region', { name: /environments/i })
    expect(within(envs).queryByRole('button', { name: /create/i })).toBeNull()
    const create = within(envs).getByRole('link', { name: /create on github/i }) as HTMLAnchorElement
    expect(create.href.startsWith('https://github.com/')).toBe(true)
    expect(screen.queryByRole('button', { name: /create environment/i })).toBeNull()

    fireEvent.click(within(envs).getByRole('button', { name: /^check rule$/i }))
    fireEvent.change(within(envs).getByLabelText(/^environment$/i), { target: { value: 'production' } })
    await act(async () => {
      fireEvent.click(within(envs).getByRole('button', { name: /^check$/i }))
    })
    const made = (await within(envs).findByRole('link', { name: /create production on github/i })) as HTMLAnchorElement
    expect(made.href).toBe(`${SETTINGS}/new`)
    expect(calls.some((c) => c.method === 'GET' && c.url === `${BASE}/environments/production/rule`)).toBe(true)
    // No request but a GET ever touched an environment.
    expect(calls.filter((c) => c.method !== 'GET' && /\/environments/.test(c.url))).toEqual([])
    expect(calls.filter((c) => c.method !== 'GET' && /environments$/.test(c.url))).toEqual([])
  })

  it('the sheet’s destination step offers the same two, and no create', async () => {
    api()
    await mount()
    fireEvent.click(screen.getByRole('button', { name: /^set a secret$/i }))
    expect(within(dialog()).queryByRole('button', { name: /create/i })).toBeNull()
    const link = within(dialog()).getByRole('link', { name: /create on github/i }) as HTMLAnchorElement
    expect(link.href.startsWith('https://github.com/')).toBe(true)
    expect(within(dialog()).getByRole('button', { name: /check rule/i })).toBeTruthy()
  })
})

describe('Delete is typed', () => {
  it('stays disabled until the name is typed, and sends the name and the environment', async () => {
    const { calls } = api()
    await mount()
    fireEvent.click(screen.getByRole('button', { name: /delete merge_app_private_key from repo-secrets/i }))
    const go = within(dialog()).getByRole('button', { name: /^delete$/i }) as HTMLButtonElement
    expect(go.disabled).toBe(true)
    fireEvent.change(within(dialog()).getByRole('textbox'), { target: { value: 'MERGE_APP_PRIVATE_KEY' } })
    expect(go.disabled).toBe(false)
    await act(async () => {
      fireEvent.click(go)
    })
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE')).toBe(true))
    const del = calls.find((c) => c.method === 'DELETE')!
    expect(del.url).toBe(`${BASE}/secrets/MERGE_APP_PRIVATE_KEY`)
    expect(del.body).toEqual({ confirm_name: 'MERGE_APP_PRIVATE_KEY', environment: 'repo-secrets' })
  })
})

describe('a link a tool minted', () => {
  it('opens the form, drops the token from the address before paint, and sends it once', async () => {
    const value = fakeValue()
    const token = linkToken()
    window.history.replaceState(null, '', `/config/secrets?scope=repository&repo=${REPO_ID}&dest=actions&name=MERGE_APP_PRIVATE_KEY&env=repo-secrets&t=${token}`)
    const { calls } = api()
    const { SecretsActions } = await load()
    render(<SecretsActions repoId={REPO_ID} repository={REPOSITORY} link={{ name: 'MERGE_APP_PRIVATE_KEY', environment: 'repo-secrets' }} />)
    expect(window.location.search).not.toContain(token)
    const field = (await within(await screen.findByRole('dialog')).findByLabelText(/^value$/i)) as HTMLInputElement
    expect(visible(dialog())).toMatch(/opened from a one-time link/i)
    fireEvent.change(field, { target: { value } })
    await act(async () => {
      fireEvent.click(within(dialog()).getByRole('button', { name: /^set secret$/i }))
    })
    await screen.findByText(/MERGE_APP_PRIVATE_KEY was set/i)
    expect(calls.some((c) => c.method === 'POST')).toBe(false)
    const put = calls.find((c) => c.method === 'PUT')!
    expect(put.body).toEqual({ value, environment: 'repo-secrets', link_token: token })
    expect(leaks(token)).toEqual([])
    expect(leaks(value)).toEqual([])
  })
})
