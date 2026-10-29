/**
 * THE CONFIG PANEL, MEASURED (CONFIG-PANEL-E; part of #2157, BGD's pick of
 * 2026-10-24, proposal E, docs/web-ui/mockups/config-e.html S1, S1a-S1g, S10a).
 *
 *   * The strip's steps 3 and 4 are measured from CONFIG-SETUP-READ's
 *     `config_setup`: Secrets set is done when nothing is needed and not
 *     stored, else "N to set" naming the first item; People invited is done
 *     when someone besides the reader holds a role, and absent in a personal
 *     workspace (S1e). An unread source is "—" with its reason, never done.
 *   * Each Config row in the panel carries its state from the same read (S1a),
 *     and a member is drawn the same states (S1d).
 *   * Admin is drawn in groups: a platform admin's workspaces, then the
 *     platform's pages, then "In Automate" links (S1b); a workspace admin's
 *     own workspaces only (S1c). The phone drawer holds the same panel (S1f).
 *   * Admin › Workspaces you admin draws each workspace's own progress (S10a).
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'

type Json = Record<string, unknown>

const ME = (isAdmin: boolean, tenant = 'eng'): Json => ({
  tenant: { tenant_id: tenant, kind: tenant.startsWith('u-') ? 'user' : 'group', display_name: `${tenant}@example.com` },
  principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: isAdmin },
  environment: 'dev',
  environment_declared: true,
})
const PERSONAL: Json = { tenant_id: 'u-ana', display_name: 'ana.b@example.com', kind: 'user', via: 'personal', role: { name: 'admin', source: 'personal' } }
const group = (id: string, role: string): Json => ({ tenant_id: id, display_name: `${id}@example.com`, kind: 'group', via: 'member', role: { name: role, source: 'override' } })

const step = (name: string, state: string, evidence: Json = {}): Json => ({ step: name, state, code: null, copy: null, checked_at: null, evidence, issues: [] })
const REPOS = ['acme/api', 'acme/web', 'acme/docs', 'acme/infra'].map((full_name) => ({ full_name }))
const item = (kind: string, name: string, extra: Json = {}): Json => ({ kind, name, needed_by: `${name}'s consumer`, where: 'config/secrets', ...extra })

const SECRETS_THREE: Json = {
  needed: 3,
  done: false,
  items: [item('mcp_server', 'linear'), item('qa_target', 'shop', { target_id: 't1', environment: 'staging' }), item('provider_key', 'typesafe')],
  sources: { mcp_servers: { needed: 1, reason: null }, qa_targets: { needed: 1, reason: null }, provider_key: { needed: 1, reason: null } },
}
const SECRETS_NONE: Json = { needed: 0, done: true, items: [], sources: { mcp_servers: { needed: 0, reason: null }, qa_targets: { needed: 0, reason: null }, provider_key: { needed: 0, reason: null } } }
const QA_UNREAD = 'the QA targets could not be read'
const SECRETS_UNREAD: Json = {
  needed: null,
  done: null,
  items: [item('mcp_server', 'linear')],
  sources: { mcp_servers: { needed: 1, reason: null }, qa_targets: { needed: null, reason: QA_UNREAD }, provider_key: { needed: 0, reason: null } },
}
const PEOPLE = (done: boolean | null, holders: number | null): Json => ({
  applies: true,
  done,
  holders,
  admins: holders === null ? null : 0,
  pending: null,
  reason: done === null ? "this workspace's roles could not be read; retry shortly" : null,
  pending_reason: 'n',
})
const PEOPLE_PERSONAL: Json = { applies: false, done: null, holders: null, admins: null, pending: null, reason: 'a personal workspace has nobody to invite', pending_reason: null }

/** An onboarding read with GitHub connected, `repos` chosen and the given `config_setup`. */
const onboarding = (setup: Json | undefined, repos: 'done' | 'todo' = 'done', tenant = 'eng'): Json => ({
  tenant_id: tenant,
  user: 'ana.b@example.com',
  user_hash: 'h',
  derived_at: '2026-10-25T09:00:00Z',
  steps: [step('github_connected', 'done', { login: 'ana' }), step('repos_chosen', repos, repos === 'done' ? { repositories: REPOS } : {})],
  next_step: null,
  complete: false,
  source: 'derived',
  ...(setup === undefined ? {} : { config_setup: setup }),
})

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

