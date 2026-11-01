/**
 * PROFILE RETURNS TO THE SPINE'S FOOT, API READS IS A PAGE OF CAPACITY, AND
 * THE SIGNED-IN NAME OPENS PROFILE (NAV-FOOT; #1672, owner 2026-10-20, after
 * NAV-PROFILE and NAV-ADMIN-ROLE shipped).
 *
 *   1. Profile is the spine's bottom-most item, below Help, at desktop,
 *      collapsed and phone widths. Its panel, its flyout and the drawer keep
 *      the five settings pages.
 *   2. API reads is the last Capacity page (`/capacity/api-reads`) and no
 *      longer a spine item. `#reference`, `/reference` and the rebrand's
 *      `/api-reads` land on it.
 *   3. The first name in the signed-in block is a link to /settings/profile
 *      whose accessible name says it opens your profile. The role chip, Sign
 *      out and the email are unchanged.
 *
 * Every other spine item keeps its place: the full order is asserted.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import { App, SECTIONS, canonical, fromAddress, spineOf } from '../App'
import { addressToPath, pathToAddress } from '../paths'
import { PANEL_PAGES } from '../Spine'

type Json = Record<string, unknown>

const ME = (isAdmin: boolean): Json => ({
  tenant: { tenant_id: 'u-ana', display_name: 'Your workspace' },
  principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: isAdmin },
  environment: 'dev',
  environment_declared: true,
})

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })

/** `/v1/tenants/me` answered as given; every other read fails. */
function serve(me: Json) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/v1/tenants/me') && !url.includes('/v1/tenants/mine')) return json(me)
    return new Response('{}', { status: 503, headers: { 'content-type': 'application/json' } })
  }) as unknown as typeof fetch
}

/** A phone: the one media query the shell asks that holds below 760px. */
function phoneWidth() {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: query.includes('max-width: 759px'),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }))
}

async function liveShell(section: 'work' | 'profile', tab: string, go = vi.fn(), admin = false) {
  serve(ME(admin))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SkyShell: Live } = await import('../Spine')
  render(
    <Live section={section} tab={tab} title="x" go={go} foot={<div className="ctl-nav-util sk-foot"><a className="sk-ri" href="/help"><small>Help</small></a></div>}>
      {null}
    </Live>,
  )
}

