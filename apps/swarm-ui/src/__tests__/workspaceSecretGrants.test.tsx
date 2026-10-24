// SETTINGS › WORKSPACE › SECRETS, THE SECOND HALF (lane SEC-UI-GRANTS, part
// of #1347; docs/design/workspace-secrets.md §5, §2.3-2.4, §6.2). The grant
// matrix and Rotate, held to:
//
//   * a cell whose slot refuses the secret's kind is disabled and says why;
//   * a tick writes exactly one grant, for exactly that consumer, through
//     `PUT .../{name}/grants/{consumer}` with `{enabled}`, and the page says
//     it takes effect at the next attempt;
//   * a refused write puts the box back and says the API's reason;
//   * `component:git` is a cell on a forge row only, and is locked there;
//   * after Rotate the new value is in no element, attribute, field, URL or
//     storage, and the row shows the new version;
//   * a person who may not manage sees every box disabled and no Rotate.
//
// Fixtures only; no network. Every value is built at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

/** A credential-shaped value, built at runtime: it must reach the PUT body and nothing else. */
const fakeValue = () => ['ws', 'rot'].join('-') + 'Z7k'.repeat(12)

const AT = '2026-10-20T09:30:00+00:00'

type Json = Record<string, unknown>

function grant(consumer: string, enabled: boolean, slot = 'files'): Json {
  return { consumer, slot, enabled, changed_at: AT, changed_by: 'alice@example.com' }
}

function record(name: string, over: Json = {}): Json {
  return {
    name,
    secret_id: `swarm-tenant-eng-wsec-${name.length}c3d4`,
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
    grants: [grant('tool:jev-browser', true, 'typesafe')],
    ...over,
  }
}

function listBody(opts: { canManage?: boolean; secrets?: Json[] } = {}): Json {
  return {
    tenant_id: 'eng',
    can_manage: opts.canManage ?? true,
    secrets: opts.secrets ?? [record('typesafe-key')],
    platform_credentials: { runner_provider_keys: [], pool_accounts: { readable: true, accounts: [] } },
    note: 'stored write-only',
  }
}

const FILES = ['api_key', 'bearer_token', 'basic_auth', 'oauth_client', 'opaque']
const NO_SLOT = 'declares no secret slot yet; a grant would deliver nothing'

/** The catalogue as `Consumer.to_api` serves it (swarm_api/workspace_secrets.py). */
const CONSUMERS: Json = {
  consumers: [
    { consumer: 'profile:claude-code', kind: 'profile', name: 'claude-code', slots: [{ slot: 'files', kinds: FILES, means: 'one file' }], grantable: true, note: '' },
    { consumer: 'profile:claude-code-browser', kind: 'profile', name: 'claude-code-browser', slots: [{ slot: 'files', kinds: FILES, means: 'one file' }], grantable: true, note: '' },
    { consumer: 'tool:jev-browser', kind: 'tool', name: 'jev-browser', slots: [{ slot: 'typesafe', kinds: ['api_key'], means: 'the key' }], grantable: true, note: '' },
    { consumer: 'tool:dev-browser', kind: 'tool', name: 'dev-browser', slots: [], grantable: false, note: NO_SLOT },
    { consumer: 'component:judge', kind: 'component', name: 'judge', slots: [{ slot: 'typesafe', kinds: ['api_key'], means: 'the TypeSafe key' }], grantable: true, note: '' },
    { consumer: 'component:merge', kind: 'component', name: 'merge', slots: [], grantable: false, note: NO_SLOT },
    { consumer: 'component:git', kind: 'component', name: 'git', slots: [{ slot: 'forge', kinds: ['forge_token'], means: 'the forge token' }], grantable: false, note: 'forge tokens only, never granted from this page (design §6.2)' },
  ],
  kinds: FILES,
  providers: ['anthropic', 'brave', 'openai', 'typesafe', 'other'],
}

type Reply = { status: number; body: unknown } | null

