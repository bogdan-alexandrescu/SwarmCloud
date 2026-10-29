/**
 * CONFIG, ONE SECTION FOR SETTING UP A WORKSPACE (CONFIG-NAV; part of #2157,
 * BGD's decisions of 2026-10-23 and his pick of 2026-10-24, proposal E,
 * docs/web-ui/mockups/config-e.html S1a-S1g).
 *
 *   * Config is a section of its own, between Capacity and Admin, with six
 *     pages in BGD's order: GitHub access, Repositories, Secrets, MCP servers,
 *     People and roles, Workspace Settings.
 *   * Work keeps no Repositories, Setup or Access; Settings keeps Profile and
 *     API keys and nothing else.
 *   * Every old address lands on its new one -- paths, the old hash router's
 *     addresses and the addresses the pages themselves still name -- so a
 *     bookmark, an issue link, the plugin's console links and the API's
 *     notification links keep working.
 *   * The workspace secrets one-time link, minted by the API at
 *     `/settings/workspace/secrets?action=add&t=..`, still opens the add form,
 *     and its token still never enters the bar.
 *   * Admin is not drawn for a person who administers nothing, and its
 *     addresses keep their path and say the refusal in place once both
 *     reads say so (admin-shell-23, BGD 2026-10-28, reversing CONFIG-NAV's
 *     redirect to Overview for this case); a workspace
 *     admin who is not a platform admin is sent from a platform Admin page
 *     to Workspaces you admin (ADMIN-REDIRECT, BGD 2026-10-24).
 *   * The setup strip runs across every Config page, measured from
 *     `GET /v1/onboarding`; a step with no read is unknown, never done.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

type Json = Record<string, unknown>

const CONFIG_TABS = ['GitHub access', 'Repositories', 'Secrets', 'MCP servers', 'People and roles', 'Workspace Settings']

const ME = (isAdmin: boolean, tenant = 'eng'): Json => ({
  tenant: { tenant_id: tenant, kind: tenant.startsWith('u-') ? 'user' : 'group', display_name: `${tenant}@example.com` },
  principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: isAdmin },
  environment: 'dev',
  environment_declared: true,
})
const PERSONAL: Json = { tenant_id: 'u-ana', display_name: 'ana.b@example.com', kind: 'user', via: 'personal', role: { name: 'admin', source: 'personal' } }
const group = (id: string, role: string): Json => ({ tenant_id: id, display_name: `${id}@example.com`, kind: 'group', via: 'member', role: { name: role, source: 'override' } })

const step = (name: string, state: string): Json => ({ step: name, state, code: null, copy: null, checked_at: null, evidence: {}, issues: [] })
const ONBOARDING: Json = {
  tenant_id: 'eng',
  user: 'ana.b@example.com',
  user_hash: 'h',
  derived_at: '2026-10-24T09:00:00Z',
  steps: [step('signed_in', 'done'), step('github_connected', 'done'), step('app_installed', 'done'), step('orgs_enabled', 'done'), step('repos_chosen', 'todo'), step('access_verified', 'todo'), step('ready', 'todo')],
  next_step: 'repos_chosen',
  complete: false,
  source: 'derived',
}

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

/** The frame's two reads and the onboarding read, answered as given; `null` holds a read open. Everything else is a 404. */
function serve(me: Json | null, mine: Json[] | null, extra: (url: string) => unknown = () => undefined) {
  globalThis.fetch = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    const more = extra(url)
    if (more !== undefined) return Promise.resolve(json(more))
    const reply = url.includes('/v1/tenants/mine') ? mine : url.includes('/v1/tenants/me') ? me : url.includes('/v1/onboarding') ? ONBOARDING : undefined
    if (reply === undefined) return Promise.resolve(json({ detail: 'Not Found' }, 404))
    if (reply === null) return new Promise<Response>(() => {})
    return Promise.resolve(json(reply))
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

async function routing() {
  vi.resetModules()
  const [App, paths] = await Promise.all([import('../App'), import('../paths')])
  /** Where a path lands once the router has resolved and rewritten it. */
  const land = (path: string): string => {
    const [pathname, search = ''] = path.split('?')
    const p = paths.pathToAddress(pathname!, search === '' ? '' : `?${search}`)
    expect(p, `${path} is no address at all`).not.toBeNull()
    return paths.addressToPath(App.canonical(App.fromAddress(p!.address)))
  }
  return { App, paths, land }
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  localStorage.clear()
  sessionStorage.clear()
  window.history.replaceState(null, '', '/')
})

