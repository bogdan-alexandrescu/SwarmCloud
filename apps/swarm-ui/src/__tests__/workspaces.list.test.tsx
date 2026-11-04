/**
 * ADMIN › WORKSPACES (HOME-ADMIN, part of #2688): the place mock-up's
 * Workspaces list, built on the reads Workspaces you admin made.
 *
 *   * per role: every group workspace for a platform admin (reach entries
 *     too), those they administer for a workspace admin, their own for a
 *     member (read only: no Config links, no lead, no Add a new workspace);
 *   * furthest behind on setup first, a setup not read after every measured
 *     one, and A to Z on the switch;
 *   * each row links to its workspace's page and says what waits in it; a
 *     count the page cannot know says "not known", never 0;
 *   * each row's roster facts are read on the list (the owner's pick,
 *     2026-11-03): roles for every row, who joined by request only where the
 *     caller administers it, the admin route for a platform admin.
 */
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

type Json = Record<string, unknown>

const ME = (isAdmin: boolean): Json => ({
  tenant: { tenant_id: 'u-ana', kind: 'user', display_name: 'Your workspace' },
  principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: isAdmin },
  environment: 'dev',
  environment_declared: true,
})
const PERSONAL: Json = { tenant_id: 'u-ana', display_name: 'ana.b@example.com', kind: 'user', via: 'personal', role: { name: 'admin', source: 'personal' } }
const group = (id: string, role: string, extra: Json = {}): Json => ({
  tenant_id: id,
  display_name: `${id}@example.com`,
  kind: 'group',
  via: 'member',
  role: { name: role, source: 'override' },
  ...extra,
})

const step = (name: string, state: string, evidence: Json = {}): Json => ({ step: name, state, code: null, copy: null, checked_at: null, evidence, issues: [] })
const SECRETS_NONE: Json = { needed: 0, done: true, items: [], sources: { mcp_servers: { needed: 0, reason: null }, qa_targets: { needed: 0, reason: null }, provider_key: { needed: 0, reason: null } } }
const PEOPLE = (done: boolean): Json => ({ applies: true, done, holders: done ? 2 : 0, admins: 0, pending: null, reason: null, pending_reason: 'n' })

/** A group workspace's setup read with `done` of its four steps done (GitHub, repositories, secrets, people). */
const setup = (tenant: string, done: 1 | 2 | 3 | 4, github = 'done'): Json => ({
  tenant_id: tenant,
  user: 'ana.b@example.com',
  user_hash: 'h',
  derived_at: '2026-10-25T09:00:00Z',
  steps: [step('github_connected', github, { login: 'ana' }), step('repos_chosen', done >= 2 ? 'done' : 'todo', done >= 2 ? { repositories: [{ full_name: 'acme/api' }] } : {})],
  next_step: null,
  complete: false,
  source: 'derived',
  config_setup: { secrets: done >= 3 ? SECRETS_NONE : { ...SECRETS_NONE, needed: 1, done: false }, people: PEOPLE(done >= 4) },
})

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

const grant = (email: string, role: string): Json => ({ email, role, source: 'override', granted_by: 'ana.b@example.com', granted_at: '2026-10-20T09:00:00Z', granted_via: 'console', reason: '' })
const roster = (tenant: string, people: Json[], defaultRole = 'member'): Json => ({ tenant_id: tenant, kind: 'group', default_role: defaultRole, default_source: 'grant', people, roles: [], audit: [] })
const joinedBy = (email: string, extra: Json = {}): Json => ({ email, by: 'ana.b@example.com', at: '2026-10-21T09:00:00Z', via: 'join_request', group_changed: false, still_in_group: false, ...extra })

type Named = Readonly<Record<string, Json | number>>
const named = (table: Named | undefined, tenant: string) => {
  const answer = table?.[tenant]
  if (answer === undefined) return json({ code: 'tenant_not_member', message: 'not a member' }, 403)
  return typeof answer === 'number' ? json({ detail: 'Internal Server Error' }, answer) : json(answer)
}

