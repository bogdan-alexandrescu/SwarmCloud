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
 *     count the page cannot know says "not known", never 0.
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

function serve(me: Json, mine: Json[] | 'empty', setups: Readonly<Record<string, Json | number>>) {
  globalThis.fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
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

  it('lists for a workspace admin only the workspaces they administer, each with its Config links', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'admin'), group('ops', 'member'), group('lab', 'admin')], { eng: setup('eng', 4), lab: setup('lab', 2) })
    await page()
    await waitFor(() => expect(measuredRows()).toHaveLength(2))
    // MUTATION: list every group workspace for a workspace admin -> ops appears and this fails.
    expect(ids()).toEqual(['lab', 'eng'])
    expect(rows().map((r) => r.querySelector('.wya-links')?.getAttribute('aria-label'))).toEqual(['lab settings', 'eng settings'])
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
    expect([...counts.querySelectorAll('[role="listitem"]')].map((c) => c.textContent)).toEqual(['3 group workspaces', 'not known set up', 'not known behind', 'not known requests wait in People and roles'])
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
    // Until the router knows the address, the click is a plain link, never a route to Overview.
    fireEvent.click(lab!.querySelector('.wya-name a')!)
    expect(go).not.toHaveBeenCalled()
  })
})

describe('the list\'s rules, as functions', () => {
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
