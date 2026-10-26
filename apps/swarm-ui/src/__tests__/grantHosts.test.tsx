// THE GRANT MATRIX'S HOSTS FIELD (lane CF-ACCESS-HOSTS-UI, part of #2442;
// swarm_api/workspace_secrets.py `slot_hosts`, docs/design/workspace-secrets.md
// "The cf-access slot"). A grant into the `cf-access` slot carries the exact
// hosts its secret may be sent to, 1 to 16. Held to:
//
//   * the cf-access slot is its own column, and its cell lists the grant's
//     current hosts, whole;
//   * add and remove edit the list, and Save writes ONE grant carrying
//     `{enabled, slot, hosts}` and nothing else -- no value, no field of one;
//   * a tick with no host, a list emptied on an enabled grant, a 17th host
//     and a host `SLOT_HOST` refuses are each refused before any call, in the
//     API's own words;
//   * the form's rule is the API module's rule, read from its source.
//
// Fixtures only; no network.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { HOST_REFUSED, HOSTS_TOO_MANY, MAX_SLOT_HOSTS, SLOT_HOST, hostsNeeded } from '../WorkspaceSecrets'

const AT = '2026-10-26T09:30:00+00:00'
const BROWSER = 'profile:claude-code-browser'
const CF = 'cf-access'
const TWO = ['admin.example.com', 'api.admin.example.com']

type Json = Record<string, unknown>
type Call = { method: string; url: string; body: unknown }

function grant(consumer: string, enabled: boolean, slot: string, hosts?: string[]): Json {
  return { consumer, slot, enabled, changed_at: AT, changed_by: 'alice@example.com', ...(hosts === undefined ? {} : { hosts }) }
}

function record(name: string, grants: Json[], kind = 'oauth_client'): Json {
  return {
    name,
    secret_id: `swarm-tenant-eng-wsec-${name.length}e5f6`,
    provider: 'other',
    kind,
    scope: 'workspace',
    managed: 'store',
    current_version: '1',
    versions: [{ version: '1', added_at: AT, added_by: 'alice@example.com', state: 'enabled', disable_after: null }],
    created_at: AT,
    created_by: 'alice@example.com',
    rotated_at: null,
    rotated_by: null,
    use_in_workspaces: [],
    grants,
  }
}

function listBody(secrets: Json[], canManage = true): Json {
  return {
    tenant_id: 'eng',
    can_manage: canManage,
    secrets,
    platform_credentials: { runner_provider_keys: [], pool_accounts: { readable: true, accounts: [] } },
    note: 'stored write-only',
  }
}

const FILES = ['api_key', 'bearer_token', 'basic_auth', 'oauth_client', 'opaque']

/** The catalogue with contract request 117's slot on claude-code-browser. */
const CONSUMERS: Json = {
  consumers: [
    { consumer: 'profile:claude-code', kind: 'profile', name: 'claude-code', slots: [{ slot: 'files', kinds: FILES, means: 'one file' }], grantable: true, note: '' },
    {
      consumer: BROWSER, kind: 'profile', name: 'claude-code-browser',
      slots: [{ slot: 'files', kinds: FILES, means: 'one file' }, { slot: CF, kinds: ['oauth_client'], means: 'a Cloudflare Access service token' }],
      grantable: true, note: '',
    },
    { consumer: 'component:judge', kind: 'component', name: 'judge', slots: [{ slot: 'typesafe', kinds: ['api_key'], means: 'the TypeSafe key' }], grantable: true, note: '' },
  ],
  kinds: FILES,
  providers: ['anthropic', 'typesafe', 'other'],
}

function api(list: Json, write?: (m: string, url: string, body: unknown) => { status: number; body: unknown } | null) {
  return serve((m, url, body) => {
    if (m === 'GET' && url.startsWith('/v1/workspace/secrets/consumers')) return { status: 200, body: CONSUMERS }
    if (m === 'GET' && url.startsWith('/v1/workspace/secrets')) return { status: 200, body: list }
    if (m === 'GET' && url.startsWith('/v1/secrets/overview')) {
      return { status: 200, body: { tenant_id: 'eng', scope: 'workspace', can_manage: list.can_manage, rows: [] } }
    }
    if (m === 'GET' && url.startsWith('/v1/repositories')) return { status: 200, body: { repositories: [] } }
    if (m !== 'GET' && write) return write(m, decodeURIComponent(url), body)
    return null
  }) as Call[]
}

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../Secrets')
}