describe('the Config section (CONFIG-NAV)', () => {
  it('lists exactly the six pages, in BGD\'s order, between Capacity and Admin', async () => {
    const { App } = await routing()
    const config = App.SECTIONS.find((s) => s.id === 'config')
    // MUTATION: drop the Config entry from SECTIONS -> fails.
    expect(config, 'there is no Config section').toBeDefined()
    expect(config!.label).toBe('Config')
    // MUTATION: swap Secrets and Repositories, or leave Workspace Settings out -> fails.
    expect(config!.tabs.map((t) => t.label)).toEqual(CONFIG_TABS)
    // MUTATION: put Config after Admin -> fails.
    expect(App.SECTIONS.map((s) => s.id)).toEqual(['overview', 'work', 'automate', 'capacity', 'config', 'admin'])
  })

  it('draws the same six pages in the panel, and Config on the spine between Capacity and Admin', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'member')])
    await app('/config/github')
    await waitFor(() => expect(document.querySelector('.sk-spine a[data-sec="config"]')).not.toBeNull())
    const spine = [...document.querySelectorAll('.sk-spine a[data-sec]')].map((a) => a.getAttribute('data-sec'))
    // MUTATION: leave Config out of the spine -> fails.
    expect(spine.indexOf('config')).toBe(spine.indexOf('capacity') + 1)
    const rows = [...document.querySelectorAll('.sk-panel .sk-pk .sk-pl')].map((e) => e.textContent)
    // MUTATION: list the panel's pages in another order -> fails.
    await waitFor(() => expect([...document.querySelectorAll('.sk-panel .sk-pk .sk-pl')].map((e) => e.textContent)).toEqual(CONFIG_TABS))
    expect(rows.length).toBeGreaterThan(0)
    expect(document.querySelector('.sk-panel .sk-pk.is-on .sk-pl')?.textContent).toBe('GitHub access')
  })

  it('leaves People and roles out of a personal workspace\'s Config (S1e)', async () => {
    serve(ME(true, 'u-ana'), [PERSONAL])
    await app('/config/github')
    await waitFor(() => expect(document.querySelector('.sk-pfoot small')?.getAttribute('title')).toBe('ana.b@example.com'))
    // MUTATION: draw PANEL_PAGES.config unfiltered -> People and roles is listed and this fails.
    await waitFor(() =>
      expect([...document.querySelectorAll('.sk-panel .sk-pk .sk-pl')].map((e) => e.textContent)).toEqual(CONFIG_TABS.filter((t) => t !== 'People and roles')),
    )
  })

  it('knows a personal workspace from its own tenant, even for someone in no group', async () => {
    // `/v1/tenants/mine` answers empty for a member of no group: the personal
    // workspace is still the one in hand, as `me` says.
    serve(ME(false, 'u-ana'), [])
    await app('/config/github')
    await waitFor(() => expect(document.querySelector('.sk-pfoot small')?.getAttribute('title')).toBe('ana.b@example.com'))
    // MUTATION: read "personal" off the tenants list -> an empty list draws People and roles and four steps, and this fails.
    await waitFor(() =>
      expect([...document.querySelectorAll('.sk-panel .sk-pk .sk-pl')].map((e) => e.textContent)).toEqual(CONFIG_TABS.filter((t) => t !== 'People and roles')),
    )
    const strip = await screen.findByRole('list', { name: 'Workspace setup steps' })
    expect(within(strip).getAllByRole('listitem')).toHaveLength(3)
  })

  it('takes Repositories, Setup and Access out of Work, and leaves Settings only Profile (API keys is Config › Secrets since SSC-UI)', async () => {
    const { App } = await routing()
    const work = App.SECTIONS.find((s) => s.id === 'work')!
    // MUTATION: leave Access in Work -> fails.
    for (const moved of ['repositories', 'setup', 'access']) expect(work.tabs.map((t) => t.id)).not.toContain(moved)
    vi.resetModules()
    const { PANEL_PAGES } = await import('../Spine')
    // MUTATION: leave Setup in the Work panel -> fails.
    expect(PANEL_PAGES.work.map((p) => p.key)).toEqual(['agents', 'workflows', 'runs', 'timeline'])
    // MUTATION: leave MCP servers under Profile -> fails.
    expect(PANEL_PAGES.profile.map((p) => p.label)).toEqual(['Profile'])
  })
})

