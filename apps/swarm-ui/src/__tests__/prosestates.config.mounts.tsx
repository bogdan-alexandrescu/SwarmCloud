// The config section's click-only states the fixture build never draws
// (issue 439): each screen mounted on hand-built reads behind a stubbed
// fetch, so one click opens the state. Copied from the tests that build the
// same reads: pluginDetail (a workspace plugin with a marked update),
// config.strip-done (a finished setup strip), access.push-test (a write
// grant), pluginsheet and pluginsheet.managed (a step's plugin sheets).

import type { ReactElement } from 'react'
import { vi } from 'vitest'

import type { Result } from '../fetch'
import type { ManagedPluginInstall, ManagedPluginsRead, PluginConfigView, PluginsView } from '../types'
import { serve } from './repofixture'

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null

const realFetch = globalThis.fetch

/** Undo the mounts' stubs: called after each state. */
export function restoreConfigStubs(): void {
  globalThis.fetch = realFetch
  vi.doUnmock('../api')
  vi.unstubAllEnvs()
  vi.resetModules()
}

async function live(): Promise<void> {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  await Promise.resolve()
}

// ---------------------------------------------------------------- a plugin's page

const TENANT = 'team-a'
const INSTALL = 'w-0123456789ab'
const BASE = `/v1/workspaces/${TENANT}/plugin-catalogue`
const C1 = '1'.repeat(40)
const C2 = '2'.repeat(40)
const D1 = 'a'.repeat(64)
const D2 = 'b'.repeat(64)
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

const INSTALLED: Json = {
  install_id: INSTALL, name: 'example-plugin', display_name: null, scope: 'workspace', tenant_id: TENANT, state: 'active',
  source: { kind: 'github', repository: 'example-org/example-repo', ref: 'v0.2.0', commit: C1 },
  current_version: 'v-000000000001', version: '0.2.0', description: 'Checks each turn before it ends',
  runnable_sha256: D1, hosts: ['api.example.com'], components: { skills: 1, commands: 1, agents: 0, hooks: 1, mcp_servers: 1 },
  removable: true, updatable: true, updated_at: '2026-11-06T01:42:00+00:00', updated_by: 'admin@example.com',
}

const VERSIONS: Json = {
  install: INSTALLED,
  versions: [version('v-000000000001', '0.2.0', C1, D1, '2026-11-06T01:42:00+00:00', true, [STOP]), version('v-000000000002', '0.3.0', C2, D2, '2026-11-07T03:10:00+00:00', false, [STOP, GUARD])],
}

const NONE = { added: [], removed: [] }
const DIFF: Json = {
  install_id: INSTALL,
  diff_sha256: 'c'.repeat(64),
  diff: {
    from_version_id: 'v-000000000001', to_version_id: 'v-000000000002',
    manifest: { version: { from: '0.2.0', to: '0.3.0' } },
    hooks: { added: [GUARD], removed: [], changed: [] },
    mcp_servers: { ...NONE, changed: [] }, skills: { added: ['plan-turn'], removed: [] }, commands: NONE, agents: NONE,
    settings: { added: ['max_retries'], removed: [], retyped: [], newly_sensitive: [] },
    hosts: NONE, runtimes: null, files: { from: 12, to: 13 }, unpacked_bytes: { from: 131_072, to: 140_000 },
    runnable_sha256: { from: D1, to: D2 },
    source: { from_commit: C1, to_commit: C2, compare: `https://github.com/example-org/example-repo/compare/${C1}...${C2}` },
    marked: ['hooks'], needs_confirmation: true,
  },
}

const USAGE: Json = {
  install_id: INSTALL,
  workspaces: [],
  workflows: [{ tenant_id: TENANT, id: 'wf-1', name: 'nightly-check', kind: 'workflow', steps: ['review', 'verify'], how: 'added' }],
  schedules: [{ tenant_id: TENANT, schedule_id: 'sch-1', name: 'every-night' }],
  steps: [{ tenant_id: TENANT, task_id: 'task-77', workflow_id: 'wf-1', step: 'review', state: 'RUNNING' }],
}

