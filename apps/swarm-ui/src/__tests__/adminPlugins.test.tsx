// ADMIN › PLUGINS (part of #2688; docs/design/managed-plugins.md §17 lane
// 10a, the owner's cards direction of 6 November 2026). Held to:
//
//   * the grid is the served catalogue: the image's built-ins, the platform's
//     installs and the workspaces', each a card with its scope, counts and
//     source, and a filter by scope;
//   * Add plugin sends each source as the route takes it: a GitHub repository,
//     a marketplace entry, or an upload (signed URL, PUT, start);
//   * the overlay form builds the §6.3 document, and the live preview draws it
//     as it is typed; once pinned, the preview is the stored version;
//   * the intake's progress, and every refusal by its fixed sentence: the
//     console's table equals swarm-api's and the intake job's word for word;
//   * a non-admin who types either address sees the route's 403, not a form.
//
// No network: fetch is stubbed per test. Every value is a placeholder.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterAll, afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { visible } from './repofixture'
import { INTAKE_REFUSALS } from '../AdminPluginInstall'

const REPO = join(__dirname, '..', '..', '..', '..')
const BASE = '/v1/admin/plugin-catalogue'
const REPOSITORY = 'example-org/example-repo'
const ADMIN = 'admin@example.com'
const SHA = 'ab'.repeat(32)
const COMMIT = 'cd'.repeat(20)
const INTAKE = 'e1'.repeat(16)
const UPLOAD_URL = 'https://upload.example.com/quarantine/' + INTAKE

type Answer = { status: number; body?: unknown }
type Call = { method: string; url: string; body: unknown; headers: Record<string, string> }

function serve(handler: (method: string, url: string, body: unknown) => Answer | null): Call[] {
  const calls: Call[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    const raw = init?.body
    const body = typeof raw === 'string' ? JSON.parse(raw) : raw ?? null
    const headers = Object.fromEntries(new Headers(init?.headers).entries())
    calls.push({ method, url, body, headers })
    const r = handler(method, url, body) ?? { status: 404, body: { detail: 'Not Found' } }
    return new Response(r.body === undefined ? null : JSON.stringify(r.body), { status: r.status, headers: { 'content-type': 'application/json' } })
  }) as unknown as typeof fetch
  return calls
}

afterEach(() => {
  cleanup()
  window.history.replaceState(null, '', '/')
})

afterAll(() => {
  vi.unstubAllEnvs()
})

// The live build's modules, imported once for the file: a fresh module graph
// per test cost three seconds a test, and no state here outlives a render.
let screens: Promise<{ grid: typeof import('../AdminPlugins'); add: typeof import('../AdminPluginInstall') }> | null = null
function load() {
  if (screens === null) {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    screens = Promise.all([import('../AdminPlugins'), import('../AdminPluginInstall')]).then(([grid, add]) => ({ grid, add }))
  }
  return screens
}

const LIMITS = { max_packed_bytes: 100 * 1048576, max_unpacked_bytes: 500 * 1048576, max_files: 20000, max_expansion_ratio: 100 }

function install(over: Record<string, unknown>): Record<string, unknown> {
  return {
    install_id: 'p-0123456789ab', name: 'review-kit', display_name: 'review-kit', scope: 'platform', tenant_id: null, built_in: false, state: 'active',
    source: { kind: 'github', repository: REPOSITORY, ref: 'v1.2.0', commit: COMMIT, subdir: '' },
    current_version: 'v-0123456789ab', version: '1.2.0', description: 'Reviews a change.', runnable_sha256: SHA, hosts: [],
    components: { skills: 3, commands: 1, agents: 0, hooks: 2, mcp_servers: 1 }, removable: true, updatable: true,
    updated_at: '2026-11-06T10:00:00+00:00', updated_by: ADMIN,
    ...over,
  }
}

