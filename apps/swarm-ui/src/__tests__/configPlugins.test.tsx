// CONFIG › PLUGINS (lane 10c of docs/design/managed-plugins.md, part of
// #2688). The owner picked the cards direction on 6 November 2026: the
// workspace's catalogue as a card grid, a full Add plugin page with the pinned
// manifest's preview, and a page per plugin with its tabs.
//
// What these hold: the Allowed list is saved with each plugin's pinned copy
// (a built-in by name, an install by its id); Every step is its own signed
// list; the settings form is generated from the pinned version's fields, one
// control per type; a secret is drawn by its status only and set through a
// one-time link; a secret may be bound to a workspace key; a workspace's
// owner installs a private plugin; every refusal is drawn in the API's words.
//
// MUTATIONS, each of which turns a case red: draw a switch live for a member;
// PUT anything but `{allowed}` or `{defaults}`; keep a plugin on for every
// step after it is no longer allowed; draw a secret's served value; send the
// value without its link's token; save a form without its version and
// revision; draw a refusal in words of our own.
//
// Fixtures only; no network. Every token-shaped value is built at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { leakedValue, serve, tokenShapedIn, visible } from './repofixture'

type Json = Record<string, unknown>
type Write = (m: string, url: string, body: unknown) => { status: number; body: unknown } | null

const TENANT = 'team-a'
const BASE = `/v1/workspaces/${TENANT}`
const PLATFORM_ID = 'p-0a1b2c3d4e5f'
const OWN_ID = 'w-1a2b3c4d5e6f'
const OWNER = 'owner@example.com'

/** A one-time link's token, built at runtime: nothing here is a literal credential. */
function linkToken(): string {
  return Array.from({ length: 4 }, () => Math.random().toString(16).slice(2, 10)).join('')
}

function selected(over: Json = {}): Json {
  return {
    tenant_id: TENANT,
    plugins: { allowed: ['gstack'] },
    defaults: { plugins: { allowed: [] } },
    set_at: '2026-11-03T09:30:00+00:00',
    set_by: OWNER,
    can_edit: true,
    catalogue: [],
    ...over,
  }
}

function catalogue(over: Json = {}): Json {
  return {
    scope: 'workspace',
    tenant_id: TENANT,
    built_in: [
      { install_id: 'builtin:gstack', name: 'gstack', version: '1.92.55.0', runner_profiles: ['claude-code'], skills: { unattended: ['review', 'investigate'] } },
    ],
    platform_installs: [
      {
        install_id: PLATFORM_ID, name: 'lint-kit', version: '2.4.0', description: 'Linters for every step',
        source: { kind: 'github', repository: 'example-org/lint-kit', commit: 'c0ffee'.padEnd(40, '1') },
        components: { skills: 3, commands: 0, agents: 0, hooks: 1, mcp_servers: 0 },
      },
    ],
    installs: [
      {
        install_id: OWN_ID, name: 'docs-helper', version: '0.3.1',
        source: { kind: 'github', repository: 'example-org/example-repo' },
        components: { skills: 1, commands: 0, agents: 0, hooks: 0, mcp_servers: 0 },
        updated_by: OWNER, updated_at: '2026-11-05T10:00:00+00:00',
      },
    ],
    can_install: true,
    ...over,
  }
}

function defaults(list: string[] = [], canEdit = true): Json {
  return { tenant_id: TENANT, defaults: list, can_edit: canEdit }
}

function field(over: Json): Json {
  return { source: 'user_config', type: 'string', kind: 'value', title: null, description: null, required: false, multiple: false, default: null, target: 'env', variable: null, bindable: [], ...over }
}

