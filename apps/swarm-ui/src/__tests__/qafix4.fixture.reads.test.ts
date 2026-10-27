// QAFIX4-FIXTURE-A (part of #1680): visual QA run 4 (wf_ecd935541ced4a539540)
// found reads the fixture build does not serve, F1-F8, and a capture persona
// that is never a platform admin (F4, F5).
//
// These read what the fixture SERVES, through the real loaders in the fixture
// build, as qafix2.fixture.exit.test.ts does: the defect was in the fixture,
// and a hand-built body would agree with whatever the test author assumed.
//
// THE PERSONA is the `swarm_qa_persona` cookie swarm_qa.py sets before each
// route's navigation, read ONCE when api.ts loads. So every persona case
// re-imports the module after setting (or clearing) the cookie.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { createElement } from 'react'
import { cleanup, render, screen } from '@testing-library/react'

type Api = typeof import('../api')

function setPersonaCookie(persona: string | null): void {
  document.cookie =
    persona === null
      ? 'swarm_qa_persona=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/'
      : `swarm_qa_persona=${persona}; path=/`
}

/** api.ts as a page loaded under `persona` (null: no cookie, a dev server opened by hand). */
async function apiAs(persona: string | null): Promise<Api> {
  // The fixture branch is taken only in a DEV build without VITE_LIVE. Said
  // here so a change to that switch fails this file by name.
  expect(import.meta.env.DEV, 'this file reads the fixture build').toBeTruthy()
  expect(import.meta.env.VITE_LIVE, 'VITE_LIVE turns the fixtures off').toBeFalsy()
  setPersonaCookie(persona)
  vi.resetModules()
  return import('../api')
}

function ok<T>(r: { status: string; data?: T; error?: unknown }, what: string): T {
  expect(r.status, `${what} answered ${r.status}: ${JSON.stringify(r.error ?? null)}`).toBe('ok')
  return r.data as T
}

afterEach(() => {
  setPersonaCookie(null)
  vi.unstubAllGlobals()
})

describe('the capture persona (F4 admin-004, F5 admin-005)', () => {
  it('with no cookie the fixture identity is today\'s member: not an admin, no group workspace', async () => {
    const api = await apiAs(null)
    const me = ok(await api.loadMe(), 'GET /v1/tenants/me')
    // MUTATION: make fixtureMe answer is_admin true for every persona -> fails.
    expect(me.principal.is_admin).toBe(false)
    expect(me.principal.email).toBe('alice@example.com')
    expect(me.tenant.tenant_id).toBe('u-bogdan')
    const mine = await api.loadMyTenants()
    // MUTATION: serve the admin's group workspace to the member -> fails.
    expect(mine.status, 'the member picks no tenant').toBe('empty')
    // MUTATION: answer the roster to the member -> fails; the member is refused as today.
    expect((await api.loadTenants()).status).toBe('error')
  })

  it('`member` is the same identity as no cookie, and an unknown value is the member too', async () => {
    for (const persona of ['member', 'root', 'Platform-Admin']) {
      const api = await apiAs(persona)
      const me = ok(await api.loadMe(), `GET /v1/tenants/me as ${persona}`)
      // MUTATION: read any non-empty cookie as the admin -> fails.
      expect(me.principal.is_admin, persona).toBe(false)
      expect((await api.loadMyTenants()).status, persona).toBe('empty')
    }
  })

  it('`platform-admin` is a platform admin who administers one group workspace', async () => {
    const api = await apiAs('platform-admin')
    const me = ok(await api.loadMe(), 'GET /v1/tenants/me')
    // MUTATION: leave fixtureMe's is_admin false for the platform-admin persona -> fails.
    expect(me.principal.is_admin).toBe(true)
    expect(me.tenant.tenant_id, 'the acting workspace is the same personal one').toBe('u-bogdan')
    const mine = ok(await api.loadMyTenants(), 'GET /v1/tenants/mine')
    const groups = mine.filter((c) => c.kind === 'group')
    // MUTATION: serve no group workspace, or two, to the admin persona -> fails.
    expect(groups.length).toBe(1)
    // MUTATION: store the persona's role there as `member` -> fails.
    expect(groups[0]?.role?.name).toBe('admin')
    // MUTATION: reach it as a platform admin who is not in it -> fails; Workspaces you admin skips a reach.
    expect(groups[0]?.via).toBe('member')
    const { adminWorkspaces } = await import('../Spine')
    // MUTATION: drop the group entry -> Workspaces you admin draws its empty state again.
    expect(adminWorkspaces(mine).map((c) => c.tenant_id)).toEqual([groups[0]?.tenant_id])
    // Admin › Tenants decides admin from the profile, so it agrees with /me.
    const profile = ok(await api.loadProfile(), 'GET /v1/profile')
    // MUTATION: leave the profile's identity non-admin for this persona -> Tenants draws no admin control.
    expect(profile.identity.is_admin).toBe(true)
    expect(profile.workspaces.map((w) => w.tenant_id)).toContain(groups[0]?.tenant_id)
  })
})

