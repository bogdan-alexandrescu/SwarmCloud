// EVERY PLUGIN LINK IS ROUTED (part of #2688; docs/design/managed-plugins.md,
// the lane plan's shared console wiring row). The console lanes built Admin ›
// Plugins, Add plugin and each plugin's page and left their routing to one
// lane; until it merged, `/admin/plugins` and `/admin/plugins/add` answered
// "Not found" and a card opened nothing. Held to:
//
//   * every href the plugin screens render -- a card, Add plugin, each
//     breadcrumb and back link, with the back addresses App hands them --
//     resolves to a route, and that route is written back as the same path;
//   * Add plugin and a plugin's page are their own pages under Plugins, from
//     both catalogues, and Config's one-time settings link lands on Config ›
//     Plugins with its token kept out of the address;
//   * Admin's panel names Plugins for a platform admin only; Config's stays.
//
// No network: fetch is stubbed per test. Every value is a placeholder.

import { randomBytes } from 'node:crypto'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { canonical, fromAddress, pluginPageOf, SECTIONS, type Route } from '../App'
import { ADMIN_PLUGIN_ADD_PATH, ADMIN_PLUGINS_PATH, adminPluginPath } from '../AdminPlugins'
import { pluginLinkOf } from '../ConfigPlugins'
import { addressToPath, pathToAddress } from '../paths'
import { adminPagesFor, PANEL_PAGES } from '../Spine'

const PLATFORM_INSTALL = 'p-0123456789ab'
const WORKSPACE_INSTALL = 'w-0123456789ab'
const BASE = '/v1/admin/plugin-catalogue'
const LIMITS = { max_packed_bytes: 100 * 1048576, max_unpacked_bytes: 500 * 1048576, max_files: 20000, max_expansion_ratio: 100 }

function install(over: Record<string, unknown>): Record<string, unknown> {
  return {
    install_id: PLATFORM_INSTALL, name: 'review-kit', display_name: 'review-kit', scope: 'platform', tenant_id: null, built_in: false, state: 'active',
    source: { kind: 'github', repository: 'example-org/example-repo', ref: 'v1.2.0', commit: 'cd'.repeat(20), subdir: '' },
    current_version: 'v-0123456789ab', version: '1.2.0', description: 'Reviews a change.', runnable_sha256: 'ab'.repeat(32), hosts: [],
    components: { skills: 3, commands: 1, agents: 0, hooks: 2, mcp_servers: 1 }, removable: true, updatable: true,
    updated_at: '2026-11-06T10:00:00+00:00', updated_by: 'admin@example.com',
    ...over,
  }
}

const CATALOGUE = {
  scope: 'platform',
  built_in: [],
  installs: [install({})],
  workspace_installs: [install({ install_id: WORKSPACE_INSTALL, name: 'rules-gate', display_name: 'rules-gate', scope: 'workspace', tenant_id: 'team-a' })],
  limits: LIMITS,
  can_install: true,
}

function serve(): void {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const ok = String(input) === BASE
    return new Response(JSON.stringify(ok ? CATALOGUE : { detail: 'Not Found' }), { status: ok ? 200 : 404, headers: { 'content-type': 'application/json' } })
  }) as unknown as typeof fetch
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

async function live() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const [grid, add, detail] = await Promise.all([import('../AdminPlugins'), import('../AdminPluginInstall'), import('../PluginDetail')])
  return { grid, add, detail }
}

/** The route an href opens: a page some section draws, whose canonical path is the href's own. */
function routed(href: string): Route {
  const u = new URL(href, 'http://localhost')
  const p = pathToAddress(u.pathname, u.search, u.hash)
  expect(p, `${href} is no route: it would say Not found`).not.toBeNull()
  const r = fromAddress(p!.address)
  const section = SECTIONS.find((s) => s.id === r.sectionId)
  expect(section?.tabs.some((t) => t.id === r.tab), `${href} lands on ${r.sectionId}/${r.tab}, no page`).toBe(true)
  expect(addressToPath(canonical(r)), `${href} is written back as another path`).toBe(u.pathname)
  return r
}

function hrefs(): string[] {
  return [...document.querySelectorAll<HTMLAnchorElement>('a[href]')].map((a) => a.getAttribute('href')!)
}

describe('every link the plugin screens render reaches a routed page', () => {
  it('Admin › Plugins: each card and Add plugin', async () => {
    serve()
    const { grid } = await live()
    render(<grid.AdminPluginsScreen />)
    await screen.findByRole('list', { name: 'Plugins' })
    const found = hrefs()
    // MUTATION: drop the plugin page's branch from paths.ts -> the cards' hrefs are no route.
    expect(found).toEqual(expect.arrayContaining([ADMIN_PLUGIN_ADD_PATH, adminPluginPath(PLATFORM_INSTALL), adminPluginPath(WORKSPACE_INSTALL)]))
    for (const href of found) routed(href)
  })

  it('Add plugin: its breadcrumb and its back link', async () => {
    serve()
    const { add } = await live()
    render(<add.AdminPluginInstallScreen />)
    await screen.findByRole('link', { name: 'Plugins' })
    const found = hrefs()
    expect(found).toContain(ADMIN_PLUGINS_PATH)
    for (const href of found) routed(href)
  })

  it("a plugin's page, with the back address App hands it from either catalogue", async () => {
    serve()
    const { detail } = await live()
    for (const [back, tenant] of [[ADMIN_PLUGINS_PATH, null], [addressToPath('config/plugins'), 'team-a']] as const) {
      render(<detail.PluginDetailScreen installId={PLATFORM_INSTALL} tenantId={tenant} backHref={back} />)
      await screen.findByRole('link', { name: 'Plugins' })
      const found = hrefs()
      expect(found).toContain(back)
      for (const href of found) routed(href)
      cleanup()
    }
  })
})

