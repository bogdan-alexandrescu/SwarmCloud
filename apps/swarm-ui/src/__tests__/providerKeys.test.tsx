// SETTINGS › API KEYS (lane KEYS-UI; docs/provider-keys.md). The TypeSafe key
// form, held to the owner's rules of 2026-10-14:
//
//   * the key never appears in anything this page draws, stores or links:
//     not in text, not in an attribute, not in localStorage, not in the URL;
//   * the key goes to exactly one place, the body of the PUT, and the input
//     is empty again once it has gone;
//   * a one-time link's token (`/settings/keys/typesafe?scope=..&t=..`) is
//     sent back as `link_token` and dropped from the address bar;
//   * the tenant key's row is an admin's only;
//   * a 503 -- the deployed state until the KEYS-IAM grant is applied -- says
//     plainly that keys cannot be saved yet, with the server's reason.
//
// Fixtures only; no network. The key and the link token are built at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

/** A key-shaped value, built at runtime, that must reach the PUT body and nothing else. */
const fakeKey = () => ['ts', 'demo'].join('-') + 'K7v'.repeat(11)
/** A link token, built at runtime. */
const linkToken = () => ['lnk', 'tok'].join('') + 'Rz4'.repeat(9)

const AT = '2026-10-14T09:30:00+00:00'

type Json = Record<string, unknown>

function keyStatus(set: boolean, secret: string, over: Json = {}): Json {
  return {
    available: true,
    set,
    set_at: set ? AT : null,
    set_by: set ? 'alice@example.com' : null,
    version: set ? '3' : null,
    secret_id: secret,
    ...over,
  }
}

function statusBody(opts: { user?: Json; tenant?: Json; uses?: 'user' | 'tenant' | 'none' } = {}): Json {
  const user = opts.user ?? keyStatus(false, 'swarm-tenant-u-alice-typesafe')
  const tenant = opts.tenant ?? keyStatus(false, 'swarm-tenant-eng-typesafe')
  const uses = opts.uses ?? 'none'
  return {
    provider: 'typesafe',
    user,
    tenant: { ...tenant, tenant_id: 'eng' },
    resolution: { tenant_id: 'eng', uses, secret_id: uses === 'none' ? null : uses === 'user' ? user.secret_id : tenant.secret_id },
    consumed_by: [],
  }
}

function meBody(admin: boolean, kind: 'group' | 'user' = 'group'): Json {
  return {
    tenant: { tenant_id: kind === 'user' ? 'u-alice' : 'eng', kind, display_name: null },
    principal: { email: 'alice@example.com', domain: 'example.com', groups: [], is_admin: admin },
    environment: 'dev',
    environment_declared: true,
  }
}

type Reply = { status: number; body: unknown } | null

/** A server: the status read, the frame's `me`, and whatever `write` answers for a write. */
function api(opts: { admin?: boolean; status?: Json; kind?: 'group' | 'user'; write?: (m: string, url: string, body: unknown) => Reply } = {}) {
  let status = opts.status ?? statusBody()
  const calls = serve((m, url, body) => {
    if (m === 'GET' && url.startsWith('/v1/tenants/me')) return { status: 200, body: meBody(opts.admin === true, opts.kind) }
    if (m === 'GET' && url.startsWith('/v1/provider-keys/typesafe')) return { status: 200, body: status }
    if (m !== 'GET' && opts.write) return opts.write(m, url, body)
    return null
  })
  return {
    calls,
    setStatus: (s: Json) => {
      status = s
    },
  }
}

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../ProviderKeys')
}

const landed = () => screen.findByText(/your agents in eng would use/i)

function row(name: RegExp): HTMLElement {
  return screen.getByRole('region', { name })
}

function keyInput(region: HTMLElement): HTMLInputElement {
  return within(region).getByLabelText(/typesafe key/i) as HTMLInputElement
}

afterEach(() => {
  window.history.replaceState(null, '', '/')
  window.localStorage.clear()
  window.sessionStorage.clear()
})

