/**
 * THE ADMIN AND CONFIG PANELS, FINISHED (CONFIG-ADMIN-POLISH; part of #2157,
 * BGD's pick of 2026-10-24, proposal E, docs/web-ui/mockups/config-e.html
 * S1a-S1g, S10a).
 *
 *   * Each Admin row carries its served count (S1b): People and roles the
 *     feed's `admin_pending`, Workspaces you admin the `mine` read's count,
 *     Pool limits "N near ceiling" from the capacity read, Tenants "N waiting"
 *     and Platform secrets "N" from the feed. A count that was not read draws
 *     no badge, never 0. A workspace admin is drawn only their own (S1c).
 *   * A personal workspace's Config panel says why it has no People and
 *     roles (S1e); the panel foot says the person's role, not the email (S1a).
 *   * Workspaces you admin leads with the workspace furthest behind and one
 *     button into its Config, and its links are the Config tabs' (S10a).
 *   * No source in this lane's files still names Settings › API keys or the
 *     settings/people address.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

type Json = Record<string, unknown>

const EMAIL = 'ana.b@example.com'
const ME = (isAdmin: boolean, tenant = 'eng'): Json => ({
  tenant: { tenant_id: tenant, kind: tenant.startsWith('u-') ? 'user' : 'group', display_name: `${tenant}@example.com` },
  principal: { email: EMAIL, domain: 'example.com', groups: [], is_admin: isAdmin },
  environment: 'dev',
  environment_declared: true,
})
const PERSONAL: Json = { tenant_id: 'u-ana', display_name: 'ana.b@example.com', kind: 'user', via: 'personal', role: { name: 'admin', source: 'personal' } }
const group = (id: string, role: string): Json => ({ tenant_id: id, display_name: `${id}@example.com`, kind: 'group', via: 'member', role: { name: role, source: 'override' } })

const FEED = (extra: Json = {}): Json => ({ items: [], unread_needs_you: 0, unread_updates: 0, admin_pending: 0, read_at: null, muted: { needs_you: 0, updates: 0 }, partial: false, ...extra })
const pool = (name: string, active: number, limit: number | null, enabled = true): Json => ({
  name,
  hard_limit: limit,
  adaptive_target: null,
  quota_derived_limit: null,
  effective_limit: limit,
  active,
  available: limit === null ? null : limit - active,
  enabled,
})
const CAPACITY = (pools: Json[]): Json => ({ pools, read_at: '2026-10-25T09:00:00Z' })

const step = (name: string, state: string, evidence: Json = {}): Json => ({ step: name, state, code: null, copy: null, checked_at: null, evidence, issues: [] })
const REPOS = ['acme/api', 'acme/web'].map((full_name) => ({ full_name }))
const SECRETS_NONE: Json = { needed: 0, done: true, items: [], sources: { mcp_servers: { needed: 0, reason: null }, qa_targets: { needed: 0, reason: null }, provider_key: { needed: 0, reason: null } } }
const PEOPLE = (done: boolean, holders: number): Json => ({ applies: true, done, holders, admins: 0, pending: null, reason: null, pending_reason: 'n' })
const onboarding = (repos: 'done' | 'todo', tenant: string, people: Json): Json => ({
  tenant_id: tenant,
  user: EMAIL,
  user_hash: 'h',
  derived_at: '2026-10-25T09:00:00Z',
  steps: [step('github_connected', 'done', { login: 'ana' }), step('repos_chosen', repos, repos === 'done' ? { repositories: REPOS } : {})],
  next_step: null,
  complete: false,
  source: 'derived',
  config_setup: { secrets: SECRETS_NONE, people },
})

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

/** The frame's reads. A number answers that status; `feed` and `capacity` default to a 404 (an API without them). */
function serve(me: Json, mine: Json[] | number, opts: { feed?: Json | number; capacity?: Json | number; setups?: Readonly<Record<string, Json>> } = {}) {
  const answer = (v: Json | Json[] | number | undefined) => (v === undefined ? json({ detail: 'Not Found' }, 404) : typeof v === 'number' ? json({ detail: 'Internal Server Error' }, v) : json(v))
  globalThis.fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/v1/notifications')) return Promise.resolve(answer(opts.feed))
    if (url.includes('/v1/capacity')) return Promise.resolve(answer(opts.capacity))
    if (url.includes('/v1/tenants/mine')) return Promise.resolve(answer(mine))
    if (url.includes('/v1/tenants/me')) return Promise.resolve(json(me))
    if (url.includes('/v1/onboarding')) {
      const headers = (init?.headers ?? {}) as Record<string, string>
      const tenant = headers['X-Swarm-Tenant'] ?? String((me.tenant as Json).tenant_id)
      return Promise.resolve(answer(opts.setups?.[tenant] ?? 403))
    }
    return Promise.resolve(json({ detail: 'Not Found' }, 404))
  }) as unknown as typeof fetch
}