describe('Pool limits\' settings and model prices are served (F1, admin-001)', () => {
  it('GET /v1/settings/model-prices answers ok with priced models, for the member too', async () => {
    for (const persona of [null, 'platform-admin']) {
      const api = await apiAs(persona)
      const prices = ok(await api.loadModelPrices(), 'GET /v1/settings/model-prices')
      // MUTATION: return the read's 404 in the fixture build -> fails.
      expect(Object.keys(prices.model_prices).length).toBeGreaterThan(1)
    }
  })

  it('GET /v1/admin/settings answers ok to the admin, with the two repository limits', async () => {
    const api = await apiAs('platform-admin')
    const s = ok(await api.loadPlatformSettings(), 'GET /v1/admin/settings')
    // MUTATION: drop the fixture branch of loadPlatformSettings -> it reads the dev server's 404.
    expect(s.max_additional_repositories).toBeGreaterThan(0)
    expect(s.additional_repositories_budget_gib).toBeGreaterThan(0)
    expect(s.settings_document).toBe('present')
    // MUTATION: serve the admin a different price table from the members' route -> fails.
    expect(Object.keys(s.model_prices).sort()).toEqual(Object.keys(ok(await api.loadModelPrices(), 'prices').model_prices).sort())
  })

  // THE SCREEN, NOT ONLY THE LOADER (review of this lane): the card read
  // `/v1/admin/settings` with a bare read(), which no fixture answers, so the
  // loader's fixture branch never reached it. The network here answers 404,
  // as the dev server does, so a card that reads around the loader shows
  // 'Not read'.
  it('Pool limits\' Additional repositories card draws its two fields for the admin persona', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 404, headers: { 'content-type': 'application/json' } })))
    await apiAs('platform-admin')
    const { AdditionalRepositoryLimitsCard } = await import('../AdminSettings')
    render(createElement(AdditionalRepositoryLimitsCard))
    const count = (await screen.findByLabelText('Most additional repositories per step', {}, { timeout: 4000 })) as HTMLInputElement
    // MUTATION: read `/v1/admin/settings` with a bare read() in AdditionalRepositoryLimitsCard -> 'Not read', no field.
    expect(count.value).toBe('4')
    expect((screen.getByLabelText('Size budget per step, GiB') as HTMLInputElement).value).toBe('20')
    // MUTATION: count the card's read as failed in the fixture build -> an alert is drawn.
    expect(screen.queryByRole('alert')).toBeNull()
    cleanup()
  })

  it('the member still sees the card\'s refusal, as on a live deployment', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 404, headers: { 'content-type': 'application/json' } })))
    await apiAs(null)
    const { AdditionalRepositoryLimitsCard } = await import('../AdminSettings')
    render(createElement(AdditionalRepositoryLimitsCard))
    // MUTATION: serve `/v1/admin/settings` to the member persona -> the fields are drawn.
    await screen.findByText('Platform admins only.', {}, { timeout: 4000 })
    expect(screen.queryByLabelText('Most additional repositories per step')).toBeNull()
    cleanup()
  })
})