describe('the status, from GET /v1/provider-keys/typesafe', () => {
  it('shows set or not set, set at, set by and version, and which key tasks here would use', async () => {
    api({
      admin: true,
      status: statusBody({ user: keyStatus(true, 'swarm-tenant-u-alice-typesafe'), uses: 'user' }),
    })
    const { ProviderKeysScreen } = await load()
    render(<ProviderKeysScreen link={null} />)
    await landed()
    const mine = visible(row(/your key/i))
    // Set, when, by whom, which version: never a value.
    expect(mine).toMatch(/set \S+ ago by alice@example\.com · version 3/i)
    expect(within(row(/your key/i)).getByRole('button', { name: /replace/i })).toBeTruthy()
    expect(visible(row(/tenant key/i))).toMatch(/not set/i)
    expect(within(row(/tenant key/i)).getByRole('button', { name: /^save/i })).toBeTruthy()
    expect(visible(document.body)).toMatch(/your agents in eng would use: your key/i)
  })

  it('names the tenant key, or none, as what tasks would use', async () => {
    api({ admin: true, status: statusBody({ tenant: keyStatus(true, 'swarm-tenant-eng-typesafe'), uses: 'tenant' }) })
    let mod = await load()
    const { unmount } = render(<mod.ProviderKeysScreen link={null} />)
    await landed()
    expect(visible(document.body)).toMatch(/would use: the tenant key/i)
    unmount()

    api({ admin: true })
    mod = await load()
    render(<mod.ProviderKeysScreen link={null} />)
    await landed()
    expect(visible(document.body)).toMatch(/would use: none/i)
  })

  it('draws the tenant key row for an admin only', async () => {
    api({ admin: false, status: statusBody({ tenant: keyStatus(true, 'swarm-tenant-eng-typesafe'), uses: 'tenant' }) })
    const { ProviderKeysScreen } = await load()
    render(<ProviderKeysScreen link={null} />)
    await landed()
    expect(screen.getByRole('region', { name: /your key/i })).toBeTruthy()
    expect(screen.queryByRole('region', { name: /tenant key/i })).toBeNull()
    // The resolution still says what a task would use: that is a member's to know.
    expect(visible(document.body)).toMatch(/would use: the tenant key/i)
  })

  it('says why there is no key of your own to set without a ready personal workspace', async () => {
    api({ status: statusBody({ user: { available: false, set: false, set_at: null, set_by: null, version: null, secret_id: null } }) })
    const { ProviderKeysScreen } = await load()
    render(<ProviderKeysScreen link={null} />)
    await landed()
    const mine = row(/your key/i)
    expect(visible(mine)).toMatch(/personal workspace/i)
    expect(within(mine).queryByLabelText(/typesafe key/i)).toBeNull()
  })
})