const CATALOGUE = {
  scope: 'platform',
  built_in: [{
    install_id: 'builtin:gstack', name: 'gstack', display_name: 'gstack', scope: 'platform', tenant_id: null, built_in: true, state: 'active',
    source: { kind: 'image' }, current_version: null, version: '1.0.0', runner_profiles: ['claude-code', 'claude-code-large'],
    skills: { unattended: ['review', 'qa'], planner: ['review', 'plan'] }, hosts: [], removable: false, updatable: false,
  }],
  installs: [install({})],
  workspace_installs: [install({ install_id: 'w-0123456789ab', name: 'rules-gate', display_name: 'rules-gate', scope: 'workspace', tenant_id: 'team-a', components: { skills: 0, commands: 0, agents: 0, hooks: 1, mcp_servers: 0 }, source: { kind: 'upload', filename: 'rules-gate.tar.gz', commit: 'upload' } })],
  limits: LIMITS,
  can_install: true,
}

describe('the catalogue as a card grid', () => {
  it('draws a card per served plugin, with scope, counts and source, and filters by scope', async () => {
    serve((_m, url) => (url === BASE ? { status: 200, body: CATALOGUE } : null))
    const { grid } = await load()
    render(<grid.AdminPluginsScreen />)
    const list = await screen.findByRole('list', { name: 'Plugins' })
    const card = (name: string) => within(list).getByRole('listitem', { name })
    // MUTATION: drop the workspace installs (`others`) from the grid -> rules-gate is missing.
    expect(within(list).getAllByRole('listitem').map((li) => li.getAttribute('aria-label'))).toEqual(['gstack', 'review-kit', 'rules-gate'])
    expect(visible(card('gstack'))).toMatch(/Built in/)
    expect(visible(card('gstack'))).toMatch(/3 skills/)
    expect(visible(card('gstack'))).toMatch(/Agent image/)
    expect(visible(card('review-kit'))).toMatch(/Platform/)
    expect(visible(card('review-kit'))).toMatch(/2 hooks/)
    expect(visible(card('review-kit'))).toContain(`${REPOSITORY}@v1.2.0`)
    expect(visible(card('rules-gate'))).toMatch(/Workspace/)
    expect(visible(card('rules-gate'))).toMatch(/team-a/)
    expect(visible(card('rules-gate'))).toMatch(/rules-gate\.tar\.gz/)
    // A built-in has no page of its own; an install opens its page.
    expect(card('gstack').querySelector('a')).toBeNull()
    expect(card('review-kit').querySelector('a')?.getAttribute('href')).toBe(grid.adminPluginPath('p-0123456789ab'))
    expect(screen.getByRole('link', { name: 'Add plugin' }).getAttribute('href')).toBe(grid.ADMIN_PLUGIN_ADD_PATH)

    fireEvent.click(screen.getByRole('button', { name: /Workspace/ }))
    expect(within(list).getAllByRole('listitem').map((li) => li.getAttribute('aria-label'))).toEqual(['rules-gate'])
  })

  it('offers no Add plugin to a reader who cannot install', async () => {
    serve((_m, url) => (url === BASE ? { status: 200, body: { ...CATALOGUE, can_install: false } } : null))
    const { grid } = await load()
    render(<grid.AdminPluginsScreen />)
    await screen.findByRole('list', { name: 'Plugins' })
    expect(screen.queryByRole('link', { name: 'Add plugin' })).toBeNull()
  })
})

