// A MANAGED PLUGIN'S PAGE (lane plugins-console-detail, part of #2688;
// docs/design/managed-plugins.md section 9). The owner picked the cards
// direction on 6 November 2026: one page per plugin with Overview, Settings,
// Versions and Usage tabs; Update shows the diff between the installed and
// the new version and one admin applies it; Remove lists who uses it first.
//
// MUTATIONS, each of which turns a case red: draw a part the route did not
// serve; show a secret's value; apply without quoting the diff's digest, or
// without the typed name on a marked diff; remove without the typed name or
// without listing the users; call a failed read empty, or offer no retry.
//
// Fixtures only; no network. Nothing here is credential-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null

const TENANT = 'team-a'
const INSTALL = 'w-0123456789ab'
const BASE = `/v1/workspaces/${TENANT}/plugin-catalogue`
const C1 = '1'.repeat(40)
const C2 = '2'.repeat(40)
const D1 = 'a'.repeat(64)
const D2 = 'b'.repeat(64)
const DIFF_SHA = 'c'.repeat(64)

const STOP = { event: 'Stop', matcher: null, command: 'node check.mjs' }
const GUARD = { event: 'PreToolUse', matcher: 'Bash', command: 'node guard.mjs' }

function version(id: string, v: string, commit: string, digest: string, at: string, current: boolean, hooks: Json[]): Json {
  return {
    version_id: id, current, version: v,
    source: { kind: 'github', repository: 'example-org/example-repo', ref: `v${v}`, commit },
    runnable_sha256: digest, runnable_bytes: 40_000, files: 12, unpacked_bytes: 131_072,
    manifest: { name: 'example-plugin', version: v, license: 'MIT' },
    components: { skills: [{ name: 'check-turn' }], hooks, commands: [{ name: 'tidy' }], agents: [], mcp_servers: [{ name: 'notes' }] },
    requires: { declared: { runtimes: ['node'], hosts: ['api.example.com'] } },
    hosts: ['api.example.com'], created_at: at, created_by: 'admin@example.com',
  }
}

function versions(current = 'v-000000000001'): Json {
  return {
    install: {
      install_id: INSTALL, name: 'example-plugin', display_name: null, scope: 'workspace', tenant_id: TENANT, state: 'active',
      source: { kind: 'github', repository: 'example-org/example-repo', ref: 'v0.2.0', commit: C1 },
      current_version: current, version: current === 'v-000000000001' ? '0.2.0' : '0.3.0', description: 'Checks each turn before it ends',
      runnable_sha256: D1, hosts: ['api.example.com'], components: { skills: 1, commands: 1, agents: 0, hooks: 1, mcp_servers: 1 },
      removable: true, updatable: true, updated_at: '2026-11-06T01:42:00+00:00', updated_by: 'admin@example.com',
    },
    versions: [
      version('v-000000000001', '0.2.0', C1, D1, '2026-11-06T01:42:00+00:00', current === 'v-000000000001', [STOP]),
      version('v-000000000002', '0.3.0', C2, D2, '2026-11-07T03:10:00+00:00', current === 'v-000000000002', [STOP, GUARD]),
    ],
  }
}

function diff(marked: boolean): Json {
  const none = { added: [], removed: [] }
  return {
    install_id: INSTALL,
    diff_sha256: DIFF_SHA,
    diff: {
      from_version_id: 'v-000000000001', to_version_id: 'v-000000000002',
      manifest: { version: { from: '0.2.0', to: '0.3.0' } },
      hooks: { added: marked ? [GUARD] : [], removed: [], changed: [] },
      mcp_servers: { ...none, changed: [] }, skills: { added: ['plan-turn'], removed: [] }, commands: none, agents: none,
      settings: { added: ['max_retries'], removed: [], retyped: [], newly_sensitive: [] },
      hosts: none, runtimes: null, files: { from: 12, to: 13 }, unpacked_bytes: { from: 131_072, to: 140_000 },
      runnable_sha256: { from: D1, to: D2 },
      source: { from_commit: C1, to_commit: C2, compare: `https://github.com/example-org/example-repo/compare/${C1}...${C2}` },
      marked: marked ? ['hooks'] : [], needs_confirmation: marked,
    },
  }
}

function workspaceCatalogue(): Json {
  return { scope: 'workspace', tenant_id: TENANT, built_in: [], platform_installs: [], installs: [versions().install], limits: {}, can_install: true }
}