describe('the key itself', () => {
  it('is a write-only password field: never prefilled, autocomplete off', async () => {
    api({ status: statusBody({ user: keyStatus(true, 'swarm-tenant-u-alice-typesafe'), uses: 'user' }) })
    const { ProviderKeysScreen } = await load()
    render(<ProviderKeysScreen link={null} />)
    await landed()
    const input = keyInput(row(/your key/i))
    expect(input.type).toBe('password')
    expect(input.getAttribute('autocomplete')).toBe('off')
    expect(input.value).toBe('')
    expect(input.getAttribute('value')).toBeNull()
  })

  it('never appears in the page, its attributes, storage or the URL, and leaves only in the PUT body', async () => {
    const key = fakeKey()
    let answered = false
    const { calls, setStatus } = api({
      write: (m, url) => {
        if (m === 'PUT' && url === '/v1/provider-keys/typesafe/user') {
          answered = true
          return { status: 200, body: { status: keyStatus(true, 'swarm-tenant-u-alice-typesafe'), scope: 'user', via_link: false, note: 'stored write-only' } }
        }
        return null
      },
    })
    const { ProviderKeysScreen } = await load()
    render(<ProviderKeysScreen link={null} />)
    await landed()
    const mine = row(/your key/i)
    const input = keyInput(mine)
    fireEvent.change(input, { target: { value: key } })

    const leaks = () => {
      const where: string[] = []
      if (document.documentElement.outerHTML.includes(key)) where.push('the document')
      if (visible(document.body).includes(key)) where.push('the text')
      if (window.location.href.includes(key)) where.push('the URL')
      for (const store of [window.localStorage, window.sessionStorage]) {
        for (let i = 0; i < store.length; i++) {
          const k = store.key(i) ?? ''
          if (k.includes(key) || (store.getItem(k) ?? '').includes(key)) where.push('storage')
        }
      }
      return where
    }
    expect(leaks()).toEqual([])

    setStatus(statusBody({ user: keyStatus(true, 'swarm-tenant-u-alice-typesafe'), uses: 'user' }))
    fireEvent.click(within(mine).getByRole('button', { name: /^save/i }))
    await waitFor(() => expect(answered).toBe(true))
    await screen.findByText(/^saved/i)

    // Cleared after submit, and still nowhere on the page.
    expect(keyInput(row(/your key/i)).value).toBe('')
    expect(leaks()).toEqual([])

    // Exactly one request carried it: the PUT, in its body, as `api_key`.
    const carrying = calls.filter((c) => c.url.includes(key) || JSON.stringify(c.body ?? null).includes(key))
    expect(carrying).toHaveLength(1)
    expect(carrying[0]!.method).toBe('PUT')
    expect(carrying[0]!.url).toBe('/v1/provider-keys/typesafe/user')
    expect(carrying[0]!.body).toEqual({ api_key: key })
  })

  it('sends the tenant key to the tenant route, for an admin', async () => {
    const key = fakeKey()
    const { calls } = api({
      admin: true,
      write: (m, url) => (m === 'PUT' && url === '/v1/provider-keys/typesafe/tenant'
        ? { status: 200, body: { status: keyStatus(true, 'swarm-tenant-eng-typesafe'), scope: 'tenant', via_link: false, note: 'n' } }
        : null),
    })
    const { ProviderKeysScreen } = await load()
    render(<ProviderKeysScreen link={null} />)
    await landed()
    const tenant = row(/tenant key/i)
    fireEvent.change(keyInput(tenant), { target: { value: key } })
    fireEvent.click(within(tenant).getByRole('button', { name: /^save/i }))
    await screen.findByText(/^saved/i)
    const puts = calls.filter((c) => c.method === 'PUT')
    expect(puts.map((c) => c.url)).toEqual(['/v1/provider-keys/typesafe/tenant'])
  })

  it('says plainly that keys cannot be saved yet on a 503, with the reason, and keeps nothing', async () => {
    const key = fakeKey()
    api({
      write: (m) => (m === 'PUT'
        ? { status: 503, body: { code: 'upstream_unavailable', message: 'could not create the tenant secret: permission denied' } }
        : null),
    })
    const { ProviderKeysScreen } = await load()
    render(<ProviderKeysScreen link={null} />)
    await landed()
    const mine = row(/your key/i)
    fireEvent.change(keyInput(mine), { target: { value: key } })
    fireEvent.click(within(mine).getByRole('button', { name: /^save/i }))
    const alert = await screen.findByRole('alert')
    expect(visible(alert)).toMatch(/keys can.t be saved yet: could not create the tenant secret: permission denied/i)
    expect(keyInput(row(/your key/i)).value).toBe('')
    expect(document.documentElement.outerHTML.includes(key)).toBe(false)
  })
})