/** The frame's reads; the onboarding read answered per `X-Swarm-Tenant` (the chosen tenant when none is named). */
function serve(me: Json, mine: Json[], setups: Readonly<Record<string, Json | number>>) {
  const seen: string[] = []
  globalThis.fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/v1/tenants/mine')) return Promise.resolve(json(mine))
    if (url.includes('/v1/tenants/me')) return Promise.resolve(json(me))
    if (url.includes('/v1/onboarding')) {
      const headers = (init?.headers ?? {}) as Record<string, string>
      const tenant = headers['X-Swarm-Tenant'] ?? String((me.tenant as Json).tenant_id)
      seen.push(tenant)
      const answer = setups[tenant]
      if (answer === undefined) return Promise.resolve(json({ code: 'tenant_not_member', message: 'not a member' }, 403))
      return Promise.resolve(typeof answer === 'number' ? json({ detail: 'Internal Server Error' }, answer) : json(answer))
    }
    return Promise.resolve(json({ detail: 'Not Found' }, 404))
  }) as unknown as typeof fetch
  return { seen }
}

async function app(path: string) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const mod = await import('../App')
  window.history.replaceState(null, '', path)
  render(<mod.App />)
  return mod
}

async function steps() {
  vi.resetModules()
  return (await import('../ConfigSetup')) as typeof import('../ConfigSetup')
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  localStorage.clear()
  sessionStorage.clear()
  window.history.replaceState(null, '', '/')
})

describe('steps 3 and 4, measured (S1)', () => {
  it('draws Secrets set as a count naming the first item, and done when nothing is missing', async () => {
    const { setupSteps } = await steps()
    const three = setupSteps(onboarding({ secrets: SECRETS_THREE, people: PEOPLE(false, 0) }) as never, false)
    const secrets = three.find((s) => s.key === 'secrets')!
    // MUTATION: leave step 3 "not measured yet" -> fails.
    expect(secrets.state).toBe('todo')
    // MUTATION: drop the first item's name, or count the sources not the items -> fails.
    expect(secrets.line).toBe('3 to set: linear and 2 more')
    const none = setupSteps(onboarding({ secrets: SECRETS_NONE, people: PEOPLE(false, 0) }) as never, false)
    expect(none.find((s) => s.key === 'secrets')).toMatchObject({ state: 'done', line: 'nothing missing' })
    // A QA entry is named with its environment.
    const qa = setupSteps(onboarding({ secrets: { ...SECRETS_THREE, needed: 1, items: [item('qa_target', 'shop', { environment: 'staging' })] }, people: PEOPLE(false, 0) }) as never, false)
    expect(qa.find((s) => s.key === 'secrets')!.line).toBe('1 to set: shop (staging)')
  })

  it('draws People invited done when someone else holds a role, and to do when only the reader does', async () => {
    const { setupSteps } = await steps()
    const others = setupSteps(onboarding({ secrets: SECRETS_NONE, people: PEOPLE(true, 5) }) as never, false)
    // MUTATION: leave step 4 "not measured yet" -> fails.
    expect(others.find((s) => s.key === 'people')).toMatchObject({ state: 'done', line: '5 other people hold a role' })
    const alone = setupSteps(onboarding({ secrets: SECRETS_NONE, people: PEOPLE(false, 0) }) as never, false)
    expect(alone.find((s) => s.key === 'people')).toMatchObject({ state: 'todo', line: 'only you so far' })
  })

  it('leaves People invited out of a personal workspace: three steps (S1e)', async () => {
    const { setupSteps } = await steps()
    const personal = setupSteps(onboarding({ secrets: SECRETS_NONE, people: PEOPLE_PERSONAL }, 'done', 'u-ana') as never, true)
    // MUTATION: push People invited whatever the workspace -> fails.
    expect(personal.map((s) => s.key)).toEqual(['github', 'repositories', 'secrets'])
  })

  it('draws an unread source as unknown with its reason, never done', async () => {
    const { setupSteps, setupCount } = await steps()
    const s = setupSteps(onboarding({ secrets: SECRETS_UNREAD, people: PEOPLE(null, null) }) as never, false)
    // MUTATION: read `needed: null` as 0 (done), or count the sources that were read -> fails.
    expect(s.find((x) => x.key === 'secrets')).toMatchObject({ state: 'unknown', line: QA_UNREAD })
    expect(s.find((x) => x.key === 'people')).toMatchObject({ state: 'unknown', line: "this workspace's roles could not be read; retry shortly" })
    expect(setupCount(s)).toEqual({ done: 2, of: 4, unknown: 2 })
    // A read with no `config_setup` at all (an older API) is unknown the same way.
    const old = setupSteps(onboarding(undefined) as never, false)
    expect(old.filter((x) => x.state === 'unknown').map((x) => x.key)).toEqual(['secrets', 'people'])
  })
})

/** The panel row for `label`, and its badge's text and name. */
function badge(label: string): { text: string; why: string } | null {
  const row = [...document.querySelectorAll('.sk-panel .sk-pk')].find((r) => r.querySelector('.sk-pl')?.textContent === label)
  const b = row?.querySelector('.cfg-badge')
  return b === null || b === undefined ? null : { text: b.textContent ?? '', why: b.getAttribute('aria-label') ?? '' }
}