function serve(me: Json, mine: Json[] | 'empty', setups: Named, people: { roles?: Named; members?: Named; adminRoles?: Named } = {}) {
  globalThis.fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const tenantOf = () => ((init?.headers ?? {}) as Record<string, string>)['X-Swarm-Tenant'] ?? ''
    const admin = /\/v1\/admin\/workspaces\/([^/?]+)\/roles/.exec(url)
    if (admin !== null) return Promise.resolve(named(people.adminRoles, decodeURIComponent(admin[1]!)))
    if (url.includes('/v1/workspace/roles')) return Promise.resolve(named(people.roles, tenantOf()))
    if (url.includes('/v1/workspace/members')) return Promise.resolve(named(people.members, tenantOf()))
    if (url.includes('/v1/tenants/mine')) return Promise.resolve(json(mine === 'empty' ? [] : mine))
    if (url.includes('/v1/tenants/me')) return Promise.resolve(json(me))
    if (url.includes('/v1/onboarding')) {
      const tenant = ((init?.headers ?? {}) as Record<string, string>)['X-Swarm-Tenant'] ?? ''
      const answer = setups[tenant]
      if (answer === undefined) return Promise.resolve(json({ code: 'tenant_not_member', message: 'not a member' }, 403))
      return Promise.resolve(typeof answer === 'number' ? json({ detail: 'Internal Server Error' }, answer) : json(answer))
    }
    return Promise.resolve(json({ detail: 'Not Found' }, 404))
  }) as unknown as typeof fetch
}

const go = vi.fn()
async function page() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { WorkspacesYouAdminScreen } = await import('../WorkspacesYouAdmin')
  render(<WorkspacesYouAdminScreen go={go} />)
}

const rows = () => [...document.querySelectorAll<HTMLElement>('.wya-row:not(.is-personal)')]
const ids = () => rows().map((r) => r.getAttribute('data-tenant'))
const measuredRows = () => rows().filter((r) => r.querySelector('.cfg-progress') !== null || r.querySelector('.wya-setup .wya-reading')?.textContent?.startsWith('Setup not read'))

beforeAll(async () => {
  await import('../WorkspacesYouAdmin')
}, 120_000)

afterEach(() => {
  cleanup()
  go.mockReset()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('Admin › Workspaces lists by role', () => {
  it('lists every group workspace for a platform admin, reach entries too, furthest behind first', async () => {
    serve(ME(true), [PERSONAL, group('eng', 'admin', { role: { name: 'admin', source: 'platform_admin' } }), group('far', 'admin', { via: 'platform_admin', role: { name: 'admin', source: 'platform_admin' } }), group('ops', 'member')], {
      eng: setup('eng', 3),
      far: setup('far', 1),
      ops: setup('ops', 4),
    })
    await page()
    // MUTATION: list only the administered, non-reach entries (today's rule) -> [eng] and this fails.
    await waitFor(() => expect(measuredRows()).toHaveLength(3))
    // MUTATION: keep mine's order -> eng, far, ops and this fails.
    expect(ids()).toEqual(['far', 'eng', 'ops'])
    for (const r of rows()) expect(r.querySelector('[data-via="platform_admin"]')?.textContent).toBe('admin as a platform admin')
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Workspaces')
    expect(screen.getByRole('link', { name: 'Add a new workspace' }).getAttribute('href')).toBe('/config/workspace?part=new')
    const lead = screen.getByRole('region', { name: 'Your workspaces' })
    expect(lead.querySelector('b')?.textContent).toBe('far is furthest behind')
    // The personal workspace leads the list, its own and nobody else's.
    const personal = document.querySelector('.wya-row.is-personal')!
    expect(personal.getAttribute('data-tenant')).toBe('u-ana')
    expect(personal.querySelector('a')?.getAttribute('href')).toBe('/admin/workspaces/u-ana')
  })

  it('lists for a workspace admin only the workspaces they administer, each opening its page, with no interim Config links', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'admin'), group('ops', 'member'), group('lab', 'admin')], { eng: setup('eng', 4), lab: setup('lab', 2) })
    await page()
    await waitFor(() => expect(measuredRows()).toHaveLength(2))
    // MUTATION: list every group workspace for a workspace admin -> ops appears and this fails.
    expect(ids()).toEqual(['lab', 'eng'])
    // MUTATION: keep the interim per-row Config links -> a row has a .wya-links and this fails.
    expect(rows().filter((r) => r.querySelector('.wya-links') !== null)).toHaveLength(0)
    expect(rows().map((r) => r.querySelector('.wya-name a')?.getAttribute('href'))).toEqual(['/admin/workspaces/lab', '/admin/workspaces/eng'])
    expect(rows()[0]!.querySelector('[data-role]')?.textContent).toBe('your role: admin')
    expect(screen.queryByRole('link', { name: 'Add a new workspace' })).toBeNull()
  })

  it('lists a member their own group workspaces, read only', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'member'), group('ops', 'viewer')], { eng: setup('eng', 4), ops: setup('ops', 1) })
    await page()
    // MUTATION: list only administered workspaces -> the empty state and this fails.
    await waitFor(() => expect(measuredRows()).toHaveLength(2))
    expect(ids()).toEqual(['ops', 'eng'])
    // MUTATION: draw the Config links for every row -> fails.
    expect(document.querySelectorAll('.wya-links[aria-label$="settings"]')).toHaveLength(0)
    expect(screen.queryByRole('region', { name: 'Your workspaces' })).toBeNull()
    expect(screen.queryByRole('link', { name: 'Add a new workspace' })).toBeNull()
    expect(rows().map((r) => r.querySelector('[data-role]')?.textContent)).toEqual(['your role: viewer', 'your role: member'])
    expect(document.querySelector('.wya-page .ur-sub')?.textContent).toContain('read only')
  })

  it('says a platform admin has no group workspace yet, rather than that they administer none', async () => {
    serve(ME(true), [PERSONAL], {})
    await page()
    await waitFor(() => expect(document.querySelector('.wya-page .c-emp h3')?.textContent).toBe('No group workspace yet'))
  })
})