function settings(): Json {
  return {
    tenant_id: TENANT,
    form: { install_id: INSTALL, version_id: 'v-000000000001', source: 'user_config', fields: [
      { key: 'threshold', type: 'number', kind: 'value', title: 'Threshold', required: false, sensitive: false, default: 0.7 },
      { key: 'mode', type: 'string', kind: 'value', title: null, required: false, sensitive: false, default: 'strict' },
      { key: 'service_key', type: 'string', kind: 'secret', title: 'Service key', required: true, sensitive: true, default: null },
    ] },
    revision: 3,
    values: { threshold: { set: true, value: 0.75 }, mode: { set: false }, service_key: { set: true, set_by: 'admin@example.com', set_at: '2026-11-06T02:04:00+00:00' } },
    missing: [], set_at: '2026-11-06T02:04:00+00:00', set_by: 'admin@example.com', can_edit: true,
  }
}

function usage(): Json {
  return {
    workspaces: [],
    workflows: [{ tenant_id: TENANT, id: 'wf-1', name: 'nightly-check', kind: 'workflow', steps: ['review', 'verify'], how: 'added' }],
    schedules: [{ tenant_id: TENANT, schedule_id: 'sch-1', name: 'every-night' }],
    steps: [{ tenant_id: TENANT, task_id: 'task-77', workflow_id: 'wf-1', step: 'review', state: 'RUNNING' }],
  }
}

type Over = Partial<Record<'versions' | 'diff' | 'usage' | 'settings', (n: number) => Reply>> & {
  write?: (m: string, url: string, body: unknown) => Reply
}

function api(over: Over = {}) {
  const n = { versions: 0, diff: 0, usage: 0, settings: 0 }
  const calls = serve((m, url, b) => {
    if (m !== 'GET') return over.write?.(m, url, b) ?? null
    if (url.startsWith(`${BASE}/installs/${INSTALL}/versions`)) {
      n.versions += 1
      return over.versions?.(n.versions) ?? { status: 200, body: versions() }
    }
    if (url.startsWith(`${BASE}/installs/${INSTALL}/diff`)) {
      n.diff += 1
      return over.diff?.(n.diff) ?? { status: 200, body: diff(true) }
    }
    if (url.startsWith(`${BASE}/installs/${INSTALL}/usage`)) {
      n.usage += 1
      return over.usage?.(n.usage) ?? { status: 200, body: usage() }
    }
    if (url.startsWith(`/v1/workspaces/${TENANT}/plugin-config/${INSTALL}`)) {
      n.settings += 1
      return over.settings?.(n.settings) ?? { status: 200, body: settings() }
    }
    if (url.startsWith(`/v1/workspaces/${TENANT}/plugin-defaults`)) return { status: 200, body: { tenant_id: TENANT, defaults: [INSTALL], can_edit: true } }
    if (url === BASE || url.startsWith(`${BASE}?`)) return { status: 200, body: workspaceCatalogue() }
    return null
  })
  return { calls, n }
}

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../PluginDetail')
}

async function page(tab?: 'overview' | 'settings' | 'versions' | 'usage') {
  const { PluginDetailScreen } = await load()
  render(<PluginDetailScreen installId={INSTALL} tenantId={TENANT} backHref="/config/plugins" />)
  await screen.findByRole('heading', { level: 1, name: /example-plugin/ })
  if (tab !== undefined) fireEvent.click(screen.getByRole('tab', { name: new RegExp(`^${tab}`, 'i') }))
  return screen.getByRole('tabpanel')
}