async function matrix() {
  await screen.findByRole('table', { name: /workspace secrets/i })
  fireEvent.click(screen.getByRole('button', { name: /open the grant matrix/i }))
  return screen.findByRole('table', { name: /secret grants/i })
}

const writes = (calls: Call[]) => calls.filter((c) => c.method !== 'GET')
const cfBox = (m: HTMLElement, secret: string) =>
  within(m).getByRole('checkbox', { name: `Grant ${secret} to ${BROWSER} in its ${CF} slot` }) as HTMLInputElement
const cfCell = (m: HTMLElement, secret: string) => cfBox(m, secret).closest('td') as HTMLElement
const listed = (cell: HTMLElement) => within(cell).queryAllByRole('listitem').map((li) => within(li).getByText(/\./).textContent)
const what = (secret: string) => `${secret} in ${BROWSER}'s ${CF} slot`

async function addHost(cell: HTMLElement, secret: string, host: string) {
  const field = within(cell).getByRole('textbox', { name: `Host for ${what(secret)}` }) as HTMLInputElement
  fireEvent.change(field, { target: { value: host } })
  await act(async () => {
    fireEvent.submit(within(cell).getByRole('form', { name: `Add a host for ${what(secret)}` }))
  })
}

afterEach(() => {
  window.history.replaceState(null, '', '/')
  window.localStorage.clear()
  window.sessionStorage.clear()
})