function api(opts: { list?: Json; write?: (m: string, url: string, body: unknown) => Reply } = {}) {
  let list = opts.list ?? listBody()
  const calls = serve((m, url, body) => {
    if (m === 'GET' && url.startsWith('/v1/workspace/secrets/consumers')) return { status: 200, body: CONSUMERS }
    if (m === 'GET' && url.startsWith('/v1/workspace/secrets')) return { status: 200, body: list }
    if (m !== 'GET' && opts.write) return opts.write(m, decodeURIComponent(url), body)
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

const matrix = () => screen.findByRole('table', { name: /secret grants/i })
const box = (m: HTMLElement, secret: string, consumer: string) =>
  within(m).getByRole('checkbox', { name: `Grant ${secret} to ${consumer}` }) as HTMLInputElement
const writes = (calls: { method: string; url: string; body: unknown }[]) => calls.filter((c) => c.method !== 'GET')

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

describe('the grant matrix', () => {
  it('draws the catalogue\'s consumers grouped Profiles · Tools · Components, each cell a box', async () => {
    api()
    const { WorkspaceSecretsScreen } = await load()
    render(<WorkspaceSecretsScreen link={null} />)
    const m = await matrix()
    const heads = within(m).getAllByRole('columnheader').map((h) => visible(h))
    // MUTATION: draw the columns ungrouped (drop the group header row) -> fails.
    expect(heads.indexOf('Profiles')).toBeGreaterThanOrEqual(0)
    expect(heads.indexOf('Profiles')).toBeLessThan(heads.indexOf('Tools'))
    expect(heads.indexOf('Tools')).toBeLessThan(heads.indexOf('Components'))
    // MUTATION: draw a box from the record's grants rather than its served state -> one of these fails.
    expect(box(m, 'typesafe-key', 'tool:jev-browser').checked).toBe(true)
    expect(box(m, 'typesafe-key', 'profile:claude-code').checked).toBe(false)
    expect(box(m, 'typesafe-key', 'profile:claude-code').disabled).toBe(false)
  })

  it('disables a cell whose slot refuses the secret\'s kind, and shows the reason', async () => {
    api({ list: listBody({ secrets: [record('search-token', { kind: 'bearer_token', grants: [] })] }) })
    const { WorkspaceSecretsScreen } = await load()
    render(<WorkspaceSecretsScreen link={null} />)
    const m = await matrix()
    const jev = box(m, 'search-token', 'tool:jev-browser')
    // MUTATION: drop the kind check (enable every grantable consumer) -> fails.
    expect(jev.disabled).toBe(true)
    const cell = jev.closest('td')!
    // MUTATION: disable the box without saying why -> fails.
    expect(visible(cell)).toMatch(/jev-browser reads an API key/)
    // A profile's `files` slot takes any non-forge kind.
    // MUTATION: disable every non-api_key cell -> fails.
    expect(box(m, 'search-token', 'profile:claude-code').disabled).toBe(false)
    // A consumer with no slot says so too.
    const dev = box(m, 'search-token', 'tool:dev-browser')
    // MUTATION: offer a consumer with no slot -> fails.
    expect(dev.disabled).toBe(true)
    expect(visible(dev.closest('td')!)).toMatch(/dev-browser reads no secret yet/)
  })

  it('a tick writes one grant, for exactly that consumer, with {enabled: true}, and says when it takes effect', async () => {
    const { calls, setList } = api({
      write: (m, url) => (m === 'PUT' && url === '/v1/workspace/secrets/typesafe-key/grants/component:judge'
        ? { status: 200, body: { secret: record('typesafe-key', { grants: [grant('tool:jev-browser', true, 'typesafe'), grant('component:judge', true, 'typesafe')] }), takes_effect: 'at the next attempt' } }
        : null),
    })
    const { WorkspaceSecretsScreen } = await load()
    render(<WorkspaceSecretsScreen link={null} />)
    const m = await matrix()
    setList(listBody({ secrets: [record('typesafe-key', { grants: [grant('tool:jev-browser', true, 'typesafe'), grant('component:judge', true, 'typesafe')] })] }))
    await act(async () => {
      fireEvent.click(box(m, 'typesafe-key', 'component:judge'))
    })
    const status = await screen.findByText(/takes effect at the next attempt/i)
    // MUTATION: drop the note -> fails.
    expect(visible(status)).toMatch(/component:judge/)
    const sent = writes(calls)
    // MUTATION: write the whole row (one PUT per consumer) or use `use-in` -> fails.
    expect(sent).toHaveLength(1)
    expect(sent[0]!.method).toBe('PUT')
    // MUTATION: name a different consumer, or the secret's id rather than its name -> fails.
    expect(decodeURIComponent(sent[0]!.url)).toBe('/v1/workspace/secrets/typesafe-key/grants/component:judge')
    // MUTATION: send a slot, a tenant or a list -> fails.
    expect(sent[0]!.body).toEqual({ enabled: true })
    await waitFor(() => expect(box(screen.getByRole('table', { name: /secret grants/i }), 'typesafe-key', 'component:judge').checked).toBe(true))
  })

  it('an untick sends {enabled: false}', async () => {
    const off = record('typesafe-key', { grants: [grant('tool:jev-browser', false, 'typesafe')] })
    const { calls, setList } = api({
      write: (m, url) => (m === 'PUT' && url.endsWith('/grants/tool:jev-browser')
        ? { status: 200, body: { secret: off, takes_effect: 'at the next attempt' } }
        : null),
    })
    const { WorkspaceSecretsScreen } = await load()
    render(<WorkspaceSecretsScreen link={null} />)
    const m = await matrix()
    // What the API serves once the write has landed.
    setList(listBody({ secrets: [off] }))
    await act(async () => {
      fireEvent.click(box(m, 'typesafe-key', 'tool:jev-browser'))
    })
    await screen.findByText(/takes effect at the next attempt/i)
    // MUTATION: always send `enabled: true` -> fails.
    expect(writes(calls).map((c) => c.body)).toEqual([{ enabled: false }])
    expect(box(m, 'typesafe-key', 'tool:jev-browser').checked).toBe(false)
  })

  it('a refused write puts the box back and says why', async () => {
    api({
      write: (m) => (m === 'PUT' ? { status: 422, body: { code: 'validation_failed', message: 'component:judge reads api_key in its typesafe slot, not a opaque' } } : null),
    })
    const { WorkspaceSecretsScreen } = await load()
    render(<WorkspaceSecretsScreen link={null} />)
    const m = await matrix()
    await act(async () => {
      fireEvent.click(box(m, 'typesafe-key', 'component:judge'))
    })
    const alert = await screen.findByRole('alert')
    // MUTATION: drop the reason from the refusal -> fails.
    expect(visible(alert)).toMatch(/reads api_key in its typesafe slot/)
    // MUTATION: keep the optimistic tick after a refusal -> fails.
    expect(box(m, 'typesafe-key', 'component:judge').checked).toBe(false)
    // MUTATION: say it took effect after a refusal -> fails.
    expect(screen.queryByText(/takes effect at the next attempt/i)).toBeNull()
  })

  it('component:git is a cell on a forge row only, and is locked there', async () => {
    api({
      list: listBody({
        secrets: [
          record('typesafe-key'),
          record('eng-git', { provider: 'github', kind: 'forge_token', managed: 'legacy', grants: [grant('component:git', true, 'forge')] }),
        ],
      }),
    })
    const { WorkspaceSecretsScreen } = await load()
    render(<WorkspaceSecretsScreen link={null} />)
    const m = await matrix()
    // MUTATION: draw the git box on every row -> fails.
    expect(within(m).queryByRole('checkbox', { name: 'Grant typesafe-key to component:git' })).toBeNull()
    const git = box(m, 'eng-git', 'component:git')
    // MUTATION: drop the git cell from the forge row -> getByRole throws.
    // MUTATION: make the git cell grantable -> fails.
    expect(git.disabled).toBe(true)
    expect(git.checked).toBe(true)
    // A forge token is never granted to an agent's profile or tool (§6.2).
    // MUTATION: offer a profile a forge token -> fails.
    expect(box(m, 'eng-git', 'profile:claude-code').disabled).toBe(true)
    // §6.2: no Rotate and no Delete on a forge row.
    // MUTATION: offer Rotate or Delete on a forge row -> fails.
    expect(screen.queryByRole('button', { name: /rotate eng-git/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /delete eng-git/i })).toBeNull()
  })
})

describe('Rotate', () => {
  it('posts the new value once to PUT .../value, then leaves it nowhere, and the row shows the new version', async () => {
    const value = fakeValue()
    const { calls, setList } = api({
      write: (m, url) => (m === 'PUT' && url === '/v1/workspace/secrets/typesafe-key/value'
        ? { status: 200, body: { secret: record('typesafe-key', { current_version: '3', rotated_at: AT, rotated_by: 'alice@example.com' }), via_link: false, note: 'n' } }
        : null),
    })
    const { WorkspaceSecretsScreen } = await load()
    render(<WorkspaceSecretsScreen link={null} />)
    await matrix()
    fireEvent.click(screen.getByRole('button', { name: /rotate typesafe-key/i }))
    const form = screen.getByRole('form', { name: /rotate typesafe-key/i })
    const field = within(form).getByLabelText(/new value/i) as HTMLInputElement
    // MUTATION: make the field type="text" -> fails.
    expect(field.type).toBe('password')
    fireEvent.change(field, { target: { value } })
    setList(listBody({ secrets: [record('typesafe-key', { current_version: '3', rotated_at: AT, rotated_by: 'alice@example.com' })] }))
    await act(async () => {
      fireEvent.click(within(form).getByRole('button', { name: /^rotate$/i }))
    })
    await screen.findByText(/typesafe-key was rotated/i)

    const carrying = calls.filter((c) => c.url.includes(value) || JSON.stringify(c.body ?? null).includes(value))
    // MUTATION: send the value twice, or in the URL -> fails.
    expect(carrying).toHaveLength(1)
    expect(carrying[0]!.method).toBe('PUT')
    expect(carrying[0]!.body).toEqual({ value })
    // MUTATION: keep the value in the field, state drawn as an attribute, or a "rotated to <value>" line -> fails.
    expect(leaks(value)).toEqual([])
    const list = screen.getByRole('table', { name: /workspace secrets/i })
    const row = within(list).getByRole('row', { name: /typesafe-key/i })
    // MUTATION: never read the list again after a rotate -> the row still says 2 and this fails.
    await waitFor(() => expect(visible(within(row).getByText('3', { exact: true }))).toBe('3'))
  })

  it('empties the field on a refusal too, and says the API\'s reason', async () => {
    const value = fakeValue()
    api({ write: (m) => (m === 'PUT' ? { status: 422, body: { code: 'validation_failed', message: 'an api_key is 8 bytes to 8 KiB' } } : null) })
    const { WorkspaceSecretsScreen } = await load()
    render(<WorkspaceSecretsScreen link={null} />)
    await matrix()
    fireEvent.click(screen.getByRole('button', { name: /rotate typesafe-key/i }))
    const form = screen.getByRole('form', { name: /rotate typesafe-key/i })
    fireEvent.change(within(form).getByLabelText(/new value/i), { target: { value } })
    await act(async () => {
      fireEvent.click(within(form).getByRole('button', { name: /^rotate$/i }))
    })
    const alert = await screen.findByRole('alert')
    expect(visible(alert)).toMatch(/8 bytes to 8 KiB/)
    // MUTATION: clear the field only on success -> fails.
    expect(leaks(value)).toEqual([])
  })
})

describe('the one-time rotate link: ?action=rotate&name=..&t=..', () => {
  it('opens Rotate for the name it names, and sends its token once, with that rotate only', async () => {
    const token = ['lnk', 'rot'].join('') + 'Wq4'.repeat(10)
    const { calls } = api({
      write: (m, url) => (m === 'PUT' && url.endsWith('/value')
        ? { status: 200, body: { secret: record('typesafe-key', { current_version: '3' }), via_link: true, note: 'n' } }
        : null),
    })
    const { WorkspaceSecretsScreen } = await load()
    render(<WorkspaceSecretsScreen link={{ action: 'rotate', name: 'typesafe-key', token }} />)
    await matrix()
    // MUTATION: ignore the link's name (open no form) -> getByRole throws.
    const form = screen.getByRole('form', { name: /rotate typesafe-key/i })
    // MUTATION: render the token anywhere -> fails.
    expect(leaks(token)).toEqual([])
    for (let i = 0; i < 2; i++) {
      const f = i === 0 ? form : (fireEvent.click(screen.getByRole('button', { name: /rotate typesafe-key/i })), screen.getByRole('form', { name: /rotate typesafe-key/i }))
      fireEvent.change(within(f).getByLabelText(/new value/i), { target: { value: fakeValue() } })
      await act(async () => {
        fireEvent.click(within(f).getByRole('button', { name: /^rotate$/i }))
      })
      await waitFor(() => expect(writes(calls)).toHaveLength(i + 1))
    }
    const puts = writes(calls)
    // MUTATION: never send the rotate link's token -> fails.
    expect((puts[0]!.body as Json).link_token).toBe(token)
    // MUTATION: keep the token after it is sent -> the second rotate carries it and this fails.
    expect('link_token' in (puts[1]!.body as Json)).toBe(false)
  })
})

describe('who may manage', () => {
  it('a non-manager sees every box disabled and no Rotate', async () => {
    api({ list: listBody({ canManage: false }) })
    const { WorkspaceSecretsScreen } = await load()
    render(<WorkspaceSecretsScreen link={null} />)
    const m = await matrix()
    const boxes = within(m).getAllByRole('checkbox') as HTMLInputElement[]
    expect(boxes.length).toBeGreaterThan(0)
    // MUTATION: enable grantable boxes whatever `can_manage` says -> fails.
    expect(boxes.filter((b) => !b.disabled).map((b) => b.getAttribute('aria-label'))).toEqual([])
    // MUTATION: draw Rotate whatever `can_manage` says -> fails.
    expect(screen.queryByRole('button', { name: /rotate/i })).toBeNull()
    expect(document.querySelector('input[type="password"]')).toBeNull()
  })
})