describe('admin only', () => {
  it('a non-admin who types either address sees the 403, not cards or a form', async () => {
    serve((_m, url) => (url === BASE ? { status: 403, body: { detail: 'admin only', code: 'admin_required' } } : null))
    const { grid, add } = await load()
    render(<grid.AdminPluginsScreen />)
    await waitFor(() => expect(visible(document.body)).toMatch(/admin/i))
    expect(screen.queryByRole('list', { name: 'Plugins' })).toBeNull()
    cleanup()
    render(<add.AdminPluginInstallScreen />)
    await waitFor(() => expect(visible(document.body)).toMatch(/admin/i))
    expect(screen.queryByRole('region', { name: 'Source' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Install' })).toBeNull()
  })
})

const RUNNING = { intake_id: INTAKE, scope: 'platform', tenant_id: null, install_id: null, state: 'running', source: { kind: 'github', repository: REPOSITORY, ref: 'v1.2.0', commit: COMMIT }, version_id: null, refusal: null, deadline: '2026-11-08T12:15:00+00:00' }
const PINNED = { ...RUNNING, state: 'pinned', install_id: 'p-0123456789ab', version_id: 'v-0123456789ab' }
const VERSIONS = {
  install: install({}),
  versions: [{
    version_id: 'v-0123456789ab', current: true, version: '1.2.0', source: { kind: 'github', repository: REPOSITORY, ref: 'v1.2.0', commit: COMMIT },
    source_sha256: SHA, runnable_sha256: SHA, runnable_bytes: 4096, files: 12, unpacked_bytes: 9000,
    manifest: { name: 'review-kit', version: '1.2.0', description: 'Reviews a change.', license: 'MIT' },
    components: {
      skills: [{ name: 'review', path: 'skills/review' }], commands: [], agents: [],
      hooks: [{ event: 'PreToolUse', matcher: 'Bash', type: 'command', command: 'node ${CLAUDE_PLUGIN_ROOT}/bin/gate.mjs', timeout: 30 }],
      mcp_servers: [{ name: 'lookup', command: 'node' }],
    },
    requires: { suggested: { runtimes: [{ name: 'node', min_version: '20' }], hosts: ['api.example.com'] }, declared: null },
    hosts: ['api.example.com'], created_at: '2026-11-08T12:00:00+00:00', created_by: ADMIN,
  }],
}

// ROLE QUERIES ARE AVOIDED ON THE ADD PAGE: its form is large enough that one
// `getByRole('button', ...)` took over a second in jsdom, eighty tests over.
function button(name: string, root: ParentNode = document): HTMLButtonElement {
  const found = [...root.querySelectorAll('button')].filter((b) => b.textContent!.trim() === name)
  if (found.length !== 1) throw new Error(`${found.length} buttons named ${name}`)
  return found[0]!
}

function labelled(name: string): HTMLElement {
  const found = document.querySelector<HTMLElement>(`[aria-label="${name}"]`)
  if (found === null) throw new Error(`nothing labelled ${name}`)
  return found
}

function stepClasses(): string[] {
  return [...labelled('Intake').querySelectorAll('li')].map((li) => li.className)
}

/** A field by its label's opening words: an (i) beside the label adds its own text. */
function typeInto(label: string | RegExp, value: string) {
  const name = typeof label === 'string' ? new RegExp(`^${label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`) : label
  fireEvent.change(screen.getByLabelText(name), { target: { value } })
}

async function openAdd(handler: (method: string, url: string, body: unknown) => Answer | null) {
  const calls = serve((m, url, body) => (m === 'GET' && url === BASE ? { status: 200, body: CATALOGUE } : handler(m, url, body)))
  const { add } = await load()
  render(<add.AdminPluginInstallScreen pollMs={5} />)
  await waitFor(() => labelled('Source'))
  return { calls, add }
}

function pinnedFlow(m: string, url: string): Answer | null {
  if (m === 'POST' && url === `${BASE}/installs`) return { status: 202, body: RUNNING }
  if (m === 'GET' && url === `${BASE}/intakes/${INTAKE}`) return { status: 200, body: PINNED }
  if (m === 'GET' && url === `${BASE}/installs/p-0123456789ab/versions`) return { status: 200, body: VERSIONS }
  return null
}

describe('Add plugin, each source', () => {
  it('a GitHub repository: one POST of the repository, ref and folder; progress to pinned; the stored version previewed', async () => {
    const { calls } = await openAdd(pinnedFlow)
    const install = button('Install')
    expect((install as HTMLButtonElement).disabled).toBe(true)
    typeInto('Repository', REPOSITORY)
    typeInto('Tag, branch or commit', 'v1.2.0')
    typeInto(/Folder/, 'plugins/review-kit')
    fireEvent.click(button('Install'))
    await screen.findByLabelText('Pinned version')
    const post = calls.find((c) => c.method === 'POST')!
    // MUTATION: send the folder under another key, or send an overlay nobody wrote -> fails.
    expect(post.url).toBe(`${BASE}/installs`)
    expect(post.body).toEqual({ source: { kind: 'github', repository: REPOSITORY, ref: 'v1.2.0', subdir: 'plugins/review-kit' } })
    // The intake was read until it ended, then the version it pinned.
    expect(calls.some((c) => c.url === `${BASE}/intakes/${INTAKE}`)).toBe(true)
    const preview = screen.getByLabelText('Pinned version')
    expect(visible(preview)).toMatch(/review-kit/)
    expect(visible(preview)).toMatch(/MIT/)
    expect(visible(preview)).toContain('node ${CLAUDE_PLUGIN_ROOT}/bin/gate.mjs')
    expect(visible(preview)).toMatch(/node >= 20/)
    expect(visible(preview)).toMatch(/net: api\.example\.com/)
    expect(visible(preview)).toContain(COMMIT.slice(0, 12))
    expect(visible(preview)).toContain(SHA.slice(0, 12))
    const steps = labelled('Intake')
    expect([...steps.querySelectorAll('li')].map((li) => li.className)).toEqual(['is-done', 'is-done', 'is-done'])
    expect([...document.querySelectorAll('a')].find((a) => a.textContent === 'Open')?.getAttribute('href')).toBe('/admin/plugins/p-0123456789ab')
  })

  it('a marketplace entry: the marketplace repository, its ref and the entry name', async () => {
    const { calls } = await openAdd(pinnedFlow)
    fireEvent.click(button('Marketplace'))
    typeInto('Marketplace', REPOSITORY)
    typeInto('Tag, branch or commit', 'main')
    typeInto('Plugin', 'review-kit')
    fireEvent.click(button('Install'))
    await screen.findByLabelText('Pinned version')
    // MUTATION: send the entry as `subdir`, or send kind github -> fails.
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ source: { kind: 'marketplace', repository: REPOSITORY, ref: 'main', plugin: 'review-kit' } })
  })

  it('an upload: an intake and a signed URL, the bytes PUT straight to storage with its headers, then start', async () => {
    const opened = { intake: { ...RUNNING, state: 'awaiting_upload', source: { kind: 'upload', filename: 'review-kit.tar.gz', commit: 'upload' } }, upload: { method: 'PUT', url: UPLOAD_URL, expires_in: 600, headers: { 'Content-Type': 'application/gzip', 'x-goog-content-length-range': `0,${LIMITS.max_packed_bytes}` } } }
    const { calls } = await openAdd((m, url) => {
      if (m === 'POST' && url === `${BASE}/uploads`) return { status: 201, body: opened }
      if (m === 'PUT' && url === UPLOAD_URL) return { status: 200 }
      if (m === 'POST' && url === `${BASE}/intakes/${INTAKE}/start`) return { status: 202, body: RUNNING }
      return pinnedFlow(m, url)
    })
    fireEvent.click(button('Upload'))
    const file = new File(['packed bytes'], 'review-kit.tar.gz', { type: 'application/gzip' })
    fireEvent.change(screen.getByLabelText(/^Archive/), { target: { files: [file] } })
    fireEvent.click(button('Install'))
    await screen.findByLabelText('Pinned version')
    const order = calls.filter((c) => c.method !== 'GET').map((c) => `${c.method} ${c.url}`)
    // MUTATION: start before the PUT, or skip the PUT -> fails.
    expect(order).toEqual([`POST ${BASE}/uploads`, `PUT ${UPLOAD_URL}`, `POST ${BASE}/intakes/${INTAKE}/start`])
    expect(calls.find((c) => c.method === 'POST' && c.url === `${BASE}/uploads`)!.body).toEqual({ filename: 'review-kit.tar.gz', size: file.size, subdir: '' })
    const put = calls.find((c) => c.method === 'PUT')!
    expect(put.body).toBe(file)
    expect(put.headers['content-type']).toBe('application/gzip')
    expect(put.headers['x-goog-content-length-range']).toBe(`0,${LIMITS.max_packed_bytes}`)
  })

  it('an upload larger than the served cap is refused before anything is sent', async () => {
    const { calls } = await openAdd(() => null)
    fireEvent.click(button('Upload'))
    const file = new File(['x'], 'big.zip')
    Object.defineProperty(file, 'size', { value: LIMITS.max_packed_bytes + 1 })
    fireEvent.change(screen.getByLabelText(/^Archive/), { target: { files: [file] } })
    expect(screen.getByRole('alert').textContent).toBe('The archive is larger than 100 MiB.')
    expect((button('Install') as HTMLButtonElement).disabled).toBe(true)
    expect(calls.filter((c) => c.method !== 'GET')).toEqual([])
  })
})

describe('the overlay and the live preview', () => {
  it('builds the §6.3 document from the form, draws it as typed, and sends it with the source', async () => {
    const { calls } = await openAdd(pinnedFlow)
    typeInto('Repository', REPOSITORY)
    typeInto('Tag, branch or commit', 'v0.2.0')
    const preview = screen.getByLabelText('Live preview')
    expect(visible(preview)).toContain(`${REPOSITORY}@v0.2.0`)
    expect(visible(preview)).toMatch(/plugin\.json from the source/)
    expect(within(preview).queryByText('swarm-overlay.json')).toBeNull()

    fireEvent.click(screen.getByLabelText('No plugin.json'))
    typeInto('Name', 'rules-gate')
    typeInto(/^Version/, '0.2.0')
    fireEvent.click(button('Add hook'))
    const hook = labelled('Hook 1')
    fireEvent.change(within(hook).getByLabelText('Event'), { target: { value: 'Stop' } })
    fireEvent.change(within(hook).getByLabelText('File in the plugin'), { target: { value: 'belay.mjs' } })
    fireEvent.change(within(hook).getByLabelText('Timeout in seconds'), { target: { value: '60' } })
    fireEvent.click(button('Add setting'))
    const setting = labelled('Setting 1')
    fireEvent.change(within(setting).getByLabelText('Key'), { target: { value: 'rules' } })
    fireEvent.change(within(setting).getByLabelText('Type'), { target: { value: 'file' } })
    fireEvent.change(within(setting).getByLabelText('Environment variable'), { target: { value: 'GATE_RULES_FILE' } })
    fireEvent.click(within(labelled('Runtimes')).getByLabelText('node'))

    const expected = {
      plugin: { name: 'rules-gate', version: '0.2.0' },
      hooks: [{ event: 'Stop', command: 'node ${CLAUDE_PLUGIN_ROOT}/belay.mjs', timeout: 60 }],
      settings: [{ key: 'rules', type: 'file', target: 'file', variable: 'GATE_RULES_FILE', sensitive: false }],
      requires: { runtimes: [{ name: 'node' }], hosts: [] },
    }
    // The preview is the document word for word, as it is typed.
    const live = screen.getByLabelText('Live preview')
    expect(visible(live)).toMatch(/plugin\.json from the overlay/)
    expect(visible(live)).toContain('node ${CLAUDE_PLUGIN_ROOT}/belay.mjs')
    const code = [...live.querySelectorAll('.c-dl')].map((l) => l.textContent!.replace(/^\d+/, '')).join('\n')
    expect(JSON.parse(code)).toEqual(expected)

    fireEvent.click(button('Install'))
    await screen.findByLabelText('Pinned version')
    // MUTATION: send the overlay under another key, or drop the hook's root prefix -> fails.
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ source: { kind: 'github', repository: REPOSITORY, ref: 'v0.2.0', subdir: '' }, overlay: expected })
  })

  it('holds Install until the manifest is named and each hook has its file', async () => {
    await openAdd(() => null)
    typeInto('Repository', REPOSITORY)
    typeInto('Tag, branch or commit', 'v1')
    const install = () => button('Install') as HTMLButtonElement
    expect(install().disabled).toBe(false)
    fireEvent.click(screen.getByLabelText('No plugin.json'))
    expect(install().disabled).toBe(true)
    typeInto('Name', 'rules-gate')
    expect(install().disabled).toBe(false)
    fireEvent.click(button('Add hook'))
    expect(install().disabled).toBe(true)
  })
})