describe('the cf-access cell\'s hosts', () => {
  it('is its own column and lists the grant\'s current hosts, whole', async () => {
    api(listBody([record('admin-access', [grant(BROWSER, true, CF, TWO)])]))
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={null} />)
    const m = await matrix()
    // MUTATION: draw claude-code-browser as one column (no cf-access cell) -> getByRole fails.
    const box = cfBox(m, 'admin-access')
    expect(box.checked).toBe(true)
    // MUTATION: count a cf-access grant as the plain cell's -> fails.
    expect((within(m).getByRole('checkbox', { name: `Grant admin-access to ${BROWSER}` }) as HTMLInputElement).checked).toBe(false)
    // MUTATION: drop the list, or show a count instead of the names -> fails.
    expect(listed(cfCell(m, 'admin-access'))).toEqual(TWO)
    expect(visible(cfCell(m, 'admin-access'))).toMatch(new RegExp(`2 of ${MAX_SLOT_HOSTS} hosts`))
  })

  it('a kind the slot does not read gets no hosts field, and says why', async () => {
    api(listBody([record('search-key', [], 'api_key')]))
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={null} />)
    const m = await matrix()
    const box = cfBox(m, 'search-key')
    // MUTATION: offer cf-access to every kind the profile's files slot reads -> fails.
    expect(box.disabled).toBe(true)
    expect(visible(cfCell(m, 'search-key'))).toMatch(/claude-code-browser reads an OAuth client/)
    expect(within(cfCell(m, 'search-key')).queryByRole('textbox')).toBeNull()
  })

  it('add and remove edit the list, and Save writes {enabled, slot, hosts} and no value', async () => {
    const calls = api(listBody([record('admin-access', [grant(BROWSER, true, CF, TWO)])]), (m, url, body) =>
      m === 'PUT' && url === `/v1/workspace/secrets/admin-access/grants/${BROWSER}`
        ? { status: 200, body: { secret: record('admin-access', [grant(BROWSER, true, CF, (body as { hosts: string[] }).hosts)]), takes_effect: 'at the next attempt' } }
        : null)
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={null} />)
    const m = await matrix()
    await addHost(cfCell(m, 'admin-access'), 'admin-access', 'console.example.com')
    // MUTATION: drop the added host, or keep it unsorted -> fails.
    expect(listed(cfCell(m, 'admin-access'))).toEqual(['admin.example.com', 'api.admin.example.com', 'console.example.com'])
    await act(async () => {
      fireEvent.click(within(cfCell(m, 'admin-access')).getByRole('button', { name: `Remove admin.example.com from ${what('admin-access')}` }))
    })
    // MUTATION: make Remove do nothing -> fails.
    expect(listed(cfCell(m, 'admin-access'))).toEqual(['api.admin.example.com', 'console.example.com'])
    // MUTATION: write on every edit rather than on Save -> fails.
    expect(writes(calls)).toHaveLength(0)
    await act(async () => {
      fireEvent.click(within(cfCell(m, 'admin-access')).getByRole('button', { name: 'Save hosts' }))
    })
    await screen.findByText(/takes effect at the next attempt/i)
    const sent = writes(calls)
    expect(sent).toHaveLength(1)
    expect(sent[0]!.method).toBe('PUT')
    expect(decodeURIComponent(sent[0]!.url)).toBe(`/v1/workspace/secrets/admin-access/grants/${BROWSER}`)
    // MUTATION: drop `hosts` or `slot`, or send a value, its fields or a tenant -> fails.
    expect(sent[0]!.body).toEqual({ enabled: true, slot: CF, hosts: ['api.admin.example.com', 'console.example.com'] })
    expect(Object.keys(sent[0]!.body as Json).sort()).toEqual(['enabled', 'hosts', 'slot'])
  })

  it('a tick with no host is refused before the call, in the API\'s words; a host then lets it through', async () => {
    const calls = api(listBody([record('admin-access', [])]), (m) =>
      m === 'PUT' ? { status: 200, body: { secret: record('admin-access', [grant(BROWSER, true, CF, ['admin.example.com'])]), takes_effect: 'at the next attempt' } } : null)
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={null} />)
    const m = await matrix()
    await act(async () => {
      fireEvent.click(cfBox(m, 'admin-access'))
    })
    // MUTATION: send the tick without hosts and let the API refuse it -> the call count fails.
    expect(writes(calls)).toHaveLength(0)
    // MUTATION: refuse in other words than `slot_hosts`' -> fails.
    expect(within(cfCell(m, 'admin-access')).getByRole('alert').textContent).toBe(hostsNeeded(CF))
    expect(cfBox(m, 'admin-access').checked).toBe(false)
    await addHost(cfCell(m, 'admin-access'), 'admin-access', 'admin.example.com')
    await act(async () => {
      fireEvent.click(cfBox(m, 'admin-access'))
    })
    await screen.findByText(/takes effect at the next attempt/i)
    // MUTATION: tick without the drafted hosts -> fails.
    expect(writes(calls).map((c) => c.body)).toEqual([{ enabled: true, slot: CF, hosts: ['admin.example.com'] }])
  })

  it('emptying an enabled grant\'s list says the refusal and Save sends nothing', async () => {
    const calls = api(listBody([record('admin-access', [grant(BROWSER, true, CF, TWO)])]), () => ({ status: 200, body: {} }))
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={null} />)
    const m = await matrix()
    for (const h of TWO) {
      await act(async () => {
        fireEvent.click(within(cfCell(m, 'admin-access')).getByRole('button', { name: `Remove ${h} from ${what('admin-access')}` }))
      })
    }
    expect(listed(cfCell(m, 'admin-access'))).toEqual([])
    // MUTATION: wait for Save to say it -> fails.
    expect(within(cfCell(m, 'admin-access')).getByRole('alert').textContent).toBe(hostsNeeded(CF))
    await act(async () => {
      fireEvent.click(within(cfCell(m, 'admin-access')).getByRole('button', { name: 'Save hosts' }))
    })
    // MUTATION: send `hosts: []` and let the API refuse -> fails.
    expect(writes(calls)).toHaveLength(0)
  })

  it('a 17th host is refused, and the list stays at 16', async () => {
    const sixteen = Array.from({ length: MAX_SLOT_HOSTS }, (_, i) => `h${String(i).padStart(2, '0')}.example.com`)
    api(listBody([record('admin-access', [grant(BROWSER, true, CF, sixteen)])]))
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={null} />)
    const m = await matrix()
    await addHost(cfCell(m, 'admin-access'), 'admin-access', 'one-more.example.com')
    // MUTATION: drop the cap -> fails.
    expect(within(cfCell(m, 'admin-access')).getByRole('alert').textContent).toBe(HOSTS_TOO_MANY)
    expect(listed(cfCell(m, 'admin-access'))).toEqual(sixteen)
  })

  it('refuses a host the API refuses: a scheme, a port, a path, a wildcard, an IP, capitals, no dot', async () => {
    api(listBody([record('admin-access', [grant(BROWSER, true, CF, TWO)])]))
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={null} />)
    const m = await matrix()
    for (const bad of ['https://admin.example.com', 'admin.example.com:443', 'admin.example.com/x', '*.example.com', '10.0.0.1', 'Admin.example.com', 'localhost']) {
      await addHost(cfCell(m, 'admin-access'), 'admin-access', bad)
      // MUTATION: accept any non-empty text -> fails.
      expect(within(cfCell(m, 'admin-access')).getByRole('alert').textContent, bad).toBe(HOST_REFUSED)
      expect(listed(cfCell(m, 'admin-access')), bad).toEqual(TWO)
    }
  })

  it('a reader who may not manage sees the hosts and no field', async () => {
    api(listBody([record('admin-access', [grant(BROWSER, true, CF, TWO)])], false))
    const { SecretsScreen } = await load()
    render(<SecretsScreen view={null} />)
    const m = await matrix()
    expect(listed(cfCell(m, 'admin-access'))).toEqual(TWO)
    // MUTATION: offer the field to a reader -> fails.
    expect(within(cfCell(m, 'admin-access')).queryByRole('textbox')).toBeNull()
    expect(within(cfCell(m, 'admin-access')).queryByRole('button')).toBeNull()
  })
})

