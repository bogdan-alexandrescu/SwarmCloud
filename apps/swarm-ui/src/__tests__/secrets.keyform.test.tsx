// CONFIG › SECRETS, THE KEY FORM AND THE KEYS CARD (lane CONSOLE-SMALL-FIXES,
// part of #2157). Settings › API keys (ProviderKeys.tsx, lane KEYS-UI) was
// unrouted once SSC-UI moved keys to Config › Secrets › "Keys for agents and
// MCP" (BGD, 2026-10-23 23:57Z), and BGD's answer of 2026-10-25 deleted it.
// Its KeyRow's behaviours, held to the owner's rules of 2026-10-14, are held
// here on Secrets.tsx's KeyForm and KeysCard instead:
//
//   * the key is a write-only password field, never prefilled, autocomplete off;
//   * the key goes to exactly one place, the body of the PUT, the field is
//     empty before the request leaves, and the page, the URL and storage never
//     hold it;
//   * your key goes to the user route and the workspace's to the tenant route;
//   * a 503 -- the deployed state until the KEYS-IAM grant is applied -- says
//     the server's reason and keeps nothing;
//   * Delete sends nothing until the name is typed exactly, and Keep it keeps
//     the key;
//   * the row says when and by whom it was set, and offers Set, Replace and
//     Delete only where the API says the reader may manage it (`can_manage`:
//     the workspace admin or a platform admin, which KeyRow decided itself).
//
// The one-time link's token going back as `link_token` and leaving the bar is
// secrets.routes.test.tsx's. Fixtures only; no network. Every key and token is
// built at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null

const fakeKey = () => ['ts', 'demo'].join('-') + 'K7v'.repeat(11)
const minted = () => ['own', 'key'].join('') + 'Rz4'.repeat(10)
const AT = '2026-10-14T09:30:00+00:00'

function keyRow(over: Json = {}): Json {
  return {
    section: 'keys', store: 'provider_keys', name: 'typesafe', scope: 'personal', kind: null, status: 'set', set_at: AT,
    set_by: 'alice@example.com', consumers: [], shadowed_by: [], shadows: [], can_manage: true, ...over,
  }
}