function pluginReads(m: string, url: string): Reply {
  if (m !== 'GET') return { status: 409, body: { code: 'PLUGIN_IN_USE', message: 'example-plugin is used by nightly-check.' } }
  if (url.startsWith(`${BASE}/installs/${INSTALL}/versions`)) return { status: 200, body: VERSIONS }
  if (url.startsWith(`${BASE}/installs/${INSTALL}/diff`)) return { status: 200, body: DIFF }
  if (url.startsWith(`${BASE}/installs/${INSTALL}/usage`)) return { status: 200, body: USAGE }
  if (url.startsWith(`/v1/workspaces/${TENANT}/plugin-defaults`)) return { status: 200, body: { tenant_id: TENANT, defaults: [INSTALL], can_edit: true } }
  if (url === BASE || url.startsWith(`${BASE}?`)) {
    return { status: 200, body: { scope: 'workspace', tenant_id: TENANT, built_in: [], platform_installs: [], installs: [INSTALLED], limits: {}, can_install: true } }
  }
  return null
}

/** A workspace plugin's page, a marked update waiting: Remove and Update each open a typed confirm. */
export async function mountPluginDetail(): Promise<ReactElement> {
  await live()
  serve(pluginReads)
  const { PluginDetailScreen } = await import('../PluginDetail')
  return <PluginDetailScreen installId={INSTALL} tenantId={TENANT} backHref="/config/plugins" />
}

// ---------------------------------------------------------------- the setup strip, finished

const json = (body: unknown, status = 200): Response => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
const step = (name: string, state: string, evidence: Json = {}): Json => ({ step: name, state, code: null, copy: null, checked_at: null, evidence, issues: [] })
const SOURCES = { mcp_servers: { needed: 0, reason: null }, qa_targets: { needed: 0, reason: null }, provider_key: { needed: 0, reason: null } }

/** The App on Config › GitHub access, every setup step done: the strip is one line and Show steps. */
export async function mountSetupStrip(): Promise<ReactElement> {
  await live()
  const me = {
    tenant: { tenant_id: 'eng', kind: 'group', display_name: 'eng@example.com' },
    principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: false },
    environment: 'dev',
    environment_declared: true,
  }
  const mine = [{ tenant_id: 'eng', display_name: 'eng@example.com', kind: 'group', via: 'member', role: { name: 'admin', source: 'override' } }]
  const doc = {
    tenant_id: 'eng', user: 'ana.b@example.com', user_hash: 'h',
    steps: [step('github_connected', 'done', { login: 'ana' }), step('repos_chosen', 'done', { repositories: [{ full_name: 'acme/api' }] })],
    next_step: null, complete: false, source: 'derived', derived_at: new Date(Date.now() - 5 * 60_000).toISOString(),
    config_setup: {
      secrets: { needed: 0, done: true, items: [], sources: SOURCES },
      people: { applies: true, done: true, holders: 5, admins: 1, pending: null, reason: null, pending_reason: null },
    },
  }
  globalThis.fetch = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/v1/tenants/mine')) return Promise.resolve(json(mine))
    if (url.includes('/v1/tenants/me')) return Promise.resolve(json(me))
    if (url.includes('/v1/onboarding')) return Promise.resolve(json(doc))
    return Promise.resolve(json({ detail: 'Not Found' }, 404))
  }) as unknown as typeof fetch
  window.location.hash = ''
  window.history.replaceState(null, '', '/config/github')
  const { App } = await import('../App')
  return <App />
}

// ---------------------------------------------------------------- a write grant's push test

const OK = { state: 'ok', code: null, checked_at: null }
const WRITE_GRANT = {
  repo_id: `repo_${'2'.padStart(16, '0')}`, repository: 'example-org/example-service', owner: 'example-org', mode: 'write',
  can_push: true, archived: false, granted_at: null, granted_by: 'dev@swarm.example.com',
  checks: { clone: OK, push: OK, pull_request: OK }, verified_at: null,
}
const ORG = {
  owner: 'example-org', method: 'app', owner_type: 'Organization', installation_id: 7, repository_selection: 'selected',
  install_state: 'installed', sso: 'ok', enabled: true, enabled_at: null, requested_at: null, checked_at: null,
}
const CONNECTION = {
  connection_id: 'github:eng:abc', forge: 'github', method: 'app_user', forge_login: 'octo-dev', forge_user_id: 42, token_id: 'tok_u',
  secret_name: 'swarm-tenant-eng-git-u-89abcdef01234567', state: 'active', failure: null,
  access_expires_at: '2026-12-01T00:00:00Z', refresh_expires_at: null, refreshed_at: '2026-11-25T00:00:00Z', refreshing: false,
  created_at: '2026-11-01T00:00:00Z', connected_at: '2026-11-01T00:00:00Z', revoked_at: null,
}