describe('intake progress and refusals', () => {
  it('the console\'s refusal table is swarm-api\'s and the intake job\'s, word for word', async () => {
    const { add } = await load()
    const table = (path: string) => {
      const text = readFileSync(join(REPO, path), 'utf8')
      const block = text.slice(text.indexOf('REFUSALS: dict[str, str] = {'))
      const body = block.slice(0, block.indexOf('\n}'))
      const out: Record<string, string> = {}
      for (const m of body.matchAll(/^\s+"([a-z_]+)":\s+("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'),?$/gm)) out[m[1]!] = m[2]!.slice(1, -1)
      return out
    }
    const api = table('apps/swarm-api/swarm_api/pluginstore.py')
    const job = table('apps/plugin-intake/plugin_intake/refusals.py')
    expect(Object.keys(api).length).toBeGreaterThan(30)
    expect(Object.keys(job).length).toBeGreaterThan(40)
    // MUTATION: drop a code from INTAKE_REFUSALS, or reword one -> fails.
    expect(add.INTAKE_REFUSALS).toEqual({ ...job, ...api })
    // One code is in both tables with two sentences; swarm-api serves its own, so the console draws that one.
    // A second such code fails here, so the drift is seen rather than inherited.
    expect(Object.keys(api).filter((code) => code in job && api[code] !== job[code])).toEqual(['subdir_invalid'])
  })

  it('shows Check as the current step while the intake runs', async () => {
    let reads = 0
    await openAdd((m, url) => {
      if (m === 'POST' && url === `${BASE}/installs`) return { status: 202, body: RUNNING }
      if (m === 'GET' && url === `${BASE}/intakes/${INTAKE}`) return { status: 200, body: (reads += 1) < 3 ? RUNNING : PINNED }
      return pinnedFlow(m, url)
    })
    typeInto('Repository', REPOSITORY)
    typeInto('Tag, branch or commit', 'v1')
    fireEvent.click(button('Install'))
    await waitFor(() => expect(labelled('Intake').querySelector('[aria-current="step"]')?.textContent).toBe('Check'))
    expect(stepClasses()).toEqual(['is-done', 'is-now', 'is-todo'])
    await screen.findByLabelText('Pinned version')
    expect(reads).toBeGreaterThanOrEqual(3)
  })

  it('a refusal by the route draws its fixed sentence, its code, and Fetch as the failed step', async () => {
    await openAdd((m, url) => (m === 'POST' && url === `${BASE}/installs` ? { status: 422, body: { code: 'ref_not_found', message: 'The ref does not name a commit in the repository.', detail: {} } } : null))
    typeInto('Repository', REPOSITORY)
    typeInto('Tag, branch or commit', 'v9')
    fireEvent.click(button('Install'))
    const banner = await screen.findByRole('alert')
    expect(visible(banner)).toContain('The ref does not name a commit in the repository.')
    expect(visible(banner)).toContain('ref_not_found')
    expect(stepClasses()).toEqual(['is-bad', 'is-todo', 'is-todo'])
    expect(button('Try again')).toBeTruthy()
  })

  it('a PUT the store refuses ends the upload with its own sentence, and nothing is started', async () => {
    const opened = { intake: { ...RUNNING, state: 'awaiting_upload' }, upload: { method: 'PUT', url: UPLOAD_URL, headers: { 'Content-Type': 'application/zip' } } }
    const { calls } = await openAdd((m, url) => (m === 'POST' && url === `${BASE}/uploads` ? { status: 201, body: opened } : m === 'PUT' ? { status: 400 } : null))
    fireEvent.click(button('Upload'))
    fireEvent.change(screen.getByLabelText(/^Archive/), { target: { files: [new File(['x'], 'a.zip')] } })
    fireEvent.click(button('Install'))
    const banner = await screen.findByRole('alert')
    expect(visible(banner)).toContain('Cloud Storage did not take the archive; nothing was installed.')
    expect(calls.some((c) => c.url.endsWith('/start'))).toBe(false)
  })

  it.each(Object.keys(INTAKE_REFUSALS).map((code) => [code]))(
    'an intake refused with %s draws that code\'s sentence, never text from the job',
    async (code) => {
      const { add } = await openAdd((m, url) => {
        if (m === 'POST' && url === `${BASE}/installs`) return { status: 202, body: RUNNING }
        // The job's own codes come back with no sentence: the console looks the code up.
        if (m === 'GET' && url === `${BASE}/intakes/${INTAKE}`) return { status: 200, body: { ...RUNNING, state: 'refused', refusal: { code, sentence: null, entry_index: 4 } } }
        return null
      })
      typeInto('Repository', REPOSITORY)
      typeInto('Tag, branch or commit', 'v1')
      fireEvent.click(button('Install'))
      const banner = await screen.findByRole('alert')
      expect(visible(banner)).toContain(add.INTAKE_REFUSALS[code]!)
      expect(visible(banner)).toContain(code)
      expect(visible(banner)).toContain('entry 4')
      expect(stepClasses()).toEqual(['is-done', 'is-bad', 'is-todo'])
    },
  )
})