describe('Admin › Workspaces orders and counts', () => {
  it('puts a workspace whose setup was not read after every measured one, and counts behind as not known', async () => {
    serve(ME(false), [PERSONAL, group('bad', 'admin'), group('eng', 'admin'), group('lab', 'admin')], { bad: 500, eng: setup('eng', 4), lab: setup('lab', 3) })
    await page()
    await waitFor(() => expect(measuredRows()).toHaveLength(3))
    // MUTATION: count an unread setup as 0 done -> bad first and this fails.
    expect(ids()).toEqual(['lab', 'eng', 'bad'])
    const counts = screen.getByRole('list', { name: 'Workspaces, counted' })
    // MUTATION: count the unread one as not set up -> "1 behind" and this fails.
    expect([...counts.querySelectorAll('[role="listitem"]')].map((c) => c.textContent)).toEqual(['3 group workspaces', 'not known set up', 'not known behind', 'not known requests wait in Platform people'])
    expect(document.querySelector('.wya-row[data-tenant="bad"] .wya-waits')?.textContent).toBe('not known')
  })

  it('orders A to Z on the switch', async () => {
    serve(ME(false), [PERSONAL, group('beta', 'admin'), group('alpha', 'admin'), group('zed', 'admin')], { beta: setup('beta', 4), alpha: setup('alpha', 4), zed: setup('zed', 1) })
    await page()
    await waitFor(() => expect(measuredRows()).toHaveLength(3))
    expect(ids()).toEqual(['zed', 'alpha', 'beta'])
    fireEvent.click(screen.getByRole('button', { name: 'A to Z' }))
    // MUTATION: ignore the switch -> zed stays first and this fails.
    expect(ids()).toEqual(['alpha', 'beta', 'zed'])
  })

  it('links each row to its workspace page and says what waits in it', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'admin'), group('lab', 'admin')], { eng: setup('eng', 4), lab: setup('lab', 1, 'failed') })
    await page()
    await waitFor(() => expect(measuredRows()).toHaveLength(2))
    const [lab, eng] = rows()
    expect(lab!.querySelector('.wya-name a')?.getAttribute('href')).toBe('/admin/workspaces/lab')
    // MUTATION: say nothing waits while a step failed -> fails.
    expect(lab!.querySelector('.wya-waits')?.textContent).toBe('GitHub connected: failed')
    expect(eng!.querySelector('.wya-waits')?.textContent).toBe('nothing waits')
    // The router knows the address: the click routes in place to the workspace's page.
    fireEvent.click(lab!.querySelector('.wya-name a')!)
    // MUTATION: follow it as a plain link (the interim guard) -> go is never called and this fails.
    expect(go).toHaveBeenCalledWith('admin/workspaces/lab')
  })
})