/** The repositories a person may reach, one write grant: Push test asks first; a refused verify names its error. */
export async function mountPushTest(): Promise<ReactElement> {
  await live()
  serve((m, url) => {
    if (m === 'GET' && url === '/v1/access') return { status: 200, body: { tenant_id: 'eng', connection: CONNECTION, orgs: [ORG], requested: [], grants: [WRITE_GRANT] } }
    if (m === 'POST' && url.endsWith('/verify')) return { status: 409, body: { code: 'GRANT_BUSY', message: 'Another check is running on this grant.' } }
    return null
  })
  const { ReachableRepositoriesRead } = await import('../ReachableRepositories')
  return <ReachableRepositoriesRead />
}

// ---------------------------------------------------------------- a step's plugin sheets

const PROFILES = ['claude-code', 'claude-code-large']
const ADDED_ID = 'w-0a1b2c3d4e5f'
const DEFAULT_ID = 'p-1a2b3c4d5e6f'

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })

const VIEW: PluginsView = {
  tenant_id: TENANT, plugins: { allowed: ['gstack'] }, defaults: { plugins: { allowed: [] } },
  set_at: null, set_by: null, can_edit: false,
  catalogue: [{ name: 'gstack', version: '1.0.0', runner_profiles: PROFILES, modes: { unattended: { available: true, skills: ['review', 'health'] } } }],
}

function install(id: string, name: string, scope: 'platform' | 'workspace'): ManagedPluginInstall {
  return {
    install_id: id, name, display_name: name, scope, tenant_id: scope === 'workspace' ? TENANT : null, built_in: false,
    state: 'active', current_version: `v-${id.slice(2)}`, version: '1.0.0', description: null, hosts: [],
    components: { hooks: 1 }, allowed: true,
  }
}

const MANAGED: ManagedPluginsRead = {
  catalogue: {
    scope: 'workspace', tenant_id: TENANT, built_in: [], can_install: false,
    platform_installs: [install(DEFAULT_ID, 'rules-gate', 'platform')],
    installs: [install(ADDED_ID, 'lint-notes', 'workspace')],
  },
  defaults: [DEFAULT_ID],
}

const FORM: PluginConfigView = {
  tenant_id: TENANT, missing: [], can_edit: false,
  form: { install_id: DEFAULT_ID, version_id: `v-${DEFAULT_ID.slice(2)}`, fields: [
    { key: 'strictness', source: 'user_config', type: 'string', kind: 'value', title: 'Strictness level', description: null, required: false, multiple: false, sensitive: false, default: 'normal' },
    { key: 'notes_dir', source: 'user_config', type: 'directory', kind: 'path', title: 'Notes directory', description: null, required: false, multiple: false, sensitive: false, default: null },
  ] },
  values: { strictness: { set: true, value: 'normal' }, notes_dir: { set: false } },
}

/** A step's plugin row on a runner that loads plugins: Add a plugin and Plugins… each open a sheet. */
export async function mountStepPlugins(): Promise<ReactElement> {
  const { StepPluginRow } = await import('../PluginSheet')
  const ctx = { view: ok(VIEW), managed: ok(MANAGED), forms: { [DEFAULT_ID]: ok(FORM) }, want: () => undefined, wantForm: () => undefined }
  return <StepPluginRow stepId="review-1" profile="claude-code" pick={null} managed={null} ctx={ctx} onPick={() => undefined} onManaged={() => undefined} />
}

// ---------------------------------------------------------------- a group workspace's roles

/** The App on the fixture build, its roles read answered as a group workspace's: the default-role card's fold is drawn. */
export async function mountGroupRoles(): Promise<ReactElement> {
  vi.resetModules()
  vi.doMock('../api', async (importOriginal) => {
    const real = await importOriginal<typeof import('../api')>()
    return {
      ...real,
      loadWorkspaceRoles: async () => {
        const r = await real.loadWorkspaceRoles()
        return r.status === 'ok' ? { ...r, data: { ...r.data, kind: 'group', default_role: 'member' } } : r
      },
    }
  })
  window.history.replaceState(null, '', '/')
  window.location.hash = '#config/workspace'
  const { App } = await import('../App')
  return <App />
}
