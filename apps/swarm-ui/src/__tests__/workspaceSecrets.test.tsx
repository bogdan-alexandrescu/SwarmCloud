// SETTINGS › WORKSPACE › SECRETS (lane SEC-UI-LIST, part of #1347;
// docs/design/workspace-secrets.md §5). The list, the add form and the typed
// delete, held to the rules a page that handles a credential value must keep:
//
//   * after a save the value is in no element's text or attribute, the form's
//     fields are empty, and nothing of it is in the URL or storage;
//   * Delete stays disabled until the secret's NAME is typed exactly, and the
//     dialog lists every enabled grant it disables;
//   * a one-time link's token is gone from the address bar once the page has
//     loaded, and goes back only as the add's `link_token`;
//   * a person who may not manage (`can_manage` false) sees no add or delete;
//   * a value the API wrongly serves is never drawn: the list is an allow-list.
//
// Fixtures only; no network. Every value and token is built at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

/** A credential-shaped value, built at runtime: it must reach the POST body and nothing else. */
const fakeValue = () => ['ws', 'demo'].join('-') + 'Q8m'.repeat(12)
/** A one-time link token, built at runtime. */
const linkToken = () => ['lnk', 'tok'].join('') + 'Hp3'.repeat(10)

const AT = '2026-10-20T09:30:00+00:00'

type Json = Record<string, unknown>

function record(name: string, over: Json = {}): Json {
  return {
    name,
    secret_id: `swarm-tenant-eng-wsec-${name.length}a1b2`,
    provider: 'typesafe',
    kind: 'api_key',
    scope: 'workspace',
    managed: 'store',
    current_version: '2',
    versions: [{ version: '2', added_at: AT, added_by: 'alice@example.com', state: 'enabled', disable_after: null }],
    created_at: AT,
    created_by: 'alice@example.com',
    rotated_at: null,
    rotated_by: null,
    use_in_workspaces: [],
    grants: [
      { consumer: 'profile:claude-code-browser', slot: 'typesafe', enabled: true, changed_at: AT, changed_by: 'alice@example.com' },
      { consumer: 'tool:jev-browser', slot: 'typesafe', enabled: true, changed_at: AT, changed_by: 'alice@example.com' },
      { consumer: 'component:judge', slot: 'typesafe', enabled: false, changed_at: AT, changed_by: 'alice@example.com' },
    ],
    ...over,
  }
}

function listBody(opts: { canManage?: boolean; secrets?: Json[] } = {}): Json {
  return {
    tenant_id: 'eng',
    can_manage: opts.canManage ?? true,
    secrets: opts.secrets ?? [record('typesafe-key')],
    platform_credentials: { runner_provider_keys: [], pool_accounts: { readable: true, accounts: [] } },
    note: 'stored write-only; no route in this API returns a secret\'s value',
  }
}

const CONSUMERS: Json = {
  consumers: [],
  kinds: ['api_key', 'bearer_token', 'basic_auth', 'oauth_client', 'opaque'],
  providers: ['anthropic', 'brave', 'openai', 'typesafe', 'other'],
}

type Reply = { status: number; body: unknown } | null

function api(opts: { list?: Json; write?: (m: string, url: string, body: unknown) => Reply } = {}) {
  let list = opts.list ?? listBody()
  const calls = serve((m, url, body) => {
    if (m === 'GET' && url.startsWith('/v1/workspace/secrets/consumers')) return { status: 200, body: CONSUMERS }
    if (m === 'GET' && url.startsWith('/v1/workspace/secrets')) return { status: 200, body: list }
    if (m !== 'GET' && opts.write) return opts.write(m, url, body)
    return null
  })
  return {
    calls,
    setList: (l: Json) => {
      list = l
    },
  }
}

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../WorkspaceSecrets')
}

const landed = () => screen.findByRole('table', { name: /workspace secrets/i })