describe('Admin › Workspaces reads each workspace\'s roster', () => {
  const calls = (path: string) => (globalThis.fetch as unknown as { mock: { calls: [RequestInfo | URL, RequestInit | undefined][] } }).mock.calls
    .filter(([u]) => String(u).includes(path))
    .map(([, init]) => ((init?.headers ?? {}) as Record<string, string>)['X-Swarm-Tenant'])
  const facts = (r: HTMLElement) => [...r.querySelectorAll('.wya-facts .c-chip')].map((c) => c.textContent)

  it('draws the default role, own roles, admins, a role waiting for its person and who joined by request', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'admin')], { eng: setup('eng', 4) }, {
      roles: { eng: roster('eng', [grant('ana.b@example.com', 'admin'), grant('bo@example.com', 'member'), grant('cy@example.com', 'viewer')]) },
      members: { eng: { platform_memberships: true, members: [joinedBy('cy@example.com', { group_changed: true }), joinedBy('ed@example.com')] } },
    })
    await page()
    await waitFor(() => expect(document.querySelector('.wya-row[data-tenant="eng"] .wya-facts .c-chip')).not.toBeNull())
    // MUTATION: drop the roster read (the branch before the pick) -> no facts and this fails.
    expect(facts(rows()[0]!)).toEqual(['default role: member', '3 with a role of their own', '1 admin', '1 role waits for its person', '2 let in by request'])
    expect(calls('/v1/workspace/roles')).toEqual(['eng'])
    expect(calls('/v1/workspace/members')).toEqual(['eng'])
  })

  it('reads a member\'s rows for their roles only: who joined by request is admin only', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'member'), group('ops', 'viewer')], { eng: setup('eng', 4), ops: setup('ops', 2) }, {
      roles: { eng: roster('eng', [grant('dee@example.com', 'admin')], 'viewer'), ops: roster('ops', []) },
    })
    await page()
    await waitFor(() => expect(rows().every((r) => r.querySelector('.wya-facts .c-chip') !== null)).toBe(true))
    const eng = document.querySelector<HTMLElement>('.wya-row[data-tenant="eng"]')!
    expect(facts(eng)).toEqual(['default role: viewer', '1 with a role of their own', '1 admin'])
    expect(calls('/v1/workspace/roles').sort()).toEqual(['eng', 'ops'])
    // MUTATION: ask for every row's members -> both are asked and refused, and this fails.
    expect(calls('/v1/workspace/members')).toEqual([])
  })

  it('says a roster it could not read is not known, never 0', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'admin')], { eng: setup('eng', 4) }, { roles: { eng: 500 }, members: { eng: 500 } })
    await page()
    await waitFor(() => expect(document.querySelector('.wya-row[data-tenant="eng"] .wya-facts .c-chip')).not.toBeNull())
    // MUTATION: draw an unread roster as an empty one -> "0 admins" and this fails.
    expect(facts(rows()[0]!)).toEqual(['roles not known'])
  })

  it('reads a platform admin\'s rosters on the admin route, which reaches a workspace they are not in', async () => {
    serve(ME(true), [PERSONAL, group('far', 'admin', { via: 'platform_admin', role: { name: 'admin', source: 'platform_admin' } })], { far: setup('far', 1) }, {
      adminRoles: { far: roster('far', []) },
    })
    await page()
    await waitFor(() => expect(document.querySelector('.wya-row[data-tenant="far"] .wya-facts .c-chip')).not.toBeNull())
    // MUTATION: read the workspace route for a reach entry -> refused, "roles not known" and this fails.
    expect(facts(rows()[0]!).slice(0, 3)).toEqual(['default role: member', '0 with a role of their own', '0 admins'])
    expect(calls('/v1/workspace/roles')).toEqual([])
  })
})

describe('the list\'s rules, as functions', () => {
  it('says the roster facts in the frame\'s words', async () => {
    const { rosterFacts } = await import('../WorkspacesYouAdmin')
    const r = { tenant_id: 'eng', kind: 'group' as const, default_role: null, default_source: null, people: [], roles: [], audit: [] }
    expect(rosterFacts(null, undefined)).toEqual([{ text: 'roles not known', tone: null }])
    expect(rosterFacts(r, null).map((f) => f.text)).toEqual(['default role: not known', '0 with a role of their own', '0 admins', 'let in by request: not known'])
    expect(rosterFacts(r, undefined).map((f) => f.text)).toHaveLength(3)
  })

  it('orders furthest behind first, unread last, ties by name', async () => {
    const { orderWorkspaces } = await import('../WorkspacesYouAdmin')
    const s = (n: number) => Array.from({ length: 4 }, (_, i) => ({ key: 'github' as const, label: 'x', state: i < n ? ('done' as const) : ('todo' as const), line: '', to: '' }))
    const out = orderWorkspaces(
      [
        { name: 'c', steps: null },
        { name: 'b', steps: s(2) },
        { name: 'a', steps: s(2) },
        { name: 'd', steps: s(0) },
      ],
      'behind',
    )
    expect(out.map((r) => r.name)).toEqual(['d', 'a', 'b', 'c'])
  })
})