function config(over: Json = {}, values: Json = {}): Json {
  return {
    tenant_id: TENANT,
    form: {
      install_id: OWN_ID,
      version_id: 'v-000001',
      fields: [
        field({ key: 'style', title: 'Style', required: true, description: 'The house style the helper writes in' }),
        field({ key: 'max_pages', title: 'Max pages', type: 'number', default: 20 }),
        field({ key: 'strict', title: 'Strict', type: 'boolean', default: false }),
        field({ key: 'docs_dir', title: 'Docs folder', type: 'directory', kind: 'path' }),
        field({ key: 'topics', title: 'Topics', multiple: true }),
        field({ key: 'api_key', title: 'API key', kind: 'secret', required: true, bindable: ['typesafe'] }),
        field({ key: 'ca_bundle', title: 'CA bundle', type: 'file', kind: 'file' }),
      ],
    },
    revision: 4,
    values: {
      style: { set: true, value: 'plain' },
      max_pages: { set: true, value: 12 },
      strict: { set: true, value: true },
      topics: { set: true, value: ['api', 'cli'] },
      api_key: { set: false },
      ca_bundle: { set: false },
      ...values,
    },
    missing: ['api_key'],
    set_at: '2026-11-06T08:00:00+00:00',
    set_by: OWNER,
    can_edit: true,
    ...over,
  }
}

function version(over: Json = {}): Json {
  return {
    version_id: 'v-000001', current: true, version: '0.3.1',
    manifest: { version: '0.3.1', license: 'MIT', description: 'Writes the docs', user_config: [{ key: 'style' }] },
    source: { kind: 'github', repository: 'example-org/example-repo', commit: 'abc123'.padEnd(40, '0') },
    runnable_sha256: 'sha256:' + 'd'.repeat(64),
    components: {
      skills: [{ name: 'write-docs' }],
      hooks: [{ event: 'PostToolUse', matcher: 'Edit', command: 'lint-docs' }],
      mcp_servers: [], commands: [], agents: [],
    },
    requires: { runtimes: [{ name: 'node', min_version: '20' }], profiles: ['claude-code'] },
    hosts: [],
    ...over,
  }
}

/** The page's reads, and the writes `write` answers (anything else is a 404). */
function api(opts: { plugins?: Json; catalogue?: Json; defaults?: Json; config?: Json; versions?: Json[]; write?: Write } = {}) {
  return serve((m, url, b) => {
    if (m === 'GET') {
      if (url.startsWith('/v1/plugins')) return { status: 200, body: opts.plugins ?? selected() }
      if (url === `${BASE}/plugin-catalogue`) return { status: 200, body: opts.catalogue ?? catalogue() }
      if (url === `${BASE}/plugin-defaults`) return { status: 200, body: opts.defaults ?? defaults() }
      if (url.startsWith(`${BASE}/plugin-config/`)) return { status: 200, body: opts.config ?? config() }
      if (url.endsWith('/versions')) return { status: 200, body: { versions: opts.versions ?? [version()] } }
    }
    return opts.write ? opts.write(m, url, b) : null
  })
}

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../ConfigPlugins')
}

/**
 * A card's plugin page, as its link opens it: the card links
 * `/config/plugins/<install_id>`, App's `WorkspacePluginRoute`, and that
 * page's Settings tab is the editable form.
 */
async function openSettings(name = 'docs-helper', installId = OWN_ID) {
  const card = await screen.findByRole('link', { name: `Open ${name}` })
  // MUTATION: open the screen's own page from the card again -> no link, and this fails.
  expect(card.getAttribute('href')).toBe(`/config/plugins/${installId}`)
  cleanup()
  const { WorkspacePluginRoute } = await import('../App')
  render(<WorkspacePluginRoute installId={installId} />)
  fireEvent.click(await screen.findByRole('tab', { name: 'Settings' }))
  return screen.findByRole('list', { name: `${name}'s settings` }, { timeout: 5000 })
}

function row(form: HTMLElement, title: string): HTMLElement {
  return within(form).getAllByRole('listitem').find((li) => visible(li).startsWith(title))!
}