describe('Remove', () => {
  it('sends nothing until the confirmation is typed exactly, and Esc keeps the key', async () => {
    let deleted = 0
    const { calls } = api({
      status: statusBody({ user: keyStatus(true, 'swarm-tenant-u-alice-typesafe'), uses: 'user' }),
      write: (m, url) => {
        if (m === 'DELETE' && url === '/v1/provider-keys/typesafe/user') {
          deleted += 1
          return { status: 200, body: { status: keyStatus(false, 'swarm-tenant-u-alice-typesafe'), scope: 'user', versions_destroyed: 1 } }
        }
        return null
      },
    })
    const { ProviderKeysScreen } = await load()
    render(<ProviderKeysScreen link={null} />)
    await landed()
    const mine = row(/your key/i)
    fireEvent.click(within(mine).getByRole('button', { name: /^remove/i }))
    const ask = within(mine).getByRole('group', { name: /remove your/i })
    const confirm = within(ask).getByRole('button', { name: /^remove/i }) as HTMLButtonElement
    expect(confirm.disabled).toBe(true)
    fireEvent.click(confirm)

    const typed = within(ask).getByLabelText(/type typesafe to remove/i)
    fireEvent.change(typed, { target: { value: 'typesaf' } })
    expect((within(ask).getByRole('button', { name: /^remove/i }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.keyDown(ask, { key: 'Escape' })
    expect(within(row(/your key/i)).queryByRole('group', { name: /remove your/i })).toBeNull()
    expect(calls.filter((c) => c.method === 'DELETE')).toHaveLength(0)

    fireEvent.click(within(row(/your key/i)).getByRole('button', { name: /^remove/i }))
    const again = within(row(/your key/i)).getByRole('group', { name: /remove your/i })
    fireEvent.change(within(again).getByLabelText(/type typesafe to remove/i), { target: { value: 'typesafe' } })
    fireEvent.click(within(again).getByRole('button', { name: /^remove/i }))
    await waitFor(() => expect(deleted).toBe(1))
  })

  it('asks an admin to type the tenant id to remove the tenant key', async () => {
    api({ admin: true, status: statusBody({ tenant: keyStatus(true, 'swarm-tenant-eng-typesafe'), uses: 'tenant' }) })
    const { ProviderKeysScreen } = await load()
    render(<ProviderKeysScreen link={null} />)
    await landed()
    const tenant = row(/tenant key/i)
    fireEvent.click(within(tenant).getByRole('button', { name: /^remove/i }))
    expect(within(tenant).getByLabelText(/type eng to remove/i)).toBeTruthy()
  })
})

describe('the one-time link: /settings/keys/typesafe?scope=..&t=..', () => {
  it('drops the token from the address bar, sends it back as link_token, and says the tab can close', async () => {
    const key = fakeKey()
    const token = linkToken()
    window.history.replaceState(null, '', `/settings/keys/typesafe?scope=user&t=${token}`)
    const { calls } = api({
      write: (m, url) => (m === 'PUT' && url === '/v1/provider-keys/typesafe/user'
        ? { status: 200, body: { status: keyStatus(true, 'swarm-tenant-u-alice-typesafe'), scope: 'user', via_link: true, note: 'n' } }
        : null),
    })
    const { ProviderKeysScreen } = await load()
    render(<ProviderKeysScreen link={{ scope: 'user' }} />)
    await landed()
    expect(window.location.href).not.toContain(token)
    expect(window.location.pathname).toBe('/settings/keys/typesafe')
    expect(document.documentElement.outerHTML.includes(token)).toBe(false)

    const mine = row(/your key/i)
    fireEvent.change(keyInput(mine), { target: { value: key } })
    await act(async () => {
      fireEvent.click(within(mine).getByRole('button', { name: /^save/i }))
    })
    await screen.findByText(/saved.+you can close this tab/i)
    const put = calls.find((c) => c.method === 'PUT')
    expect(put?.body).toEqual({ api_key: key, link_token: token })
    expect(window.location.href).not.toContain(token)
  })

  it('sends the token with the scope it was minted for, never the other', async () => {
    const token = linkToken()
    window.history.replaceState(null, '', `/settings/keys/typesafe?scope=tenant&t=${token}`)
    const { calls } = api({
      admin: true,
      write: (m) => (m === 'PUT' ? { status: 200, body: { status: keyStatus(true, 'x'), scope: 'user', via_link: false, note: 'n' } } : null),
    })
    const { ProviderKeysScreen } = await load()
    render(<ProviderKeysScreen link={{ scope: 'tenant' }} />)
    await landed()
    const mine = row(/your key/i)
    fireEvent.change(keyInput(mine), { target: { value: fakeKey() } })
    fireEvent.click(within(mine).getByRole('button', { name: /^save/i }))
    await screen.findByText(/^saved/i)
    const put = calls.find((c) => c.method === 'PUT')
    expect(put?.url).toBe('/v1/provider-keys/typesafe/user')
    expect(put?.body).not.toHaveProperty('link_token')
  })
})