afterEach(() => {
  cleanup()
  localStorage.clear()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

const PROFILE_PAGES: readonly [string, string][] = [
  ['Profile', '/settings/profile'],
  // Workspace, MCP servers and People and roles are Config's since CONFIG-NAV
  // (part of #2157), and API keys is Config › Secrets since SSC-UI (part of #2158).
]

const spine = () => document.querySelector<HTMLElement>('.sk-spine')!
/** Every spine item, by its label, in drawn order. */
const spineLabels = () => [...spine().querySelectorAll('a.sk-ri')].map((a) => (a.querySelector('small') ?? a).textContent?.trim())
/** The spine's last element that is a control: a link or a button. */
const lastControl = () => {
  const all = [...spine().querySelectorAll<HTMLElement>('a, button')]
  return all[all.length - 1]!
}
const panelRows = () => [...document.querySelectorAll<HTMLAnchorElement>('.sk-panel a.sk-pk')]

describe('the spine order (NAV-FOOT)', () => {
  it('draws Submit, the sections, Help, then Profile last, at desktop width', () => {
    render(<App />)
    // MUTATION: draw `sectionLink(PROFILE_ITEM)` under the Hive again (NAV-PROFILE's place) and Profile is first, not last.
    // MUTATION: put the API reads link back in App's `ctl-nav-util` foot and 'API reads' appears between Help and Profile.
    expect(spineLabels()).toEqual(['Submit', 'Overview', 'Work', 'Automate', 'Capacity', 'Config', 'Help', 'Profile'])
    // MUTATION: draw Profile above `{foot}` and Help's link is the last control.
    expect(lastControl().getAttribute('data-sec')).toBe('profile')
    expect(lastControl().getAttribute('href')).toBe('/settings/profile')
  })

  it('draws Admin in its place, after Capacity, for a platform admin', async () => {
    await liveShell('work', 'running', vi.fn(), true)
    // MUTATION: drop NAV-ADMIN-ROLE's filter, or move `admin` in SPINE, and the order changes.
    // Voice it follows Admin: a platform admin is who the identity read can name
    // as the person Voice it is open to until swarm-api's session routes say so.
    await waitFor(() => expect(spineLabels()).toEqual(['Submit', 'Overview', 'Work', 'Automate', 'Capacity', 'Config', 'Admin', 'Voice it', 'Help', 'Profile']))
    expect(lastControl().getAttribute('data-sec')).toBe('profile')
  })

  it('keeps Profile last on the collapsed spine, below Help, and flies out its two pages', () => {
    localStorage.setItem('swarm.shell.collapsed', '1')
    render(<App />)
    expect(document.querySelector('.sk-app.is-collapsed')).not.toBeNull()
    // MUTATION: draw Profile before the collapsed spine's expand control or Sign out, and another control is last.
    expect(spineLabels()).toEqual(['Submit', 'Overview', 'Work', 'Automate', 'Capacity', 'Config', 'Help', 'Profile'])
    expect(lastControl().getAttribute('data-sec')).toBe('profile')
    fireEvent.focus(spine().querySelector('a[data-sec="profile"]')!)
    const fly = document.querySelector<HTMLElement>('.sk-flyout')
    // MUTATION: drop `profile` from PANEL_PAGES and Profile flies out nothing.
    expect(fly, 'Profile opens no flyout').not.toBeNull()
    expect(fly!.classList.contains('is-profile')).toBe(true)
    expect([...fly!.querySelectorAll('a')].map((a) => [a.textContent, a.getAttribute('href')])).toEqual(PROFILE_PAGES)
  })

  it('keeps Profile last in the phone drawer, and its panel lists its page', () => {
    phoneWidth()
    window.history.replaceState(null, '', '/settings/profile')
    render(<App />)
    fireEvent.click(document.querySelector<HTMLElement>('.sk-pbar button[aria-label="Open the menu"]')!)
    expect(document.querySelector('.sk-app.has-drawer'), 'the drawer did not open').not.toBeNull()
    // MUTATION: draw Profile under the Hive again and the drawer's spine starts with it.
    expect(spineLabels()).toEqual(['Submit', 'Overview', 'Work', 'Automate', 'Capacity', 'Config', 'Help', 'Profile'])
    expect(lastControl().getAttribute('data-sec')).toBe('profile')
    expect(lastControl().classList.contains('is-on')).toBe(true)
    // MUTATION: drop a PANEL_PAGES.profile row and the drawer's panel loses it.
    expect(panelRows().map((a) => [a.querySelector('.sk-pl')?.textContent, a.getAttribute('href')])).toEqual(PROFILE_PAGES)
  })
})

describe('API reads is a page of Capacity (NAV-FOOT)', () => {
  it('is the last Capacity tab in SECTIONS, labelled API reads', () => {
    const capacity = SECTIONS.find((s) => s.id === 'capacity')!
    // MUTATION: leave API reads out of SECTIONS' Capacity tabs, or not last, and this is red.
    expect(capacity.tabs[capacity.tabs.length - 1]).toEqual({ id: 'api-reads', label: 'API reads' })
    // MUTATION: leave it out of PANEL_PAGES.capacity and the panel and flyout lose it.
    expect(PANEL_PAGES.capacity.map((p) => p.label)).toEqual(['Pools', 'Runtimes', 'Holders', 'Accounts', 'API reads'])
  })

  it('lights Capacity and its API reads page at /capacity/api-reads, with the Failures only row', async () => {
    window.history.replaceState(null, '', '/capacity/api-reads')
    render(<App />)
    // MUTATION: map the tab to 'api' in spineOf and Capacity is not lit.
    await waitFor(() => expect(document.querySelector('.sk-spine .sk-ri.is-on')?.getAttribute('data-sec')).toBe('capacity'))
    const on = panelRows().filter((a) => a.classList.contains('is-on'))
    expect(on.map((a) => a.querySelector('.sk-pl')?.textContent)).toEqual(['API reads'])
    expect(screen.getByRole('heading', { level: 1, name: 'API reads' })).toBeTruthy()
    // MUTATION: drop the panel's two filter buttons under API reads and these are gone.
    const failures = screen.getByRole('button', { name: 'Failures only' })
    expect(failures.getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(failures)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Failures only' }).getAttribute('aria-pressed')).toBe('true'))
  })

  it('is not a spine item', () => {
    window.history.replaceState(null, '', '/capacity/api-reads')
    render(<App />)
    // MUTATION: keep the API reads link in App's spine foot and the spine carries it.
    expect(spineLabels()).not.toContain('API reads')
    expect(spine().querySelector('a[href="/api-reads"], a[href="/capacity/api-reads"]')).toBeNull()
  })

  it.each([
    ['#reference', 'reference'],
    ['#reference/anything', 'reference/anything'],
  ])('lands the old hash %s on the page', (_, address) => {
    const r = fromAddress(address)
    // MUTATION: drop the `reference` rewrite in fromAddress and the old address lands nowhere in Capacity.
    expect(r).toMatchObject({ sectionId: 'capacity', tab: 'api-reads', taskId: null })
    expect(spineOf(r.sectionId, r.tab)).toBe('capacity')
    expect(addressToPath(canonical(r))).toBe('/capacity/api-reads')
  })

  it.each(['/reference', '/reference/anything', '/api-reads'])('lands the old path %s on the page', (path) => {
    const p = pathToAddress(path)
    // MUTATION: drop the old path from paths.ts and it is a 404.
    expect(p, `${path} is not served`).not.toBeNull()
    expect(fromAddress(p!.address)).toMatchObject({ sectionId: 'capacity', tab: 'api-reads' })
  })

  it.each(['/reference', '/api-reads', '/#reference'])('rewrites %s to /capacity/api-reads in the app', async (url) => {
    window.history.replaceState(null, '', url)
    render(<App />)
    // MUTATION: keep `reference: '/api-reads'` as a FIXED path and the bar keeps the old one.
    await waitFor(() => expect(window.location.pathname).toBe('/capacity/api-reads'))
    expect(screen.getByRole('heading', { level: 1, name: 'API reads' })).toBeTruthy()
  })

  it('writes /capacity/api-reads and reads it back', () => {
    // MUTATION: drop 'capacity/api-reads' from paths.ts FIXED and the path is /overview.
    expect(addressToPath('capacity/api-reads')).toBe('/capacity/api-reads')
    expect(pathToAddress('/capacity/api-reads')?.address).toBe('capacity/api-reads')
  })
})

describe('the signed-in name opens Profile (NAV-FOOT)', () => {
  const nameLink = () => document.querySelector<HTMLAnchorElement>('.sk-pfoot .sk-pfrow a')

  it('is a link to /settings/profile whose name says it opens your profile', async () => {
    const go = vi.fn()
    await liveShell('work', 'running', go)
    await waitFor(() => expect(nameLink()).not.toBeNull())
    // MUTATION: draw the name as a plain `<b>` again and there is no link.
    expect(nameLink()!.getAttribute('href')).toBe('/settings/profile')
    expect(nameLink()!.textContent).toBe('ana.b')
    // MUTATION: drop the link's aria-label and its name is only "ana.b".
    expect(screen.getByRole('link', { name: /open your profile/i })).toBe(nameLink())
    fireEvent.click(nameLink()!)
    // MUTATION: route the click anywhere but settings/profile.
    expect(go).toHaveBeenCalledWith('settings/profile')
  })

  it('leaves the role chip and the role line, the email in its title, outside the link', async () => {
    await liveShell('work', 'running', vi.fn(), true)
    await waitFor(() => expect(nameLink()).not.toBeNull())
    const foot = document.querySelector<HTMLElement>('.sk-pfoot')!
    // MUTATION: move the chip or the email into the link and these change.
    expect(foot.querySelector('.sk-pfrow > .sk-adm')?.textContent).toBe('admin')
    // The role line replaced the email (S1a, CONFIG-ADMIN-POLISH); the address is its title.
    expect(foot.querySelector(':scope > small')?.textContent).toBe('Platform admin')
    expect(foot.querySelector(':scope > small')?.getAttribute('title')).toBe('ana.b@example.com')
    expect(nameLink()!.querySelector('.sk-adm')).toBeNull()
    expect(foot.querySelectorAll('a')).toHaveLength(1)
  })

  it('is the same link in the phone drawer', async () => {
    phoneWidth()
    await liveShell('profile', 'profile')
    fireEvent.click(document.querySelector<HTMLElement>('.sk-pbar button[aria-label="Open the menu"]')!)
    // MUTATION: draw the link only outside the drawer and the phone loses it.
    await waitFor(() => expect(document.querySelector('.sk-app.has-drawer .sk-pfoot .sk-pfrow a[href="/settings/profile"]')).not.toBeNull())
  })
})