describe('the Config panel\'s row badges (S1a)', () => {
  it('draws each row\'s state from the strip\'s read, and the two agree', async () => {
    serve(ME(true), [PERSONAL, group('eng', 'admin')], { eng: onboarding({ secrets: SECRETS_THREE, people: PEOPLE(false, 0) }) })
    await app('/config/secrets')
    const strip = await screen.findByRole('list', { name: 'Workspace setup steps' })
    await waitFor(() => expect(badge('Secrets')?.text).toBe('3 to set'))
    // MUTATION: draw no badge on the Config rows -> every line below fails.
    expect(badge('GitHub access')?.text).toBe('✓')
    expect(badge('Repositories')?.text).toBe('4')
    expect(badge('MCP servers')?.text).toBe('1 to set')
    expect(badge('People and roles')?.text).toBe('only you')
    // Optional, so dim like S1a's lone admin, never the warning tone.
    // MUTATION: give "only you" the missing tone -> fails.
    const people = [...document.querySelectorAll('.sk-panel .sk-pk')].find((r) => r.querySelector('.sk-pl')?.textContent === 'People and roles')
    expect(people?.querySelector('.cfg-badge')?.className).toBe('cfg-badge is-note')
    expect([...document.querySelectorAll('.sk-panel .cfg-badge.is-missing')].map((b) => b.textContent)).toEqual(['3 to set', '1 to set'])
    expect(badge('Workspace Settings')).toBeNull()
    // THE BADGES MATCH THE STRIP: each badge's name is the step's own line.
    const lines = within(strip)
      .getAllByRole('listitem')
      .map((li) => li.querySelector('small')?.textContent ?? '')
    // MUTATION: derive the Secrets badge from another count than the strip's -> fails.
    expect(lines[2]).toContain('3 to set: linear and 2 more')
    expect(badge('Secrets')?.why).toBe('Secrets set: 3 to set: linear and 2 more')
    expect(lines[3]).toBe('only you so far')
    expect(badge('People and roles')?.why).toBe('People invited: only you so far')
  })

  it('draws a dash with the reason on a row whose source was unread, never a tick', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'admin')], {
      eng: onboarding({ secrets: { ...SECRETS_UNREAD, sources: { ...(SECRETS_UNREAD.sources as Json), mcp_servers: { needed: null, reason: 'the MCP servers could not be read' } } }, people: PEOPLE(null, null) }),
    })
    await app('/config/secrets')
    await waitFor(() => expect(badge('Secrets')?.text).toBe('—'))
    // MUTATION: draw `needed: null` as done -> a tick and this fails.
    expect(badge('Secrets')?.why).toContain('could not be read')
    expect(badge('MCP servers')).toEqual({ text: '—', why: 'MCP servers: the MCP servers could not be read' })
    expect(badge('People and roles')?.text).toBe('—')
  })

  it('draws no tick on MCP servers when nothing is missing: the read counts credentials, not registrations (VQ-046)', async () => {
    serve(ME(true), [PERSONAL, group('eng', 'admin')], { eng: onboarding({ secrets: SECRETS_NONE, people: PEOPLE(false, 0) }) })
    await app('/config/secrets')
    await waitFor(() => expect(badge('Secrets')?.text).toBe('✓'))
    // MUTATION: draw "MCP servers: nothing to set" as a done tick -> it reads as configured over none registered, and this fails.
    expect(badge('MCP servers')).toBeNull()
  })

  it('draws a member the same states, and People invited done when someone else holds a role (S1d)', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'member')], { eng: onboarding({ secrets: SECRETS_THREE, people: PEOPLE(true, 5) }) })
    await app('/config/people')
    await waitFor(() => expect(badge('Secrets')?.text).toBe('3 to set'))
    expect(badge('People and roles')?.text).toBe('5')
    const strip = await screen.findByRole('list', { name: 'Workspace setup steps' })
    await waitFor(() => expect(document.querySelector('.cfg-count b')?.textContent).toBe('3 of 4 steps done'))
    expect(within(strip).getAllByRole('listitem')[3]!.className).toContain('is-done')
  })
})