async function app(path: string) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const mod = await import('../App')
  window.history.replaceState(null, '', path)
  render(<mod.App />)
  return mod
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  localStorage.clear()
  sessionStorage.clear()
  window.history.replaceState(null, '', '/')
})

/** The panel row named `label`. */
const row = (label: string) => [...document.querySelectorAll('.sk-panel .sk-pk')].find((r) => r.querySelector('.sk-pl')?.textContent === label) ?? null
/** The count drawn on the row named `label`, or null for none. */
const count = (label: string) => row(label)?.querySelector('.sk-cnt')?.textContent ?? null

describe('the Admin panel\'s badges (S1b)', () => {
  it('draws each Admin row\'s served count', async () => {
    serve(ME(true), [PERSONAL, group('eng', 'admin'), group('ops', 'admin'), group('far', 'member')], {
      feed: FEED({ admin_pending: 2, tenants_waiting: 1, platform_secrets_unset: 3 }),
      // 46 of 50 is 92%, 18 of 30 is 60%, a paused pool is never ranked.
      capacity: CAPACITY([pool('global', 46, 50), pool('tenant:eng', 18, 30), pool('resource:browser', 12, 12, false)]),
    })
    // Platform secrets' page, not Pool limits': the panel is the subject, and it reads no capacity of its own.
    await app('/admin/platform-secrets')
    // MUTATION: draw no badge on the Admin rows -> every line below fails.
    await waitFor(() => expect(count('Tenants')).toBe('1 waiting'))
    await waitFor(() => expect(count('Pool limits')).toBe('1 near ceiling'))
    expect(count('People and roles')).toBe('2')
    expect(count('Workspaces you admin')).toBe('2')
    expect(count('Platform secrets')).toBe('3')
    // Something waiting is amber; how many workspaces you admin is only a count.
    expect(row('Tenants')?.querySelector('.sk-cnt')?.className).toBe('sk-cnt is-alert')
    expect(row('Workspaces you admin')?.querySelector('.sk-cnt')?.className).toBe('sk-cnt')
    // Nothing counts Runner sizes or Platform counts.
    expect(count('Runner sizes')).toBeNull()
    expect(count('Platform counts')).toBeNull()
  })

  it('draws no badge for a count that was not read, never 0', async () => {
    // An API whose feed serves neither new count (an older one), a capacity read that failed, and nothing waiting.
    serve(ME(true), [PERSONAL, group('eng', 'admin')], { feed: FEED({ tenants_waiting: null }), capacity: 500 })
    await app('/admin/limits')
    await waitFor(() => expect(count('Workspaces you admin')).toBe('1'))
    const asked = () => vi.mocked(globalThis.fetch).mock.calls.map((c) => String(c[0]))
    await waitFor(() => expect(asked().some((u) => u.includes('/v1/capacity')) && asked().some((u) => u.includes('/v1/notifications'))).toBe(true))
    await new Promise((r) => setTimeout(r, 50))
    // MUTATION: read a null or absent count as 0 -> a "0" badge and this fails.
    for (const label of ['People and roles', 'Pool limits', 'Tenants', 'Platform secrets']) expect(count(label), label).toBeNull()
  })

  it('draws no Workspaces you admin badge when the list could not be read', async () => {
    serve(ME(true), 500, { feed: FEED({ tenants_waiting: 2 }) })
    await app('/admin/limits')
    await waitFor(() => expect(count('Tenants')).toBe('2 waiting'))
    // MUTATION: count an unread list as 0 workspaces -> "0" and this fails.
    expect(count('Workspaces you admin')).toBeNull()
  })

  it('draws a workspace admin only their own badges (S1c)', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'admin'), group('ops', 'admin')], {
      // Whatever a feed says of the platform, it is not this person's to see.
      feed: FEED({ admin_pending: 4, tenants_waiting: 2, platform_secrets_unset: 1 }),
      capacity: CAPACITY([pool('global', 50, 50)]),
    })
    await app('/admin/workspaces')
    await waitFor(() => expect(count('Workspaces you admin')).toBe('2'))
    // MUTATION: hand a workspace admin the platform's request count -> "4" and this fails.
    expect(count('People and roles')).toBeNull()
    expect([...document.querySelectorAll('.sk-panel [aria-label="Your workspaces"] .sk-cnt')].map((c) => c.textContent)).toEqual(['2'])
  })
})