describe('every old address lands on its new one', () => {
  const MOVED: readonly [string, string][] = [
    ['/access', '/config/github'],
    // GA-MOVE-VIEWS: Members is People and roles' tab, Browser logins Secrets'.
    ['/access?view=members', '/config/people?view=members'],
    ['/access?view=logins', '/config/secrets?view=logins'],
    ['/repositories', '/config/repositories'],
    ['/repositories/register', '/config/repositories/register'],
    ['/repositories/tokens', '/config/repositories/tokens'],
    ['/repositories/tokens/permissions', '/config/repositories/tokens/permissions'],
    ['/repositories/acme%2Fapi', '/config/repositories/acme%2Fapi'],
    ['/repositories/acme%2Fapi/graph?view=tests&q=src', '/config/repositories/acme%2Fapi/graph?view=tests&q=src'],
    ['/repositories?page=qa-targets', '/config/repositories?page=qa-targets'],
    ['/setup', '/config/workspace/setup'],
    ['/settings/workspace', '/config/workspace'],
    ['/settings/workspace/secrets', '/config/secrets'],
    ['/settings/workspace/secrets?action=rotate&name=gh-token', '/config/secrets?action=rotate&name=gh-token'],
    ['/settings/mcp-servers', '/config/mcp-servers'],
    ['/settings/people', '/config/people'],
    ['/config', '/config/github'],
  ]

  it.each(MOVED)('%s -> %s', async (old, now) => {
    const { land } = await routing()
    // MUTATION: drop any one entry of the old-path table in paths.ts -> its row fails.
    expect(land(old)).toBe(now)
  })

  it('round-trips every new address, so a Config link is written once and read back the same', async () => {
    const { land } = await routing()
    for (const [, now] of MOVED) expect(land(now), now).toBe(now)
  })

  it('lands the addresses the pages and the old hash router still name', async () => {
    const { App, paths } = await routing()
    const OLD: readonly [string, string][] = [
      ['work/access', '/config/github'],
      ['work/setup', '/config/workspace/setup'],
      ['work/repositories', '/config/repositories'],
      ['work/repositories?repo=acme%2Fapi&tab=settings', '/config/repositories/acme%2Fapi/settings'],
      ['work/repositories?page=qa-targets', '/config/repositories?page=qa-targets'],
      ['settings/workspace', '/config/workspace'],
      ['settings/workspace/secrets?action=add', '/config/secrets?action=add'],
      ['settings/mcp-servers', '/config/mcp-servers'],
      ['settings/people', '/config/people'],
    ]
    for (const [address, path] of OLD) {
      // MUTATION: let addressToPath write the old path for an old address -> fails.
      expect(paths.addressToPath(address), address).toBe(path)
      // MUTATION: drop the address rewrite in fromAddress -> `#work/access` opens an agent called "access" and this fails.
      expect(paths.addressToPath(App.canonical(App.fromAddress(address))), `#${address}`).toBe(path)
    }
  })

  it('keeps the plugin\'s and the API\'s anchors on the setup page', async () => {
    const { paths } = await routing()
    // `/setup#workspace` is what swarm_api's notifications link to.
    expect(paths.pathToAddress('/setup', '', '#workspace')?.address).toBe('config/workspace?part=setup')
  })

  it('rewrites the bar of an old bookmark in place', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'admin')])
    await app('/settings/mcp-servers')
    // MUTATION: drop the normalise to the new path -> the bar keeps /settings/mcp-servers and this fails.
    await waitFor(() => expect(window.location.pathname).toBe('/config/mcp-servers'))
    expect(await screen.findByRole('heading', { level: 1, name: 'MCP servers' })).toBeTruthy()
  })
})