describe('Runner sizes are served (F2, admin-002)', () => {
  it('the document, its versions and the preview answer ok', async () => {
    const api = await apiAs('platform-admin')
    const doc = ok(await api.loadRunnerSizes(), 'GET /v1/admin/runner-sizes')
    // MUTATION: leave loadRunnerSizes answering repoNotServed -> fails.
    expect(Object.keys(doc.classes)).toEqual(expect.arrayContaining(['standard', 'browser']))
    expect(doc.classes.browser?.live_split, 'the browser class carries its live split').toBeDefined()
    for (const [name, size] of Object.entries(doc.classes)) {
      // MUTATION: give a class a CPU limit under its request -> fails.
      expect(size.cpu_limit, name).toBeGreaterThanOrEqual(size.cpu_request)
      expect(size.memory_gib, name).toBeGreaterThanOrEqual(doc.bounds.memory_gib.min)
    }
    const versions = ok(await api.loadRunnerSizeVersions(), 'GET /v1/admin/runner-sizes/versions')
    // MUTATION: serve no version history -> fails.
    expect(versions.length).toBeGreaterThan(0)
    expect(versions[0]?.version).toBe(doc.current_version)
    const preview = ok(await api.previewRunnerSizes({}, doc.current_version), 'POST /v1/admin/runner-sizes:preview')
    // MUTATION: leave the preview answering repoNotServed -> fails.
    expect(preview.valid).toBe(true)
    expect(Object.keys(preview.classes).sort()).toEqual(Object.keys(doc.classes).sort())
  })
})

describe('the people list is served (F3, admin-003)', () => {
  it('some signed in, one pending, one admin', async () => {
    const api = await apiAs('platform-admin')
    const doc = ok(await api.loadPeople(), 'GET /v1/admin/people') as import('../types').PeopleDoc
    // MUTATION: leave loadPeople answering repoNotServed -> fails.
    expect(doc.people.length).toBeGreaterThan(2)
    expect(doc.people.filter((p) => p.last_seen !== null).length, 'signed in').toBeGreaterThan(1)
    const pending = doc.people.filter((p) => p.workspace.state === 'requested')
    // MUTATION: serve two pending requests, or a `pending` count that disagrees with the rows -> fails.
    expect(pending.length).toBe(1)
    expect(doc.pending).toBe(pending.length)
    expect(doc.count).toBe(doc.people.length)
    // MUTATION: name no admin -> fails. The two platform owners hold no granted
    // document (PLATFORM_OWNER), and alice, a person here, holds the admin one.
    expect(doc.admins?.map((a) => a.role)).toEqual(['owner', 'owner', 'admin'])
    const admin = doc.admins?.find((a) => a.role === 'admin')
    expect(doc.people.map((p) => p.email)).toContain(admin?.email)
  })
})

describe('Admin › Tenants is served to the admin persona (F4, admin-004)', () => {
  it('the roster answers ok and names the persona\'s two workspaces', async () => {
    const api = await apiAs('platform-admin')
    const roster = ok(await api.loadTenants(), 'GET /v1/admin/tenants')
    const mine = ok(await api.loadMyTenants(), 'GET /v1/tenants/mine')
    // MUTATION: keep fixtureTenants' 403 for the admin persona -> fails above.
    for (const c of mine) expect(roster.tenants.map((t) => t.tenant_id), c.tenant_id).toContain(c.tenant_id)
  })
})

describe('the Schedules list is served (F6, overview-automate-001)', () => {
  it('GET /v1/schedules answers ok for the tenant, beside the repositories its built-in rows come from', async () => {
    const api = await apiAs(null)
    const list = ok(await api.loadSchedules(), 'GET /v1/schedules')
    // MUTATION: drop the fixture branch of loadSchedules -> it reads the dev server's 404.
    expect(list.tenant_id).toBe('u-bogdan')
    expect(list.schedules.length).toBeGreaterThan(0)
    for (const s of list.schedules) expect(s.tenant_id, s.schedule_id).toBe('u-bogdan')
    // The built-in rows are one per registered repository.
    expect(ok(await api.loadRepositories(), 'GET /v1/repositories').length).toBeGreaterThan(0)
  })
})