describe('the Config panel in a personal workspace (S1e) and the panel foot (S1a)', () => {
  const NOTE = 'A personal workspace has no People and roles: nobody else is in it.'
  const notes = () => [...document.querySelectorAll('.sk-panel .sk-pnote')].map((n) => n.textContent)

  it('says why a personal workspace has no People and roles', async () => {
    serve(ME(false, 'u-ana'), [PERSONAL])
    await app('/config/github')
    // MUTATION: draw no note -> fails.
    await waitFor(() => expect(notes()).toEqual([NOTE]))
  })

  it('says nothing of it in a group workspace', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'member')])
    await app('/config/github')
    await waitFor(() => expect(row('People and roles')).not.toBeNull())
    // MUTATION: draw the note whatever the workspace -> fails.
    expect(notes()).toEqual([])
  })

  it.each([
    ['a platform admin', ME(true), [PERSONAL, group('eng', 'member')], 'Platform admin'],
    ['a workspace admin', ME(false), [PERSONAL, group('eng', 'member'), group('ops', 'admin')], 'Workspace admin'],
    ['a member', ME(false), [PERSONAL, group('eng', 'member')], 'Member'],
    ['a viewer', ME(false), [PERSONAL, group('eng', 'viewer')], 'Viewer'],
  ])('shows %s their role, not their email, with the email in its title', async (_who, me, mine, role) => {
    serve(me, mine)
    await app('/overview')
    // MUTATION: draw the email on the foot's second line -> fails.
    await waitFor(() => expect(document.querySelector('.sk-pfoot small')?.textContent).toBe(role))
    expect(document.querySelector('.sk-pfoot small')?.getAttribute('title')).toBe(EMAIL)
  })
})

describe('Workspaces you admin\'s lead and links (S10a)', () => {
  it('leads with the workspace furthest behind and one button into its Config', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'admin'), group('ops', 'admin')], {
      setups: { eng: onboarding('done', 'eng', PEOPLE(true, 2)), ops: onboarding('todo', 'ops', PEOPLE(false, 0)) },
    })
    await app('/admin/workspaces')
    const lead = await screen.findByRole('region', { name: 'Your workspaces' })
    expect(lead.querySelector('b')?.textContent).toBe('ops@example.com is behind')
    const buttons = [...lead.querySelectorAll('a, button')]
    // MUTATION: draw the old status line with no button -> fails.
    expect(buttons.map((b) => b.textContent)).toEqual(['Open ops@example.com’s Config'])
    expect(buttons[0]!.getAttribute('href')).toBe('/config/repositories')
    fireEvent.click(buttons[0]!)
    const { chosenTenant } = await import('../fetch')
    // MUTATION: open the page without selecting the workspace -> fails.
    expect(chosenTenant()).toBe('ops')
    await waitFor(() => expect(window.location.pathname).toBe('/config/repositories'))
  })

  it('links each workspace to its Config pages by their tab names', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'admin')], { setups: { eng: onboarding('done', 'eng', PEOPLE(true, 2)) } })
    await app('/admin/workspaces')
    const nav = await screen.findByRole('navigation', { name: 'eng@example.com settings' })
    const links = [...nav.querySelectorAll('a')]
    // MUTATION: keep "Workspace" -> settings/workspace and MCP servers -> settings/mcp-servers -> fails.
    expect(links.map((a) => a.textContent)).toEqual(['Workspace Settings', 'MCP servers', 'People and roles', 'Secrets'])
    expect(links.map((a) => a.getAttribute('href'))).toEqual(['/config/workspace', '/config/mcp-servers', '/config/people', '/config/secrets'])
  })
})

describe('no stale place names in this lane\'s files', () => {
  const FILES = ['BrowserBadges.tsx', 'CodeIntelligence.tsx', 'roles.ts', 'WorkspacesYouAdmin.tsx', 'Spine.tsx']
  it.each(FILES)('%s names neither Settings › API keys nor settings/people', (file) => {
    const src = readFileSync(join(__dirname, '..', file), 'utf8')
    // MUTATION: put either old name back -> fails.
    expect(src).not.toContain('Settings › API keys')
    expect(src).not.toContain('settings/people')
  })

  it('points the workspace roles address at Config › People and roles, as SECTIONS spells it', async () => {
    const { WORKSPACE_PEOPLE_ADDRESS } = await import('../roles')
    expect(WORKSPACE_PEOPLE_ADDRESS).toBe('config/people')
  })
})