describe('the workspace secrets one-time link', () => {
  const linkToken = () => ['lnk', 'tok'].join('') + 'Zq4'.repeat(10)
  const LIST = { tenant_id: 'eng', can_manage: true, secrets: [], platform_credentials: { runner_provider_keys: [], pool_accounts: { readable: true, accounts: [] } }, note: 'n' }
  const CONSUMERS = { consumers: [], kinds: ['api_key'], providers: ['other'] }

  it('opens Config › Secrets with its add form, and the token never stays in the bar', async () => {
    const token = linkToken()
    serve(ME(false), [PERSONAL, group('eng', 'admin')], (url) =>
      url.startsWith('/v1/workspace/secrets/consumers') ? CONSUMERS : url.startsWith('/v1/workspace/secrets') ? LIST : undefined,
    )
    const { SECRETS_LINK_PATH } = await import('../paths')
    // The path the API mints (swarm_api `WorkspaceSecrets.mint_link`): unchanged.
    expect(SECRETS_LINK_PATH).toBe('/settings/workspace/secrets')
    await app(`/settings/workspace/secrets?action=add&t=${token}`)
    // MUTATION: route the link's path to Workspace Settings instead -> no add form and this fails.
    expect(await screen.findByRole('form', { name: /add a secret/i })).toBeTruthy()
    expect(screen.getByText('Opened from a one-time link')).toBeTruthy()
    await waitFor(() => expect(window.location.pathname).toBe('/config/secrets'))
    // MUTATION: carry `t` through the redirect -> fails.
    expect(window.location.href).not.toContain(token)
    expect(window.location.search).toBe('?action=add')
  })
})