describe('the Admin panel in groups (S1b, S1c)', () => {
  const groups = () =>
    [...document.querySelectorAll<HTMLElement>('.sk-panel [role="group"]')].map((g) => ({
      name: g.getAttribute('aria-label'),
      rows: [...g.querySelectorAll('.sk-pk .sk-pl')].map((e) => (e.textContent ?? '').replace(' ↗', '')),
      hrefs: [...g.querySelectorAll('a.sk-pk')].map((a) => a.getAttribute('href')),
    }))

  it('draws a platform admin their workspaces, then the platform\'s pages, then In Automate links only', async () => {
    serve(ME(true), [PERSONAL, group('eng', 'admin')], {})
    await app('/admin/limits')
    await waitFor(() => expect(groups().length).toBe(3))
    const [ws, platform, automate] = groups()
    // MUTATION: draw PANEL_PAGES.admin as one list -> no groups and this fails.
    expect(ws).toMatchObject({ name: 'People and workspaces', rows: ['People and roles', 'Workspaces you admin'] })
    expect(platform).toMatchObject({ name: 'Platform', rows: ['Pool limits', 'Runner sizes', 'Tenants', 'Platform counts', 'Platform secrets'] })
    // MUTATION: draw Schedules or Approvals as Admin pages -> their links leave Automate and this fails.
    expect(automate).toEqual({ name: 'In Automate', rows: ['Schedules', 'Approvals'], hrefs: ['/automate/schedules?scope=all', '/automate/approvals'] })
    // The spine's Admin still opens Pool limits.
    expect(document.querySelector('.sk-spine a[data-sec="admin"]')?.getAttribute('href')).toBe('/admin/limits')
  })

  it('draws a workspace admin only their own workspaces\' group, and the two links into Automate', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'admin'), group('ops', 'admin'), group('far', 'member')], {})
    await app('/admin/workspaces')
    await waitFor(() => expect(groups().length).toBe(2))
    const [ws, automate] = groups()
    // MUTATION: hand a workspace admin the Platform group -> fails.
    expect(ws).toMatchObject({ name: 'Your workspaces', rows: ['People and roles', 'Workspaces you admin'] })
    expect(automate).toEqual({ name: 'In Automate', rows: ['Schedules', 'Approvals'], hrefs: ['/automate/schedules', '/automate/approvals'] })
    expect(document.querySelector('.sk-panel')?.textContent).not.toMatch(/Pool limits|Tenants|Platform counts/)
    expect(document.querySelector('.sk-pnote')?.textContent).toBe('Showing eng and ops, the workspaces you administer. The platform’s own pages are shown to platform admins only.')
  })
})

describe('Workspaces you admin, each with its own setup progress (S10a)', () => {
  const strips = () => [...document.querySelectorAll<HTMLElement>('.wya-row .cfg-progress')]

  it('draws one strip per workspace, each from that workspace\'s own read, and names the one behind', async () => {
    const { seen } = serve(ME(false), [PERSONAL, group('eng', 'admin'), group('ops', 'admin'), group('far', 'member')], {
      eng: onboarding({ secrets: SECRETS_THREE, people: PEOPLE(true, 2) }),
      ops: onboarding({ secrets: SECRETS_NONE, people: PEOPLE(false, 0) }, 'todo', 'ops'),
    })
    await app('/admin/workspaces')
    // MUTATION: draw the rows without a strip -> fails.
    await waitFor(() => expect(strips()).toHaveLength(2))
    // MUTATION: read every workspace's setup without naming it -> both read eng and this fails.
    expect(seen).toEqual(expect.arrayContaining(['eng', 'ops']))
    const [eng, ops] = strips()
    expect(eng!.getAttribute('aria-label')).toBe('eng setup')
    expect(eng!.querySelector('.cfg-progress-h b')?.textContent).toBe('Setup 3 of 4 steps')
    expect(eng!.querySelector('.cfg-progress-next')?.textContent).toBe('Next: Set secrets (3 to set: linear and 2 more)')
    expect(ops!.querySelector('.cfg-progress-h b')?.textContent).toBe('Setup 2 of 4 steps')
    expect(ops!.querySelector('.cfg-progress-next a')?.getAttribute('href')).toBe('/config/repositories')
    // MUTATION: drop the comparison -> nothing says which is behind and this fails.
    const lead = screen.getByRole('region', { name: 'Your workspaces' })
    expect(lead.querySelector('b')?.textContent).toBe('ops is behind')
    expect(lead.querySelector('.ws-lead-s')?.textContent).toBe('Next: repositories chosen (not yet).')
    // Reading one workspace's setup does not switch the one in hand.
    const { chosenTenant } = await import('../fetch')
    expect(chosenTenant()).toBeNull()
  })

  it('says a workspace\'s setup was not read, rather than drawing it done', async () => {
    serve(ME(false), [PERSONAL, group('eng', 'admin'), group('ops', 'admin')], { eng: onboarding({ secrets: SECRETS_NONE, people: PEOPLE(true, 1) }), ops: 500 })
    await app('/admin/workspaces')
    await waitFor(() => expect(strips()).toHaveLength(1))
    const ops = document.querySelector('.wya-row[data-tenant="ops"]')!
    // MUTATION: draw a failed read as the empty strip -> a strip and this fails.
    expect(ops.querySelector('.cfg-progress')).toBeNull()
    expect(ops.querySelector('.wya-reading')?.textContent).toMatch(/^Setup not read/)
    // One read is no comparison.
    expect(screen.queryByRole('region', { name: 'Your workspaces' })).toBeNull()
  })
})