describe("each plugin's page is read where its install lives", () => {
  it("Admin's reads a platform install as the platform's and a workspace's in its workspace; Config's in the selected one", async () => {
    const urls: string[] = []
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      urls.push(url)
      const body = url === BASE ? CATALOGUE : url === '/v1/plugins' ? { tenant_id: 'team-b' } : null
      return new Response(JSON.stringify(body ?? { detail: 'Not Found' }), { status: body === null ? 404 : 200, headers: { 'content-type': 'application/json' } })
    }) as unknown as typeof fetch
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const app = await import('../App')
    const read = (prefix: string) => waitFor(() => expect(urls.some((u) => u.startsWith(prefix)), `${prefix} was never read: ${urls.join(', ')}`).toBe(true))
    render(<app.AdminPluginRoute installId={PLATFORM_INSTALL} />)
    await read(`${BASE}/installs/${PLATFORM_INSTALL}/versions`)
    cleanup()
    // MUTATION: hand Admin's page a null workspace for a `w-` install -> it reads nothing and says no workspace is chosen.
    render(<app.AdminPluginRoute installId={WORKSPACE_INSTALL} />)
    await read(`/v1/workspaces/team-a/plugin-catalogue/installs/${WORKSPACE_INSTALL}/versions`)
    cleanup()
    render(<app.WorkspacePluginRoute installId={WORKSPACE_INSTALL} />)
    await read(`/v1/workspaces/team-b/plugin-catalogue/installs/${WORKSPACE_INSTALL}/versions`)
  })
})

describe('the plugin pages are pages of Plugins', () => {
  it('Admin › Plugins, Add plugin and each plugin, both ways', () => {
    expect(routed(ADMIN_PLUGINS_PATH)).toMatchObject({ sectionId: 'admin', tab: 'plugins' })
    expect(pluginPageOf(routed(ADMIN_PLUGINS_PATH))).toBeNull()
    expect(pluginPageOf(routed(ADMIN_PLUGIN_ADD_PATH))).toEqual({ kind: 'add' })
    for (const id of [PLATFORM_INSTALL, WORKSPACE_INSTALL]) {
      const r = routed(adminPluginPath(id))
      expect(r).toMatchObject({ sectionId: 'admin', tab: 'plugins' })
      expect(pluginPageOf(r)).toEqual({ kind: 'plugin', installId: id })
    }
  })

  it('Config › Plugins stays, and a plugin opens its page under it', () => {
    expect(pluginPageOf(routed('/config/plugins'))).toBeNull()
    const r = routed(`/config/plugins/${WORKSPACE_INSTALL}`)
    expect(r).toMatchObject({ sectionId: 'config', tab: 'plugins' })
    expect(pluginPageOf(r)).toEqual({ kind: 'plugin', installId: WORKSPACE_INSTALL })
  })

  it("Config's one-time settings link lands on Config › Plugins, its token never in the address", () => {
    const token = randomBytes(16).toString('hex')
    const search = `?field=api_key&t=${token}`
    const path = `/config/plugins/${PLATFORM_INSTALL}`
    // App reads the link off the bar before its normalise effect rewrites it.
    expect(pluginLinkOf(path, search)).toEqual({ install_id: PLATFORM_INSTALL, field: 'api_key', token })
    const p = pathToAddress(path, search)
    expect(p?.address).toBe('config/plugins')
    const r = fromAddress(p!.address)
    expect(r).toMatchObject({ sectionId: 'config', tab: 'plugins' })
    expect(pluginPageOf(r)).toBeNull()
    expect(addressToPath(canonical(r))).not.toContain(token)
  })
})

describe("Admin's navigation names Plugins for a platform admin only", () => {
  it('is in the panel and the section, marked admin, and Config keeps its Plugins', () => {
    const admin = SECTIONS.find((s) => s.id === 'admin')!
    expect(admin.tabs.find((t) => t.id === 'plugins')).toMatchObject({ label: 'Plugins', admin: true })
    expect(adminPagesFor(true, false).map((p) => p.to)).toContain('admin/plugins')
    // MUTATION: add `plugins` to WORKSPACE_ADMIN_PAGE_KEYS -> a workspace admin is offered it.
    expect(adminPagesFor(false, true).map((p) => p.to)).not.toContain('admin/plugins')
    expect(adminPagesFor(false, false, true).map((p) => p.to)).not.toContain('admin/plugins')
    expect(PANEL_PAGES.config.map((p) => p.to)).toContain('config/plugins')
    for (const p of [...PANEL_PAGES.admin, ...PANEL_PAGES.config]) routed(addressToPath(p.to))
  })
})