describe('Admin is drawn only for an admin (CONFIG-NAV)', () => {
  // SOMEONE WHO ADMINISTERS NOTHING IS TOLD SO IN PLACE (visual QA pass 1
  // admin-shell-23, part of #2596; BGD 2026-10-28 reversed CONFIG-NAV's
  // redirect to Overview for this case): the address stays, and the page's
  // place says which page, the role and the workspace, with a link to Overview.
  it('refuses a member in place at /admin/limits, and draws no Admin', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'member')])
    await app('/admin/limits')
    // MUTATION: keep the redirect to Overview (or drop the refusal) -> this is not found.
    expect(await screen.findByText('Pool limits is for platform admins')).toBeTruthy()
    expect(document.querySelector('[data-refused="admin"]')?.textContent).toMatch(/You are a member of \S+\./)
    expect(screen.getByRole('link', { name: 'Open Overview' })).toBeTruthy()
    expect(window.location.pathname).toBe('/admin/limits')
    expect(document.querySelector('.sk-spine a[data-sec="admin"]')).toBeNull()
  })

  // QA1B-04 review: the owner of a personal workspace is its admin, as the
  // panel foot says, even when `/v1/tenants/mine` carries no stored role for it.
  // admin2-02 (visual QA pass 2, part of #2694): "You are an admin of <their own
  // workspace>" read as if they qualified; the refusal says it is their own.
  it('tells the personal-workspace owner it is their own workspace, with no stored role', async () => {
    const { role: _stored, ...unroled } = PERSONAL
    serve(ME(false, 'u-ana'), [unroled])
    await app('/admin/limits')
    await waitFor(() => expect(document.querySelector('[data-refused="admin"]')).not.toBeNull())
    const said = document.querySelector('[data-refused="admin"]')!.textContent
    // MUTATION: name the role by `roleOf` alone -> "your role in ... was not read" and this fails.
    expect(said).not.toContain('was not read')
    expect(said).toMatch(/You are not a platform admin\. [^.]+ is your own workspace\./)
    expect(said).not.toContain('an admin of')
  })

  // QA1B-04 review: the refusal is drawn in the page's frame -- the gutter of
  // `.app` and the head of `PageHead` -- not flush against the panel.
  it('draws the refusal inside the page frame, under a head titled by the page', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'member')])
    await app('/admin/people')
    await waitFor(() => expect(document.querySelector('[data-refused="admin"]')).not.toBeNull())
    const refusal = document.querySelector('[data-refused="admin"]')!
    // MUTATION: draw `<AdminRoleRefusal/>` alone in the children's place -> no frame and this fails.
    const work = refusal.closest('main.work')
    expect(work).not.toBeNull()
    expect(work!.parentElement!.classList.contains('app')).toBe(true)
    expect(work!.querySelector('.c-phead h1')?.textContent).toBe('People and roles')
    // The inset is `.app`'s own: a gutter on both sides at every width.
    const css = readFileSync(join(__dirname, '..', 'styles.css'), 'utf8')
    expect(css).toMatch(/\n\.app \{[^}]*padding: 0 var\(--app-pad\) 48px;/)
  })

  it('refuses a member in place at every Admin address, deep links included', async () => {
    for (const path of ['/admin/tenants', '/admin/people', '/admin/workspaces', '/admin']) {
      serve(ME(false), [PERSONAL])
      await app(path)
      await waitFor(() => expect(document.querySelector('[data-refused="admin"]'), path).not.toBeNull())
      // `/admin` is written as its first page; either way it stays in Admin.
      expect(window.location.pathname, path).toBe(path === '/admin' ? '/admin/limits' : path)
      cleanup()
    }
  })

  it('does not redirect while either read is in flight', async () => {
    serve(null, [PERSONAL])
    await app('/admin/limits')
    await new Promise((r) => setTimeout(r, 30))
    // MUTATION: treat an unread session as "not an admin" -> redirected and this fails.
    expect(window.location.pathname).toBe('/admin/limits')
  })

  it('keeps a platform admin and a workspace admin where they asked to go', async () => {
    serve(ME(true), [PERSONAL])
    await app('/admin/limits')
    await waitFor(() => expect(document.querySelector('.sk-spine a[data-sec="admin"]')).not.toBeNull())
    expect(window.location.pathname).toBe('/admin/limits')
    cleanup()
    serve(ME(false), [PERSONAL, group('eng', 'admin')])
    await app('/admin/workspaces')
    await waitFor(() => expect(document.querySelector('.sk-spine a[data-sec="admin"]')).not.toBeNull())
    expect(window.location.pathname).toBe('/admin/workspaces')
  })
})