describe('the form\'s rule is the API\'s', () => {
  const py = readFileSync(join(__dirname, '..', '..', '..', 'swarm-api', 'swarm_api', 'workspace_secrets.py'), 'utf8')
  // Adjacent string literals joined, as Python joins them.
  const joined = py.replace(/"\s*\n\s*f?"/g, '')

  it('SLOT_HOST, the cap and the host-scoped slots match swarm_api/workspace_secrets.py', () => {
    const pattern = /^SLOT_HOST = re\.compile\(r"(.+)"\)$/m.exec(py)
    expect(pattern, 'SLOT_HOST not found').not.toBeNull()
    // MUTATION: loosen or tighten either side's host rule -> fails.
    expect(SLOT_HOST.source).toBe(`^${pattern![1]}$`)
    expect(py).toMatch(new RegExp(`^MAX_SLOT_HOSTS = ${MAX_SLOT_HOSTS}$`, 'm'))
    expect(py).toMatch(/^HOST_SCOPED_SLOTS: tuple\[str, \.\.\.\] = \("cf-access",\)$/m)
  })

  it('every refusal is the API\'s words', () => {
    expect(joined).toContain(HOST_REFUSED)
    expect(joined).toContain(HOSTS_TOO_MANY.replace(String(MAX_SLOT_HOSTS), '{MAX_SLOT_HOSTS}'))
    expect(joined).toContain(hostsNeeded('{slot}').replace(`1 to ${MAX_SLOT_HOSTS}`, '1 to {MAX_SLOT_HOSTS}'))
  })
})

describe('the hosts cell wraps inside its column', () => {
  const css = readFileSync(join(__dirname, '..', 'styles', 'secrets.css'), 'utf8')
  const rule = (sel: string): string => {
    const m = new RegExp(`(?:^|\\n)${sel.replace(/[.*+?^${}()|[\]\\>]/g, '\\$&')}\\s*\\{([^}]*)\\}`).exec(css)
    expect(m, `${sel} not found`).not.toBeNull()
    return m![1]
  }

  it('resets the matrix\'s nowrap and caps its width, so a refusal wraps at phone-390', () => {
    // The matrix's cells inherit `white-space: nowrap`; without the reset the
    // API's refusal drew as one 624px line in a 326px viewport (review of
    // CF-ACCESS-HOSTS-UI, dark-phone-390-02). MUTATION: drop either -> fails.
    const hosts = rule('.sec-hosts')
    expect(hosts).toMatch(/white-space:\s*normal/)
    expect(hosts).toMatch(/max-width:\s*\d+px/)
    expect(rule('.sec-matrix > tbody > tr > td.sec-hosted')).toMatch(/white-space:\s*normal/)
  })
})