describe('a 404 on a list read says the route was not found (F6, overview-automate-001)', () => {
  function notFound() {
    return new Response(JSON.stringify({ code: 'not_found', message: 'Not Found' }), { status: 404, headers: { 'content-type': 'application/json' } })
  }

  it('a read naming no item says its route was not found, never another tenant\'s item', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => notFound()))
    const { errorReassurance, read, route } = await import('../fetch')
    const r = await read(route('/v1/schedules'), () => false)
    expect(r.status).toBe('error')
    if (r.status !== 'error') throw new Error('unreachable')
    const words = errorReassurance(r.error)
    // MUTATION: restore the one-item copy for every 404 -> fails.
    expect(words).not.toMatch(/another tenant/)
    expect(words).toMatch(/route/)
    // V007: never name a thing nobody asked for.
    expect(words).not.toMatch(/\b(task|schedule|account|run)\b/i)
  })

  it('a read of one item keeps the one-item copy', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => notFound()))
    const { errorReassurance, read, route } = await import('../fetch')
    const r = await read(route('/v1/schedules/{id}', { id: 'sch_0000' }), () => false)
    if (r.status !== 'error') throw new Error('unreachable')
    // MUTATION: give every 404 the list copy -> fails.
    expect(errorReassurance(r.error)).toMatch(/another tenant/)
  })
})

describe('the Settings pages\' reads are served (F7, overview-automate-009..012)', () => {
  it('API keys, Workspace, MCP servers, Profile and People and roles each answer ok', async () => {
    const api = await apiAs(null)
    const keys = ok(await api.loadProviderKeys('typesafe'), 'GET /v1/provider-keys/typesafe')
    // MUTATION: leave readAs answering repoNotServed for every fixture read -> fails.
    expect(keys.resolution.tenant_id).toBe('u-bogdan')
    const judgment = ok(await api.loadJudgment(), 'GET /v1/judgment')
    expect(judgment.tenant_id).toBe('u-bogdan')
    const ci = ok(await api.loadCodeIntelligence(), 'GET /v1/code-intelligence')
    // "The switch below…": the switch is drawn only for a workspace that read, and editable for its owner.
    expect(ci.can_edit).toBe(true)
    const { loadMcpServers } = await import('../McpServers')
    const mcp = ok(await loadMcpServers(), 'GET /v1/mcp-servers')
    // MUTATION: leave McpServers' fixtureNotServed on the read -> fails. "the addresses below" needs addresses.
    expect(mcp.servers.length).toBeGreaterThan(0)
    expect(mcp.egress_addresses?.length).toBeGreaterThan(0)
    const profile = ok(await api.loadProfile(), 'GET /v1/profile')
    // MUTATION: serve a profile under another identity than the shell's -> fails.
    expect(profile.identity.email).toBe('alice@example.com')
    expect(profile.identity.is_admin).toBe(false)
    expect(profile.revision).not.toBeNull()
    expect(profile.workspaces.map((w) => w.tenant_id)).toEqual(['u-bogdan'])
    const roles = ok(await api.loadWorkspaceRoles(), 'GET /v1/workspace/roles')
    expect(roles.tenant_id).toBe('u-bogdan')
    expect(roles.me.email).toBe('alice@example.com')
  })

  it('nothing a Settings read serves looks like a key', async () => {
    const api = await apiAs(null)
    const { loadMcpServers } = await import('../McpServers')
    const bodies = [
      await api.loadProviderKeys('typesafe'), await api.loadJudgment(), await api.loadCodeIntelligence(),
      await loadMcpServers(), await api.loadProfile(), await api.loadWorkspaceRoles(),
    ].map((r) => JSON.stringify(r))
    // MUTATION: put a key's value in the provider-keys fixture -> fails.
    for (const b of bodies) expect(b).not.toMatch(new RegExp('(sk-|gh[pousr]_|-----BEGIN)'))
  })
})

describe('registered repositories are served (F8, work-a-011)', () => {
  it('a few repositories, one with a long name', async () => {
    const api = await apiAs(null)
    const repos = ok(await api.loadRepositories(), 'GET /v1/repositories')
    // MUTATION: leave loadRepositories answering repoNotServed -> fails.
    expect(repos.length).toBeGreaterThanOrEqual(3)
    // MUTATION: give every fixture repository a short name -> fails; the list's overflow is never captured.
    expect(Math.max(...repos.map((r) => `${r.owner}/${r.repo}`.length))).toBeGreaterThan(40)
    expect(new Set(repos.map((r) => r.repo_id)).size).toBe(repos.length)
  })
})