afterEach(() => {
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

describe("A managed plugin's page", () => {
  it('heads the page with the served name, version, scope and the workspace facts, and has the four tabs', async () => {
    api()
    await page()
    const h1 = screen.getByRole('heading', { level: 1 })
    expect(visible(h1)).toBe('example-plugin0.2.0')
    expect(screen.getByText('Workspace')).toBeTruthy()
    expect(screen.getByText('Every step')).toBeTruthy()
    const tabs = screen.getAllByRole('tab').map((t) => visible(t))
    expect(tabs).toEqual(['Overview', 'Settings', 'Versions', 'Usage'])
    expect(screen.getByRole('link', { name: 'Plugins' }).getAttribute('href')).toBe('/config/plugins')
  })

  it('Overview draws the installed version as served: hooks, skills, commands, MCP servers, source, digest and needs', async () => {
    api()
    const panel = await page()
    await within(panel).findByRole('region', { name: 'Hooks' })
    const hooks = within(panel).getByRole('region', { name: 'Hooks' })
    expect(visible(hooks)).toContain('Stop')
    expect(visible(hooks)).toContain('node check.mjs')
    // The current version's parts, not the candidate's: its new hook is not drawn.
    expect(visible(panel)).not.toContain('guard.mjs')
    expect(visible(within(panel).getByRole('region', { name: 'Skills' }))).toContain('check-turn')
    expect(visible(within(panel).getByRole('region', { name: 'Commands' }))).toContain('tidy')
    expect(visible(within(panel).getByRole('region', { name: 'MCP servers' }))).toContain('notes')
    expect(within(panel).queryByRole('region', { name: 'Agents' })).toBeNull()
    const side = within(panel).getByRole('complementary', { name: 'The pinned copy' })
    expect(visible(side)).toContain('example-org/example-repo')
    expect(visible(side)).toContain(C1.slice(0, 12))
    expect(visible(side)).toContain(D1.slice(0, 12))
    expect(visible(side)).toContain('MIT')
    expect(visible(side)).toContain('net: api.example.com')
    // The why is behind an (i), not on the surface.
    expect(within(panel).getByRole('button', { name: 'About hooks' })).toBeTruthy()
  })

  it('Settings shows each value or its default, and a secret only as stored, never its value', async () => {
    api()
    const panel = await page('settings')
    const table = await within(panel).findByRole('table', { name: 'Settings' })
    const rows = within(table).getAllByRole('row').map((r) => visible(r))
    expect(rows[0]).toContain('threshold')
    expect(rows[0]).toContain('0.75')
    expect(rows[1]).toContain('mode')
    expect(rows[1]).toContain('default strict')
    expect(rows[2]).toContain('service_key')
    expect(rows[2]).toContain('secret')
    expect(rows[2]).toContain('Stored')
    expect(visible(panel)).toContain('admin@example.com')
  })

  it('Versions lists every pinned version, newest first, the current one marked and the newer one offered for review', async () => {
    api()
    const panel = await page('versions')
    const list = await within(panel).findByRole('list')
    const rows = within(list).getAllByRole('listitem').map((r) => visible(r))
    expect(rows).toHaveLength(2)
    expect(rows[0]).toContain('0.3.0')
    expect(rows[0]).toContain('New')
    expect(rows[0]).toContain(C2.slice(0, 12))
    expect(rows[1]).toContain('0.2.0')
    expect(rows[1]).toContain('Current')
    expect(within(panel).getByRole('button', { name: 'Review the update' })).toBeTruthy()
    expect(within(panel).getByRole('button', { name: 'Check for a new version' })).toBeTruthy()
  })

  it('Update reads the diff from the installed to the new version and draws it, marked where a malicious update acts', async () => {
    const { calls } = api()
    const panel = await page('versions')
    fireEvent.click(await within(panel).findByRole('button', { name: 'Review the update' }))
    const upd = await within(panel).findByRole('region', { name: 'Update example-plugin' })
    await within(upd).findByRole('region', { name: 'Hooks' })
    expect(calls.some((c) => c.method === 'GET' && c.url === `${BASE}/installs/${INSTALL}/diff?version_id=v-000000000002`)).toBe(true)
    expect(visible(upd)).toContain('0.2.0 → 0.3.0')
    const hooks = within(upd).getByRole('region', { name: 'Hooks' })
    expect(visible(hooks)).toContain('Marked')
    expect(visible(hooks)).toContain('PreToolUse')
    expect(visible(hooks)).toContain('node guard.mjs')
    expect(within(hooks).getByLabelText('Added')).toBeTruthy()
    expect(visible(within(upd).getByRole('region', { name: 'Skills' }))).toContain('plan-turn')
    expect(visible(within(upd).getByRole('region', { name: 'Settings' }))).toContain('max_retries')
    expect(within(upd).getByRole('link', { name: 'Compare' }).getAttribute('href')).toContain(`${C1}...${C2}`)
  })

  it('a marked update applies only with the name typed, quoting the digest of the diff that was read', async () => {
    const { calls } = api({
      write: (m, url) => (m === 'POST' && url === `${BASE}/installs/${INSTALL}/apply` ? { status: 200, body: versions('v-000000000002') } : null),
    })
    const panel = await page('versions')
    fireEvent.click(await within(panel).findByRole('button', { name: 'Review the update' }))
    fireEvent.click(await within(panel).findByRole('button', { name: 'Update to 0.3.0' }))
    const dialog = await screen.findByRole('dialog')
    const go = within(dialog).getByRole('button', { name: 'Update to 0.3.0' }) as HTMLButtonElement
    expect(go.disabled).toBe(true)
    expect(calls.filter((c) => c.method === 'POST')).toHaveLength(0)
    fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: 'example-plugin' } })
    expect(go.disabled).toBe(false)
    fireEvent.click(go)
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST')).toHaveLength(1))
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ version_id: 'v-000000000002', diff_sha256: DIFF_SHA, confirm_name: 'example-plugin' })
    // Applied: back on the list, read again.
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await within(panel).findByRole('button', { name: 'Check for a new version' })
  })

  it('an unmarked update applies with one click and no typed name', async () => {
    const { calls } = api({
      diff: () => ({ status: 200, body: diff(false) }),
      write: (m, url) => (m === 'POST' && url.endsWith('/apply') ? { status: 200, body: versions('v-000000000002') } : null),
    })
    const panel = await page('versions')
    fireEvent.click(await within(panel).findByRole('button', { name: 'Review the update' }))
    fireEvent.click(await within(panel).findByRole('button', { name: 'Update to 0.3.0' }))
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST')).toHaveLength(1))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ version_id: 'v-000000000002', diff_sha256: DIFF_SHA })
  })

  it('a diff that changed since it was read is refused in place and read again', async () => {
    const { n } = api({
      diff: () => ({ status: 200, body: diff(false) }),
      write: () => ({ status: 409, body: { code: 'diff_changed', message: 'The diff changed since it was shown; read it again before applying.' } }),
    })
    const panel = await page('versions')
    fireEvent.click(await within(panel).findByRole('button', { name: 'Review the update' }))
    fireEvent.click(await within(panel).findByRole('button', { name: 'Update to 0.3.0' }))
    const alert = await within(panel).findByRole('alert')
    expect(visible(alert)).toContain('The diff changed')
    await waitFor(() => expect(n.diff).toBe(2))
  })

  it('Usage lists the workflows, schedules and steps that name it, beside the workspace facts', async () => {
    api()
    const panel = await page('usage')
    const wf = await within(panel).findByRole('table', { name: 'Workflows' })
    expect(visible(wf)).toContain('nightly-check')
    expect(visible(wf)).toContain('review')
    expect(visible(wf)).toContain('verify')
    expect(visible(within(panel).getByRole('table', { name: 'Schedules' }))).toContain('every-night')
    expect(visible(within(panel).getByRole('table', { name: 'Steps now' }))).toContain('RUNNING')
    const facts = within(panel).getByLabelText('This workspace')
    expect(visible(facts)).toContain('Every step')
    expect(visible(facts)).toContain('Yes')
  })

  it('Usage the API does not serve yet is said as not served, never as nobody', async () => {
    api({ usage: () => ({ status: 404, body: { detail: 'Not Found' } }) })
    const panel = await page('usage')
    await within(panel).findByText('Not served yet')
    expect(visible(panel)).not.toContain('Nothing uses it')
  })

  it('Remove lists who uses it, unlocks only on the typed name, and removes it', async () => {
    const { calls } = api({
      write: (m, url) => (m === 'DELETE' ? { status: 200, body: { ...(versions().install as Json), state: 'removed', removable: false, updatable: false, url } } : null),
    })
    await page()
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }))
    const dialog = await screen.findByRole('dialog')
    await within(dialog).findByRole('table', { name: 'Workflows' })
    expect(visible(dialog)).toContain('Used by 3')
    expect(visible(dialog)).toContain('nightly-check')
    expect(visible(dialog)).toContain('every-night')
    const go = within(dialog).getByRole('button', { name: 'Remove' }) as HTMLButtonElement
    expect(go.disabled).toBe(true)
    fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: 'example-plugin' } })
    fireEvent.click(go)
    await waitFor(() => expect(calls.filter((c) => c.method === 'DELETE')).toHaveLength(1))
    expect(calls.find((c) => c.method === 'DELETE')!.url).toBe(`${BASE}/installs/${INSTALL}?confirm=example-plugin`)
    await screen.findByText('example-plugin removed')
    expect(screen.queryByRole('button', { name: 'Remove' })).toBeNull()
  })

  it('a read that fails says so with a retry, and the retry reads again', async () => {
    const { n } = api({ versions: (k) => (k === 1 ? { status: 500, body: { code: 'internal', message: 'boom' } } : null) })
    const { PluginDetailScreen } = await load()
    render(<PluginDetailScreen installId={INSTALL} tenantId={TENANT} />)
    const retry = await screen.findByRole('button', { name: 'Try again' })
    expect(screen.queryByRole('region', { name: 'Hooks' })).toBeNull()
    fireEvent.click(retry)
    await screen.findByRole('region', { name: 'Hooks' })
    expect(n.versions).toBe(2)
  })
})