function api(o: { rows?: Json[]; write?: (m: string, url: string, body: unknown) => Reply } = {}) {
  return serve((m, url, body) => {
    const u = decodeURIComponent(url)
    if (m === 'GET' && u.startsWith('/v1/secrets/overview')) {
      const q = new URLSearchParams(u.slice(u.indexOf('?') + 1))
      const scope = q.get('scope') ?? 'workspace'
      const tenant = scope === 'personal' ? 'u-alice' : 'eng'
      return { status: 200, body: { tenant_id: tenant, scope, personal_tenant_id: 'u-alice', can_manage: true, rows: o.rows ?? [] } }
    }
    if (m === 'GET' && u.startsWith('/v1/repositories')) return { status: 200, body: { repositories: [] } }
    if (m === 'GET' && u.startsWith('/v1/workspace/secrets')) return { status: 200, body: { tenant_id: 'eng', can_manage: true, secrets: [] } }
    if (m === 'POST' && u.endsWith('/links')) return { status: 201, body: { url: null, path: `/config/secrets?key=typesafe&t=${minted()}`, expires_at: AT } }
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

describe('the TypeSafe key form', () => {
  it('is a write-only password field: never prefilled, autocomplete off', async () => {
    api()
    const { SecretsScreen } = await load()
    render(<SecretsScreen view="scope=personal&key=typesafe" />)
    const form = await screen.findByRole('form', { name: /set your typesafe key/i })
    const input = within(form).getByLabelText(/typesafe key/i) as HTMLInputElement
    // MUTATION: make it type="text", prefill it or let the browser remember it -> fails.
    expect(input.type).toBe('password')
    expect(input.getAttribute('autocomplete')).toBe('off')
    expect(input.value).toBe('')
    expect(input.getAttribute('value')).toBeNull()
  })

  it('sends your key to the user route alone, in the PUT body, and the page never holds it', async () => {
    const key = fakeKey()
    let atPut: string | null = 'not put'
    const calls = api({
      write: (m, u) => {
        if (m === 'PUT' && u === '/v1/provider-keys/typesafe/user') {
          atPut = (document.querySelector('input[type="password"]') as HTMLInputElement | null)?.value ?? null
          return { status: 200, body: { scope: 'user', via_link: true, note: 'stored write-only' } }
        }
        return null
      },
    })
    const { SecretsScreen } = await load()
    render(<SecretsScreen view="scope=personal&key=typesafe" />)
    const form = await screen.findByRole('form', { name: /set your typesafe key/i })
    fireEvent.change(within(form).getByLabelText(/typesafe key/i), { target: { value: key } })
    expect(leaks(key)).toEqual(['an input'])
    await act(async () => {
      fireEvent.click(within(form).getByRole('button', { name: /set secret/i }))
    })
    await screen.findByText(/your typesafe key was written/i)
    // MUTATION: empty the field only after the answer -> it still held the key when the PUT arrived.
    expect(atPut).toBe('')
    // MUTATION: draw "saved: <key>", keep it in the field, or store it -> fails.
    expect(leaks(key)).toEqual([])
    const carrying = calls.filter((c) => c.url.includes(key) || JSON.stringify(c.body ?? null).includes(key))
    expect(carrying).toHaveLength(1)
    expect(carrying[0]).toMatchObject({ method: 'PUT', url: '/v1/provider-keys/typesafe/user', body: { api_key: key, link_token: minted() } })
  })

  it("sends the workspace's key to the tenant route", async () => {
    const calls = api({
      write: (m, u) => (m === 'PUT' && u === '/v1/provider-keys/typesafe/tenant' ? { status: 200, body: { scope: 'tenant', via_link: true, note: 'n' } } : null),
    })
    const { SecretsScreen } = await load()
    render(<SecretsScreen view="scope=workspace&key=typesafe" />)
    const form = await screen.findByRole('form', { name: /set the workspace.s typesafe key/i })
    fireEvent.change(within(form).getByLabelText(/typesafe key/i), { target: { value: fakeKey() } })
    await act(async () => {
      fireEvent.click(within(form).getByRole('button', { name: /set secret/i }))
    })
    await screen.findByText(/typesafe key was written/i)
    // MUTATION: send every scope to the user route -> the workspace's key is written as the person's.
    expect(calls.filter((c) => c.method === 'PUT').map((c) => c.url)).toEqual(['/v1/provider-keys/typesafe/tenant'])
    expect(calls.find((c) => c.url === '/v1/provider-keys/typesafe/links')?.body).toEqual({ scope: 'tenant' })
  })

  it("says the server's reason on a 503, and keeps nothing", async () => {
    const key = fakeKey()
    api({
      write: (m) => (m === 'PUT'
        ? { status: 503, body: { code: 'upstream_unavailable', message: 'could not create the tenant secret: permission denied' } }
        : null),
    })
    const { SecretsScreen } = await load()
    render(<SecretsScreen view="scope=personal&key=typesafe" />)
    const form = await screen.findByRole('form', { name: /set your typesafe key/i })
    fireEvent.change(within(form).getByLabelText(/typesafe key/i), { target: { value: key } })
    await act(async () => {
      fireEvent.click(within(form).getByRole('button', { name: /set secret/i }))
    })
    // MUTATION: swallow the refusal, or say it without the server's words -> fails.
    const alert = await screen.findByRole('alert')
    expect(visible(alert)).toMatch(/your typesafe key was not set: could not create the tenant secret: permission denied/i)
    expect(leaks(key)).toEqual([])
  })
})

describe('the Keys card', () => {
  it('says when and by whom the key was set, and offers Replace and Delete where the reader may manage it', async () => {
    api({ rows: [keyRow({ scope: 'workspace' })] })
    const { SecretsScreen } = await load()
    render(<SecretsScreen view="scope=workspace" />)
    const table = await screen.findByRole('table', { name: /keys for agents and mcp/i })
    await waitFor(() => expect(visible(table)).toMatch(/set \S+.* ago by alice@example\.com/i))
    expect(within(table).getByRole('button', { name: /replace the typesafe key/i })).toBeTruthy()
    expect(within(table).getByRole('button', { name: /delete the typesafe key/i })).toBeTruthy()
  })

  it('offers nothing to change where the API says the reader may not manage it', async () => {
    api({ rows: [keyRow({ scope: 'workspace', can_manage: false })] })
    const { SecretsScreen } = await load()
    render(<SecretsScreen view="scope=workspace" />)
    const table = await screen.findByRole('table', { name: /keys for agents and mcp/i })
    await waitFor(() => expect(visible(table)).toMatch(/alice@example\.com/))
    // MUTATION: draw the actions for every row -> a member is offered a key they cannot set.
    expect(within(table).queryByRole('button')).toBeNull()
  })

  it('Delete sends nothing until the name is typed exactly, and Keep it keeps the key', async () => {
    let deleted = 0
    const calls = api({
      rows: [keyRow()],
      write: (m, u) => {
        if (m === 'DELETE' && u === '/v1/provider-keys/typesafe/user') {
          deleted += 1
          return { status: 200, body: { scope: 'user', versions_destroyed: 1 } }
        }
        return null
      },
    })
    const { SecretsScreen } = await load()
    render(<SecretsScreen view="scope=personal" />)
    const table = await screen.findByRole('table', { name: /keys for agents and mcp/i })
    fireEvent.click(await within(table).findByRole('button', { name: /delete the typesafe key/i }))
    let dialog = screen.getByRole('dialog')
    const confirm = () => within(screen.getByRole('dialog')).getByRole('button', { name: /^delete$/i }) as HTMLButtonElement
    // MUTATION: let the dialog confirm without the typed name -> enabled.
    expect(confirm().disabled).toBe(true)
    fireEvent.change(within(dialog).getByLabelText(/type typesafe to confirm/i), { target: { value: 'typesaf' } })
    expect(confirm().disabled).toBe(true)
    fireEvent.click(within(dialog).getByRole('button', { name: /keep it/i }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(calls.filter((c) => c.method === 'DELETE')).toHaveLength(0)

    fireEvent.click(within(table).getByRole('button', { name: /delete the typesafe key/i }))
    dialog = screen.getByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText(/type typesafe to confirm/i), { target: { value: 'typesafe' } })
    fireEvent.click(confirm())
    // MUTATION: delete the workspace's key from Yours -> the tenant route, and `deleted` stays 0.
    await waitFor(() => expect(deleted).toBe(1))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(calls.filter((c) => c.method === 'DELETE').map((c) => c.url)).toEqual(['/v1/provider-keys/typesafe/user'])
  })
})

describe('Settings › API keys is gone', () => {
  // Every source and test module, read through Vite's own loader.
  const SOURCES = import.meta.glob(['../**/*.{ts,tsx}', '!../**/node_modules/**'], { query: '?raw', import: 'default', eager: true }) as Record<string, string>

  it('no ProviderKeys module is left, and nothing imports one', () => {
    const paths = Object.keys(SOURCES)
    expect(paths.length).toBeGreaterThan(100)
    // MUTATION: put ProviderKeys.tsx back -> it is listed here.
    expect(paths.filter((p) => /\/ProviderKeys\.tsx?$/.test(p))).toEqual([])
    const importing = Object.entries(SOURCES)
      .filter(([, text]) => /from\s+['"][./]+ProviderKeys['"]|import\(\s*['"][./]+ProviderKeys['"]\s*\)/.test(text))
      .map(([p]) => p)
    expect(importing).toEqual([])
  })
})