function addForm(): HTMLElement {
  return screen.getByRole('form', { name: /add a secret/i })
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

afterEach(() => {
  window.history.replaceState(null, '', '/')
  window.localStorage.clear()
  window.sessionStorage.clear()
})

describe('the list, from GET /v1/workspace/secrets', () => {
  it('shows name, provider, kind, set by, set at, version, last used and the enabled consumers', async () => {
    api()
    const { WorkspaceSecretsScreen } = await load()
    render(<WorkspaceSecretsScreen link={null} />)
    const table = await landed()
    const row = within(table).getByRole('row', { name: /typesafe-key/i })
    const text = visible(row)
    expect(text).toMatch(/typesafe-key/)
    expect(text).toMatch(/typesafe/)
    expect(text).toMatch(/API key/i)
    expect(text).toMatch(/alice@example\.com/)
    expect(text).toMatch(/ago/)
    expect(text).toMatch(/2/)
    expect(within(table).getByRole('columnheader', { name: /last used/i })).toBeTruthy()
    // MUTATION: list every grant, enabled or not -> `component:judge` shows and this fails.
    expect(text).toMatch(/profile:claude-code-browser/)
    expect(text).toMatch(/tool:jev-browser/)
    expect(text).not.toMatch(/component:judge/)
  })

  it('never renders a value, even when the API wrongly serves one', async () => {
    const value = fakeValue()
    api({ list: listBody({ secrets: [record('typesafe-key', { value, payload: value, fields: { username: value, password: value }, last4: value.slice(-4), length: value.length })] }) })
    const { WorkspaceSecretsScreen } = await load()
    render(<WorkspaceSecretsScreen link={null} />)
    await landed()
    // MUTATION: spread the raw record into the row (`{...raw}`) or draw `r.value` -> fails.
    expect(leaks(value)).toEqual([])
    // MUTATION: draw a served `last4` -> fails.
    expect(visible(document.body)).not.toContain(value.slice(-4))
  })
})

describe('Add', () => {
  it('posts the value once, then leaves it in no element, attribute, field, URL or storage', async () => {
    const value = fakeValue()
    let answered = false
    const { calls, setList } = api({
      write: (m, url) => {
        if (m === 'POST' && url === '/v1/workspace/secrets') {
          answered = true
          return { status: 201, body: { secret: record('brave-search', { provider: 'brave', grants: [] }), via_link: false, note: 'n' } }
        }
        return null
      },
    })
    const { WorkspaceSecretsScreen } = await load()
    render(<WorkspaceSecretsScreen link={null} />)
    await landed()
    const form = addForm()
    fireEvent.change(within(form).getByLabelText(/^name/i), { target: { value: 'brave-search' } })
    fireEvent.change(within(form).getByLabelText(/^provider/i), { target: { value: 'brave' } })
    fireEvent.change(within(form).getByLabelText(/^kind/i), { target: { value: 'api_key' } })
    const field = within(form).getByLabelText(/^value/i) as HTMLInputElement
    // MUTATION: make the value field type="text" -> fails.
    expect(field.type).toBe('password')
    fireEvent.change(field, { target: { value } })

    setList(listBody({ secrets: [record('typesafe-key'), record('brave-search', { provider: 'brave', grants: [] })] }))
    await act(async () => {
      fireEvent.click(within(form).getByRole('button', { name: /^add/i }))
    })
    await waitFor(() => expect(answered).toBe(true))
    await screen.findByText(/brave-search was added/i)

    // MUTATION: drop the `field.value = ''` after the read -> the field keeps it and this fails.
    expect((within(addForm()).getByLabelText(/^value/i) as HTMLInputElement).value).toBe('')
    // MUTATION: keep the name after a save -> fails ("the form clears").
    expect((within(addForm()).getByLabelText(/^name/i) as HTMLInputElement).value).toBe('')
    // MUTATION: render a "saved: <value>" confirmation, or make the field controlled -> fails.
    expect(leaks(value)).toEqual([])

    // Exactly one request carried it: the POST, in its body, as `value`.
    const carrying = calls.filter((c) => c.url.includes(value) || JSON.stringify(c.body ?? null).includes(value))
    // MUTATION: send the value in the URL or a second request -> fails.
    expect(carrying).toHaveLength(1)
    expect(carrying[0]!.method).toBe('POST')
    expect(carrying[0]!.body).toEqual({ name: 'brave-search', provider: 'brave', kind: 'api_key', value })
  })

  it('sends a structured kind as its two fields, and empties both', async () => {
    const user = ['svc', 'user'].join('-')
    const pass = fakeValue()
    const { calls } = api({
      write: (m, url) => (m === 'POST' && url === '/v1/workspace/secrets'
        ? { status: 201, body: { secret: record('basic-login', { kind: 'basic_auth', grants: [] }), via_link: false, note: 'n' } }
        : null),
    })
    const { WorkspaceSecretsScreen } = await load()
    render(<WorkspaceSecretsScreen link={null} />)
    await landed()
    const form = addForm()
    fireEvent.change(within(form).getByLabelText(/^name/i), { target: { value: 'basic-login' } })
    fireEvent.change(within(form).getByLabelText(/^kind/i), { target: { value: 'basic_auth' } })
    fireEvent.change(within(form).getByLabelText(/^username/i), { target: { value: user } })
    fireEvent.change(within(form).getByLabelText(/^password/i), { target: { value: pass } })
    await act(async () => {
      fireEvent.click(within(form).getByRole('button', { name: /^add/i }))
    })
    await screen.findByText(/basic-login was added/i)
    const posts = calls.filter((c) => c.method === 'POST')
    expect(posts).toHaveLength(1)
    // MUTATION: send `value` for a structured kind -> fails.
    expect(posts[0]!.body).toEqual({ name: 'basic-login', provider: 'typesafe', kind: 'basic_auth', fields: { username: user, password: pass } })
    expect(leaks(pass)).toEqual([])
  })

  it('empties the value on a refusal too, and says the API\'s reason', async () => {
    const value = fakeValue()
    api({
      write: (m) => (m === 'POST' ? { status: 422, body: { code: 'validation_failed', message: 'a secret\'s name is 1-64 characters' } } : null),
    })
    const { WorkspaceSecretsScreen } = await load()
    render(<WorkspaceSecretsScreen link={null} />)
    await landed()
    const form = addForm()
    fireEvent.change(within(form).getByLabelText(/^name/i), { target: { value: 'ok-name' } })
    fireEvent.change(within(form).getByLabelText(/^value/i), { target: { value } })
    await act(async () => {
      fireEvent.click(within(form).getByRole('button', { name: /^add/i }))
    })
    const alert = await screen.findByRole('alert')
    expect(visible(alert)).toMatch(/1-64 characters/)
    // MUTATION: clear the field only on success -> fails.
    expect((within(addForm()).getByLabelText(/^value/i) as HTMLInputElement).value).toBe('')
    expect(leaks(value)).toEqual([])
  })
})

describe('Delete', () => {
  it('stays disabled until the name is typed exactly, lists the grants it disables, then sends the name', async () => {
    let body: unknown = null
    const { calls } = api({
      write: (m, url, b) => {
        if (m === 'DELETE' && url === '/v1/workspace/secrets/typesafe-key') {
          body = b
          return { status: 200, body: { name: 'typesafe-key', secret_id: 's', versions_destroyed: 1, grants_disabled: ['profile:claude-code-browser', 'tool:jev-browser'] } }
        }
        return null
      },
    })
    const { WorkspaceSecretsScreen } = await load()
    render(<WorkspaceSecretsScreen link={null} />)
    const table = await landed()
    fireEvent.click(within(table).getByRole('button', { name: /delete typesafe-key/i }))
    const dialog = screen.getByRole('dialog', { name: /delete typesafe-key/i })
    // MUTATION: drop the grants list from the dialog -> fails.
    expect(visible(dialog)).toMatch(/profile:claude-code-browser/)
    expect(visible(dialog)).toMatch(/tool:jev-browser/)
    expect(visible(dialog)).not.toMatch(/component:judge/)
    // MUTATION: drop the running-attempts sentence -> fails.
    expect(visible(dialog)).toMatch(/running attempts keep/i)

    const verb = () => within(screen.getByRole('dialog')).getByRole('button', { name: /^delete/i }) as HTMLButtonElement
    const typed = within(dialog).getByLabelText(/type typesafe-key to confirm/i)
    // MUTATION: compare case-insensitively, by prefix, or trimmed -> one of these fails.
    expect(verb().disabled).toBe(true)
    for (const near of ['typesafe-ke', 'TYPESAFE-KEY', 'typesafe-key ', ' typesafe-key', 'typesafe-keys']) {
      fireEvent.change(typed, { target: { value: near } })
      expect(verb().disabled, `typed ${JSON.stringify(near)}`).toBe(true)
    }
    fireEvent.click(verb())
    expect(calls.filter((c) => c.method === 'DELETE')).toHaveLength(0)

    fireEvent.change(typed, { target: { value: 'typesafe-key' } })
    // MUTATION: keep the button disabled for an exact match -> fails.
    expect(verb().disabled).toBe(false)
    await act(async () => {
      fireEvent.click(verb())
    })
    await waitFor(() => expect(body).toEqual({ name: 'typesafe-key' }))
  })
})

describe('who may manage', () => {
  it('a non-manager sees the list read-only: no add form, no delete', async () => {
    api({ list: listBody({ canManage: false }) })
    const { WorkspaceSecretsScreen } = await load()
    render(<WorkspaceSecretsScreen link={null} />)
    const table = await landed()
    expect(visible(table)).toMatch(/typesafe-key/)
    // MUTATION: draw the form whatever `can_manage` says -> fails.
    expect(screen.queryByRole('form', { name: /add a secret/i })).toBeNull()
    expect(document.querySelector('input[type="password"]')).toBeNull()
    // MUTATION: draw Delete whatever `can_manage` says -> fails.
    expect(screen.queryByRole('button', { name: /delete/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^add/i })).toBeNull()
  })

  it('an unread can_manage is not a manager', async () => {
    const list = listBody()
    delete list.can_manage
    api({ list })
    const { WorkspaceSecretsScreen } = await load()
    render(<WorkspaceSecretsScreen link={null} />)
    await landed()
    // MUTATION: treat a missing flag as true (`!== false`) -> fails.
    expect(screen.queryByRole('form', { name: /add a secret/i })).toBeNull()
  })
})

describe('the one-time link: /settings/workspace/secrets?action=add&t=..', () => {
  it('is gone from the address bar once loaded, and goes back only as the add\'s link_token', async () => {
    const value = fakeValue()
    const token = linkToken()
    window.history.replaceState(null, '', `/settings/workspace/secrets?action=add&t=${token}`)
    const { calls } = api({
      write: (m, url) => (m === 'POST' && url === '/v1/workspace/secrets'
        ? { status: 201, body: { secret: record('from-link', { grants: [] }), via_link: true, note: 'n' } }
        : null),
    })
    const { WorkspaceSecretsScreen } = await load()
    render(<WorkspaceSecretsScreen link={{ action: 'add', name: null }} />)
    await landed()
    // MUTATION: drop the replaceState that strips `t` -> fails.
    expect(window.location.href).not.toContain(token)
    expect(window.location.pathname).toBe('/settings/workspace/secrets')
    expect(window.location.search).toBe('?action=add')
    // MUTATION: render the token (a hidden input, a data attribute) -> fails.
    expect(leaks(token)).toEqual([])

    const form = addForm()
    fireEvent.change(within(form).getByLabelText(/^name/i), { target: { value: 'from-link' } })
    fireEvent.change(within(form).getByLabelText(/^value/i), { target: { value } })
    await act(async () => {
      fireEvent.click(within(form).getByRole('button', { name: /^add/i }))
    })
    await screen.findByText(/from-link was added/i)
    const posts = calls.filter((c) => c.method === 'POST')
    // MUTATION: never send the token -> fails.
    expect(posts).toHaveLength(1)
    expect((posts[0]!.body as Json).link_token).toBe(token)
    // The token travelled in that body alone.
    expect(calls.filter((c) => c.url.includes(token) || JSON.stringify(c.body ?? null).includes(token))).toHaveLength(1)
  })

  it('takes the token App captured, and sends it once: a second add goes without it', async () => {
    const token = linkToken()
    const { calls } = api({
      write: (m) => (m === 'POST' ? { status: 201, body: { secret: record('one', { grants: [] }), via_link: true, note: 'n' } } : null),
    })
    const { WorkspaceSecretsScreen } = await load()
    render(<WorkspaceSecretsScreen link={{ action: 'add', name: null, token }} />)
    await landed()
    for (const name of ['first-one', 'second-one']) {
      const form = addForm()
      fireEvent.change(within(form).getByLabelText(/^name/i), { target: { value: name } })
      fireEvent.change(within(form).getByLabelText(/^value/i), { target: { value: fakeValue() } })
      await act(async () => {
        fireEvent.click(within(form).getByRole('button', { name: /^add/i }))
      })
      await screen.findByText(new RegExp(`${name} was added`, 'i'))
    }
    const posts = calls.filter((c) => c.method === 'POST')
    expect(posts).toHaveLength(2)
    // MUTATION: keep the token after it is sent -> the second POST carries it and this fails.
    expect((posts[0]!.body as Json).link_token).toBe(token)
    expect('link_token' in (posts[1]!.body as Json)).toBe(false)
  })
})