afterEach(() => {
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

describe('Config › Plugins: the catalogue', () => {
  it('draws a card per plugin with its pinned version and where it comes from', async () => {
    api()
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    const grid = await screen.findByRole('list', { name: 'The plugin catalogue' })
    const cards = within(grid).getAllByRole('listitem')
    // A built-in opens this screen's Overview; an install's card links its routed page.
    // MUTATION: draw every card a button again -> lint-kit and docs-helper have no link and this fails.
    expect(within(cards[0]!).getByRole('button', { name: 'Open gstack' })).toBeTruthy()
    expect(cards.slice(1).map((c) => within(c).getByRole('link', { name: /^Open / }).getAttribute('href'))).toEqual([`/config/plugins/${PLATFORM_ID}`, `/config/plugins/${OWN_ID}`])
    expect(visible(cards[0]!)).toMatch(/gstack\s*1\.92\.55\.0\s*Built in/)
    expect(visible(cards[1]!)).toMatch(/lint-kit\s*2\.4\.0\s*Platform/)
    expect(visible(cards[1]!)).toContain('example-org/lint-kit')
    expect(visible(cards[2]!)).toMatch(/docs-helper\s*0\.3\.1\s*This workspace/)
    // A workspace install is allowed by being installed: its switch is on and inert.
    const own = within(cards[2]!).getByRole('switch', { name: 'docs-helper: allowed in this workspace' }) as HTMLInputElement
    expect(own.checked).toBe(true)
    expect(own.disabled).toBe(true)
    expect(screen.getByRole('button', { name: 'Add plugin' })).toBeTruthy()
  })

  it('saves the Allowed list with each plugin’s pinned copy: a built-in by name, an install by its id', async () => {
    const seen: Array<{ m: string; url: string; body: unknown }> = []
    const stored = selected()
    api({
      plugins: stored,
      write: (m, url, body) => {
        seen.push({ m, url, body })
        // What the API signed is what the page reads back.
        stored.plugins = { allowed: ['gstack', PLATFORM_ID] }
        return { status: 200, body: stored }
      },
    })
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    fireEvent.click(await screen.findByRole('switch', { name: 'lint-kit: allowed in this workspace' }))
    const bar = screen.getByRole('region', { name: 'Changes not saved' })
    expect(visible(bar)).toContain('lint-kit')
    fireEvent.click(within(bar).getByRole('button', { name: 'Save and sign' }))
    await waitFor(() => expect(seen).toHaveLength(1))
    expect(seen[0]).toEqual({ m: 'PUT', url: `${BASE}/plugins`, body: { allowed: ['gstack', PLATFORM_ID].sort() } })
    await screen.findByText('Saved and signed')
    // The id names one install, so the list allows its pinned copy: the card says which.
    const card = screen.getByRole('link', { name: 'Open lint-kit' }).closest('li')!
    expect(visible(card)).toMatch(/lint-kit\s*2\.4\.0/)
    expect((within(card).getByRole('switch', { name: 'lint-kit: allowed in this workspace' }) as HTMLInputElement).checked).toBe(true)
  })

  it('saves Every step as its own list, and a plugin no longer allowed leaves it', async () => {
    const seen: Array<{ m: string; url: string; body: unknown }> = []
    api({
      defaults: defaults(['gstack']),
      write: (m, url, body) => {
        seen.push({ m, url, body })
        if (url.endsWith('/plugin-defaults')) return { status: 200, body: defaults((body as { defaults: string[] }).defaults) }
        return { status: 200, body: selected({ plugins: { allowed: (body as { allowed: string[] }).allowed } }) }
      },
    })
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    const every = (await screen.findByRole('switch', { name: 'gstack: on for every step' })) as HTMLInputElement
    expect(every.checked).toBe(true)
    // Not allowed, so not offered for every step.
    expect((screen.getByRole('switch', { name: 'lint-kit: on for every step' }) as HTMLInputElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole('switch', { name: 'docs-helper: on for every step' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save and sign' }))
    await waitFor(() => expect(seen).toHaveLength(1))
    expect(seen[0]).toEqual({ m: 'PUT', url: `${BASE}/plugin-defaults`, body: { defaults: [OWN_ID, 'gstack'].sort() } })
  })

  it('takes a plugin off Every step when it is no longer allowed, and says steps naming it are refused', async () => {
    const seen: Array<{ m: string; url: string; body: unknown }> = []
    api({
      defaults: defaults(['gstack']),
      write: (m, url, body) => {
        seen.push({ m, url, body })
        return { status: 200, body: url.endsWith('/plugin-defaults') ? defaults([]) : selected({ plugins: { allowed: [] } }) }
      },
    })
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    fireEvent.click(await screen.findByRole('switch', { name: 'gstack: allowed in this workspace' }))
    expect((screen.getByRole('switch', { name: 'gstack: on for every step' }) as HTMLInputElement).checked).toBe(false)
    const bar = screen.getByRole('region', { name: 'Changes not saved' })
    expect(visible(bar)).toContain('Refused from the next submission')
    fireEvent.click(within(bar).getByRole('button', { name: 'Save and sign' }))
    await waitFor(() => expect(seen).toHaveLength(2))
    expect(seen[0]).toEqual({ m: 'PUT', url: `${BASE}/plugins`, body: { allowed: [] } })
    expect(seen[1]).toEqual({ m: 'PUT', url: `${BASE}/plugin-defaults`, body: { defaults: [] } })
  })

  it('is read only for a member: every switch is inert, no save bar and no Add plugin', async () => {
    api({ plugins: selected({ can_edit: false }), catalogue: catalogue({ can_install: false }), defaults: defaults([], false) })
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    await screen.findByRole('list', { name: 'The plugin catalogue' })
    for (const s of screen.getAllByRole('switch')) expect((s as HTMLInputElement).disabled).toBe(true)
    fireEvent.click(screen.getAllByRole('switch')[0]!)
    expect(screen.queryByRole('region', { name: 'Changes not saved' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Add plugin' })).toBeNull()
    expect(visible(document.body)).toContain('View only')
  })

  it('says a save the API stored but could not sign allows nothing', async () => {
    api({ plugins: selected({ plugins: { allowed: [] } }), write: () => ({ status: 200, body: selected({ plugins: { allowed: [] } }) }) })
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    fireEvent.click(await screen.findByRole('switch', { name: 'gstack: allowed in this workspace' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save and sign' }))
    await screen.findByText('Saved, not signed')
    expect(visible(document.body)).toMatch(/gstack: allows nothing/)
  })

  it('draws a refused save in the API’s words and keeps the change on the page', async () => {
    const words = 'Only the workspace’s owner or admin may change its plugins. Nothing was changed.'
    api({ write: () => ({ status: 403, body: { code: 'role_admin_required', message: words } }) })
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    fireEvent.click(await screen.findByRole('switch', { name: 'lint-kit: allowed in this workspace' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save and sign' }))
    const alert = await screen.findByRole('alert')
    expect(visible(alert)).toBe(`Not saved: ${words}`)
    expect(screen.getByRole('region', { name: 'Changes not saved' })).toBeTruthy()
  })
})

describe('Config › Plugins: a plugin’s settings', () => {
  it('generates one control per setting type from the pinned version’s fields', async () => {
    api()
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    const form = await openSettings()
    expect((within(form).getByRole('textbox', { name: 'Style' }) as HTMLInputElement).value).toBe('plain')
    expect(visible(row(form, 'Style'))).toContain('Required')
    expect(within(row(form, 'Style')).getByRole('button', { name: 'About Style' })).toBeTruthy()
    const pages = within(form).getByRole('spinbutton', { name: 'Max pages' }) as HTMLInputElement
    expect(pages.value).toBe('12')
    expect(pages.placeholder).toBe('20')
    expect((within(form).getByRole('switch', { name: 'Strict' }) as HTMLInputElement).checked).toBe(true)
    expect((within(form).getByRole('textbox', { name: 'Docs folder' }) as HTMLInputElement).value).toBe('')
    const topics = within(form).getByRole('textbox', { name: 'Topics' }) as HTMLTextAreaElement
    expect(topics.tagName).toBe('TEXTAREA')
    expect(topics.value).toBe('api\ncli')
    // Secrets and files get no box of their own: only their status.
    expect(within(form).queryByRole('textbox', { name: 'API key' })).toBeNull()
    expect(visible(row(form, 'API key'))).toMatch(/Not set.*Missing.*Secret/)
    expect(visible(row(form, 'CA bundle'))).toMatch(/Not set.*File/)
  })

  it('saves only the changed values, typed, against the version and revision the form was read at', async () => {
    const seen: Array<{ m: string; url: string; body: unknown }> = []
    api({
      write: (m, url, body) => {
        seen.push({ m, url, body })
        return { status: 200, body: config({ revision: 5 }, { max_pages: { set: true, value: 30 }, topics: { set: true, value: ['api', 'sdk'] } }) }
      },
    })
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    const form = await openSettings()
    fireEvent.change(within(form).getByRole('spinbutton', { name: 'Max pages' }), { target: { value: '30' } })
    fireEvent.change(within(form).getByRole('textbox', { name: 'Topics' }), { target: { value: 'api\n\nsdk\n' } })
    fireEvent.click(within(form).getByRole('switch', { name: 'Strict' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(seen).toHaveLength(1))
    expect(seen[0]).toEqual({
      m: 'PUT', url: `${BASE}/plugin-config/${OWN_ID}`,
      body: { version_id: 'v-000001', revision: 4, values: { max_pages: 30, strict: false, topics: ['api', 'sdk'] } },
    })
    await screen.findByText('Saved')
  })

  it('refuses a path outside the checkout before it is sent', async () => {
    const seen: unknown[] = []
    api({ write: (_m, _u, b) => { seen.push(b); return null } })
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    const form = await openSettings()
    fireEvent.change(within(form).getByRole('textbox', { name: 'Docs folder' }), { target: { value: '../outside' } })
    expect(visible(row(form, 'Docs folder'))).toContain('Not a path in the checkout')
    const save = screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement
    expect(save.disabled).toBe(true)
    fireEvent.click(save)
    expect(seen).toHaveLength(0)
  })

  it('draws a stale form’s refusal in the API’s words', async () => {
    const words = 'The plugin was updated since this form was read: read it again. Nothing was saved.'
    api({ write: () => ({ status: 409, body: { code: 'form_changed', message: words } }) })
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    const form = await openSettings()
    fireEvent.change(within(form).getByRole('textbox', { name: 'Style' }), { target: { value: 'terse' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(visible(await screen.findByRole('alert'))).toBe(`Not saved: ${words}`)
  })

  it('shows a set secret by its status only, never a value, even one the answer carried', async () => {
    api({ config: config({}, { api_key: { set: true, value: leakedValue(), set_by: OWNER, set_at: '2026-11-06T08:00:00+00:00' } }) })
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    const form = await openSettings()
    expect(visible(row(form, 'API key'))).toContain(`Set by ${OWNER}`)
    expect(within(row(form, 'API key')).getByRole('button', { name: 'Replace' })).toBeTruthy()
    expect(tokenShapedIn(document.body.outerHTML)).toEqual([])
  })

  it('sets a secret through a one-time link: the token is elided, the box write-only, and the value goes with the token', async () => {
    const t = linkToken()
    const typed = linkToken()
    const seen: Array<{ m: string; url: string; body: unknown }> = []
    api({
      write: (m, url, body) => {
        seen.push({ m, url, body })
        if (url.endsWith('/links')) {
          return { status: 201, body: { field: 'api_key', path: `/config/plugins/${OWN_ID}?field=api_key&t=${t}`, expires_at: '2026-11-08T10:10:00+00:00' } }
        }
        return { status: 200, body: { install_id: OWN_ID, field: 'api_key', set: true, set_by: OWNER, set_at: '2026-11-08T10:01:00+00:00' } }
      },
    })
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    const form = await openSettings()
    fireEvent.click(within(row(form, 'API key')).getByRole('button', { name: 'Set' }))
    const box = await screen.findByRole('group', { name: 'API key: one-time link' })
    expect(seen[0]).toEqual({ m: 'POST', url: `${BASE}/plugin-config/${OWN_ID}/secrets/api_key/links`, body: null })
    expect(visible(box)).not.toContain(t)
    expect(visible(box)).toContain('t=…')
    const input = within(box).getByLabelText('API key: value') as HTMLInputElement
    expect(input.type).toBe('password')
    fireEvent.change(input, { target: { value: typed } })
    fireEvent.click(within(box).getByRole('button', { name: 'Store' }))
    await waitFor(() => expect(seen).toHaveLength(2))
    expect(seen[1]).toEqual({ m: 'PUT', url: `${BASE}/plugin-config/${OWN_ID}/secrets/api_key`, body: { value: typed, link_token: t } })
    await waitFor(() => expect(screen.queryByRole('group', { name: 'API key: one-time link' })).toBeNull())
    expect(document.body.outerHTML).not.toContain(typed)
  })

  it('opens the box straight away for a one-time link opened on this console', async () => {
    const t = linkToken()
    const seen: Array<{ m: string; url: string; body: unknown }> = []
    api({ write: (m, url, body) => { seen.push({ m, url, body }); return { status: 200, body: { set: true } } } })
    const { ConfigPluginsScreen, pluginLinkOf } = await load()
    const link = pluginLinkOf(`/config/plugins/${OWN_ID}`, `?field=ca_bundle&t=${t}`)
    expect(link).toEqual({ install_id: OWN_ID, field: 'ca_bundle', token: t })
    expect(pluginLinkOf('/config/plugins/not-an-id', `?field=ca_bundle&t=${t}`)).toBeNull()
    render(<ConfigPluginsScreen link={link} />)
    const box = await screen.findByRole('group', { name: 'CA bundle: one-time link' })
    fireEvent.change(within(box).getByLabelText('CA bundle: content'), { target: { value: 'pem text' } })
    fireEvent.click(within(box).getByRole('button', { name: 'Store' }))
    await waitFor(() => expect(seen).toHaveLength(1))
    expect(seen[0]).toEqual({ m: 'PUT', url: `${BASE}/plugin-config/${OWN_ID}/secrets/ca_bundle`, body: { value: 'pem text', link_token: t } })
  })

  it('binds a secret to the workspace key, and a bound one says so', async () => {
    const seen: Array<{ m: string; url: string; body: unknown }> = []
    api({ write: (m, url, body) => { seen.push({ m, url, body }); return { status: 200, body: { set: true, binding: { kind: 'workspace_key', provider: 'typesafe' } } } } })
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    const form = await openSettings()
    expect(within(row(form, 'CA bundle')).queryByRole('button', { name: /^Use the / })).toBeNull()
    fireEvent.click(within(row(form, 'API key')).getByRole('button', { name: 'Use the TypeSafe key' }))
    await waitFor(() => expect(seen).toHaveLength(1))
    expect(seen[0]).toEqual({ m: 'PUT', url: `${BASE}/plugin-config/${OWN_ID}/secrets/api_key/binding`, body: { provider: 'typesafe' } })
  })

  it('draws a bound secret by the key it uses, with no button to bind it again', async () => {
    api({ config: config({}, { api_key: { set: true, binding: { kind: 'workspace_key', provider: 'typesafe' } } }) })
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    const form = await openSettings()
    const r = row(form, 'API key')
    expect(visible(r)).toContain('TypeSafe key')
    expect(within(r).queryByRole('button', { name: 'Use the TypeSafe key' })).toBeNull()
    expect(within(r).getByRole('button', { name: 'Set' })).toBeTruthy()
  })

  it('draws a refused link in the API’s words', async () => {
    const words = 'Only the workspace’s owner or admin sets a plugin’s secrets.'
    api({ write: () => ({ status: 403, body: { code: 'role_admin_required', message: words } }) })
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    const form = await openSettings()
    fireEvent.click(within(row(form, 'API key')).getByRole('button', { name: 'Set' }))
    expect(visible(await screen.findByRole('alert'))).toBe(`Refused: ${words}`)
  })

  it('is read only for a member: no control is live and no secret action is offered', async () => {
    api({ config: config({ can_edit: false }) })
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    const form = await openSettings()
    expect((within(form).getByRole('textbox', { name: 'Style' }) as HTMLInputElement).disabled).toBe(true)
    expect(within(form).queryByRole('button', { name: 'Set' })).toBeNull()
    expect(within(form).queryByRole('button', { name: 'Use the TypeSafe key' })).toBeNull()
  })
})

describe('Config › Plugins: a workspace install', () => {
  it('installs a private plugin from GitHub for the owner, checks its intake once and previews the pinned manifest', async () => {
    const seen: Array<{ m: string; url: string; body: unknown }> = []
    let reads = 0
    serve((m, url, body) => {
      if (m === 'GET' && url.startsWith('/v1/plugins')) return { status: 200, body: selected() }
      if (m === 'GET' && url === `${BASE}/plugin-catalogue`) return { status: 200, body: catalogue() }
      if (m === 'GET' && url === `${BASE}/plugin-defaults`) return { status: 200, body: defaults() }
      if (m === 'GET' && url === `${BASE}/plugin-catalogue/intakes/i-0001`) {
        reads += 1
        return { status: 200, body: { intake_id: 'i-0001', state: 'pinned', install_id: OWN_ID, version_id: 'v-000001' } }
      }
      if (m === 'GET' && url.endsWith('/versions')) return { status: 200, body: { versions: [version()] } }
      if (m !== 'GET') {
        seen.push({ m, url, body })
        return { status: 202, body: { intake_id: 'i-0001', state: 'running', install_id: null } }
      }
      return null
    })
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    fireEvent.click(await screen.findByRole('button', { name: 'Add plugin' }))
    const install = screen.getByRole('button', { name: 'Install' }) as HTMLButtonElement
    expect(install.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText(/^Repository/, { selector: 'input' }), { target: { value: 'example-org/example-repo' } })
    fireEvent.change(screen.getByLabelText('Tag, branch or commit'), { target: { value: 'v0.3.1' } })
    fireEvent.change(screen.getByLabelText(/^Overlay/, { selector: 'textarea' }), { target: { value: '{"settings": []}' } })
    fireEvent.click(install)
    await waitFor(() => expect(seen).toHaveLength(1))
    expect(seen[0]).toEqual({
      m: 'POST', url: `${BASE}/plugin-catalogue/installs`,
      body: { source: { kind: 'github', repository: 'example-org/example-repo', ref: 'v0.3.1', subdir: '' }, overlay: { settings: [] } },
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Check' }))
    const preview = screen.getByRole('region', { name: 'What the plugin contains' })
    await waitFor(() => expect(visible(preview)).toContain('Installed'))
    expect(reads).toBe(1)
    expect(visible(preview)).toMatch(/write-docs/)
    expect(visible(preview)).toMatch(/PostToolUse\s*Edit\s*lint-docs/)
    expect(visible(preview)).toContain('node 20')
    expect(within(preview).getByRole('button', { name: 'Settings' })).toBeTruthy()
  })

  it('refuses an overlay that is not one JSON object before anything is sent', async () => {
    const seen: unknown[] = []
    api({ write: (_m, _u, b) => { seen.push(b); return null } })
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    fireEvent.click(await screen.findByRole('button', { name: 'Add plugin' }))
    fireEvent.change(screen.getByLabelText(/^Repository/, { selector: 'input' }), { target: { value: 'example-org/example-repo' } })
    fireEvent.change(screen.getByLabelText('Tag, branch or commit'), { target: { value: 'main' } })
    fireEvent.change(screen.getByLabelText(/^Overlay/, { selector: 'textarea' }), { target: { value: '[1, 2]' } })
    expect(visible(document.body)).toContain('Not one JSON object')
    expect((screen.getByRole('button', { name: 'Install' }) as HTMLButtonElement).disabled).toBe(true)
    expect(seen).toHaveLength(0)
  })

  it('draws an intake’s refusal with its own sentence', async () => {
    const sentence = 'The archive holds a symbolic link out of the plugin: nothing was installed.'
    api({ write: () => ({ status: 202, body: { intake_id: 'i-0002', state: 'refused', install_id: null, refusal: { code: 'archive_link', sentence } } }) })
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    fireEvent.click(await screen.findByRole('button', { name: 'Add plugin' }))
    fireEvent.change(screen.getByLabelText(/^Repository/, { selector: 'input' }), { target: { value: 'example-org/example-repo' } })
    fireEvent.change(screen.getByLabelText('Tag, branch or commit'), { target: { value: 'main' } })
    fireEvent.click(screen.getByRole('button', { name: 'Install' }))
    const preview = screen.getByRole('region', { name: 'What the plugin contains' })
    await waitFor(() => expect(visible(preview)).toContain(sentence))
    expect(visible(preview)).toContain('Not installed')
  })

  it('draws a refused install in the API’s words', async () => {
    const words = 'This repository could not be read through the workspace’s GitHub connection.'
    api({ write: () => ({ status: 422, body: { code: 'source_unreadable', message: words } }) })
    const { ConfigPluginsScreen } = await load()
    render(<ConfigPluginsScreen />)
    fireEvent.click(await screen.findByRole('button', { name: 'Add plugin' }))
    fireEvent.change(screen.getByLabelText(/^Repository/, { selector: 'input' }), { target: { value: 'example-org/example-repo' } })
    fireEvent.change(screen.getByLabelText('Tag, branch or commit'), { target: { value: 'main' } })
    fireEvent.click(screen.getByRole('button', { name: 'Install' }))
    const preview = screen.getByRole('region', { name: 'What the plugin contains' })
    await waitFor(() => expect(visible(preview)).toContain(words))
  })
})