describe('a workspace admin on a platform Admin page is refused in place, and a bare /admin goes to Workspaces you admin (ADMIN-REDIRECT)', () => {
  // BGD 2026-10-24, "Redirect, in a later lane": the nav never links a
  // workspace admin to a platform page, and a typed or bookmarked one sent
  // them to the one Admin page they are shown. Visual QA pass 2 admin2-01
  // (part of #2694) applies BGD 2026-10-28's admin-shell-23 rule to them
  // too: a platform page is refused in place, with a link to Workspaces you
  // admin; only the bare /admin, Admin itself, still lands there.
  // Not /admin/people since ADMIN-PEOPLE-ROLES (part of #2157): a workspace
  // admin has a scoped People and roles page there (the case below).
  const PLATFORM_PAGES = ['/admin/limits', '/admin/tenants', '/admin/runner-sizes', '/admin/counts', '/admin']

  it('refuses a workspace admin in place on every platform Admin address, and sends the bare /admin to /admin/workspaces', async () => {
    for (const path of PLATFORM_PAGES) {
      serve(ME(false), [PERSONAL, group('eng', 'admin')])
      await app(path)
      if (path === '/admin') {
        await waitFor(() => expect(window.location.pathname, path).toBe('/admin/workspaces'))
      } else {
        // MUTATION: redirect the workspace admin again -> no refusal, and the address changes.
        await waitFor(() => expect(document.querySelector('[data-refused="admin"]')?.textContent, path).toContain('is for platform admins'))
        expect(window.location.pathname, path).toBe(path)
      }
      expect(document.querySelector('.sk-spine a[data-sec="admin"]'), path).not.toBeNull()
      cleanup()
    }
  })

  it('renders People and roles at /admin/people for a workspace admin instead of redirecting (ADMIN-PEOPLE-ROLES)', async () => {
    const scoped = {
      scope: 'workspaces',
      workspaces: [{ tenant_id: 'eng', group: 'eng@example.com', display_name: 'Engineering', default_role: 'member', unavailable: false }],
      workspaces_error: null,
      people: [{ email: 'ana.b@example.com', display_name: null, roles: { eng: 'admin' }, last_active: null }],
      count: 1,
      pending: 0,
    }
    serve(ME(false), [PERSONAL, group('eng', 'admin')], (url) => (url.includes('/v1/admin/people') ? scoped : undefined))
    await app('/admin/people')
    await screen.findByRole('heading', { name: 'People and roles', level: 1 })
    await waitFor(() => expect(document.querySelector('.pp-grid th[data-tenant="eng"]')).not.toBeNull())
    await new Promise((r) => setTimeout(r, 30))
    // MUTATION: keep People out of a workspace admin's Admin pages -> the
    // redirect sends them to /admin/workspaces and this fails.
    expect(window.location.pathname).toBe('/admin/people')
    const rows = [...document.querySelectorAll('.sk-panel .sk-pk .sk-pl')].map((e) => e.textContent)
    // Their workspaces' group, then the In Automate links (CONFIG-PANEL-E); no platform page.
    expect(rows.slice(0, 2)).toEqual(['People and roles', 'Workspaces you admin'])
    expect(rows.join(' ')).not.toMatch(/Pool limits|Runner sizes|Tenants|Platform counts/)
    // The Admin item still lands on Workspaces you admin, as ADMIN-REDIRECT drew it.
    expect(document.querySelector('.sk-spine a[data-sec="admin"]')?.getAttribute('href')).toBe('/admin/workspaces')
  })

  it('rewrites the bare /admin in place, so Back does not return to it', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'admin')])
    const push = vi.spyOn(window.history, 'pushState')
    await app('/admin')
    await waitFor(() => expect(window.location.pathname).toBe('/admin/workspaces'))
    // MUTATION: navigate with `go` (a push) -> Back lands on /admin/limits again.
    expect(push).not.toHaveBeenCalled()
    push.mockRestore()
  })

  it('never redirects a platform admin, from any Admin page', async () => {
    for (const path of PLATFORM_PAGES.filter((p) => p !== '/admin')) {
      serve(ME(true), [PERSONAL, group('eng', 'admin')])
      await app(path)
      await waitFor(() => expect(document.querySelector('.sk-spine a[data-sec="admin"]')).not.toBeNull())
      await new Promise((r) => setTimeout(r, 30))
      // MUTATION: redirect on the stored role alone -> a platform admin who
      // also administers a group is sent away and this fails.
      expect(window.location.pathname, path).toBe(path)
      cleanup()
    }
  })

  it('refuses a member and a viewer in place, never sending them to Workspaces you admin', async () => {
    for (const role of ['member', 'viewer']) {
      serve(ME(false), [PERSONAL, group('eng', role)])
      await app('/admin/tenants')
      // MUTATION: send everyone refused to /admin/workspaces -> this fails.
      await waitFor(() => expect(document.querySelector('[data-refused="admin"]'), role).not.toBeNull())
      expect(window.location.pathname, role).toBe('/admin/tenants')
      cleanup()
    }
  })

  it('does not redirect a workspace admin while the identity read is in flight', async () => {
    serve(null, [PERSONAL, group('eng', 'admin')])
    await app('/admin/limits')
    await new Promise((r) => setTimeout(r, 30))
    // MUTATION: decide on `/v1/tenants/mine` alone -> a platform admin whose
    // `/v1/tenants/me` is still loading would be redirected; this fails.
    expect(window.location.pathname).toBe('/admin/limits')
  })
})

describe('the setup strip (proposal E)', () => {
  // ONE CASE PER PAGE, NOT ONE LOOP (PR #2305's CI, 2026-10-25): seven full
  // app mounts in one case overran the 30s test timeout on a slow runner, and
  // the timed-out loop went on mounting pages into the next case, which then
  // found two setup strips. Each page now has its own timeout and cleanup.
  // The strip lands ~1.7s after App mounts on an idle machine, close to
  // setup.ts' 2s `asyncUtilTimeout`, so its own wait is 10s: still a failure,
  // not a hang (MCP-LEAD-FORM, 2026-10-25).
  const STRIP_WAIT = { timeout: 10_000 }
  it.each(['/config/github', '/config/repositories', '/config/secrets', '/config/mcp-servers', '/config/people', '/config/workspace', '/config/workspace/setup'])(
    'runs across every Config page: %s',
    async (path) => {
      serve(ME(false), [PERSONAL, group('eng', 'admin')])
      await app(path)
      // MUTATION: mount the strip on GitHub access only -> every other row fails.
      const strip = await screen.findByRole('list', { name: 'Workspace setup steps' }, STRIP_WAIT)
      expect(within(strip).getAllByRole('listitem').length, path).toBe(4)
    },
  )

  it('carries a compact form for the phone that names the next measured step (S2d)', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'admin')])
    await app('/config/secrets')
    await screen.findByRole('list', { name: 'Workspace setup steps' }, STRIP_WAIT)
    await waitFor(() => expect(document.querySelector('.cfg-compact')?.textContent ?? '').toContain('Next: Choose repositories'), STRIP_WAIT)
    const compact = document.querySelector('.cfg-compact')!
    // MUTATION: drop the compact form -> the phone shows four stacked steps and this fails.
    expect(compact.textContent).toContain('Setup 1 of 4 steps')
    // MUTATION: link the compact form to Workspace setup while a step is next -> fails.
    expect(compact.querySelector('a')?.getAttribute('href')).toBe('/config/repositories')
    cleanup()
  })

  it('measures GitHub and repositories from the onboarding read and never calls an unread step done', async () => {
    vi.resetModules()
    // Named at run time, so the file's other cases run (and fail on their own) before the module exists.
    const mod = './../ConfigSetup'
    const { setupSteps } = (await import(/* @vite-ignore */ mod)) as {
      setupSteps: (doc: unknown, personal: boolean) => { label: string; state: string }[]
    }
    const steps = setupSteps(ONBOARDING, false)
    expect(steps.map((s) => s.label)).toEqual(['GitHub connected', 'Repositories chosen', 'Secrets set', 'People invited'])
    // MUTATION: read github_connected as todo -> fails.
    expect(steps.map((s) => s.state)).toEqual(['done', 'todo', 'unknown', 'unknown'])
    // MUTATION: count an unknown step as done -> fails.
    expect(steps.filter((s) => s.state === 'done')).toHaveLength(1)
    // No read: every step unknown, none done.
    expect(setupSteps(null, false).every((s) => s.state === 'unknown')).toBe(true)
    // A personal workspace has nobody to invite (S1e).
    expect(setupSteps(ONBOARDING, true).map((s) => s.label)).toEqual(['GitHub connected', 'Repositories chosen', 'Secrets set'])
  })
})
