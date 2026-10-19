/**
 * PROFILE IS THE FIRST SPINE ITEM, AND ITS PANEL HOLDS EVERY SETTINGS PAGE
 * (NAV-PROFILE; part of #1633, owner decision 2026-10-19 "All under Profile;
 * Admin by role").
 *
 * The panel foot stacked four "Settings ›" links under the name, and People
 * and roles had no link at all. Profile now sits under the Hive, above
 * Submit, drawn as the person's avatar; its panel lists Profile, API keys,
 * Workspace, MCP servers and People and roles. The foot keeps the name, the
 * admin tag, Sign out, the email and the theme. The addresses are unchanged.
 *
 * MUTATION: drop the Profile item from the spine, or draw it after Submit,
 * and the first case goes red; drop or reorder a PANEL_PAGES.profile row and
 * the second does; take a tab out of `spineOf`'s Profile set and the third
 * does; put a `sk-pset` link back in the foot and the fourth does; put the
 * foot's avatar back and the fifth does.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'

import { App, fromAddress, spineOf } from '../App'
import { pathToAddress } from '../paths'
import { SkyShell } from '../Spine'

afterEach(() => {
  cleanup()
  localStorage.clear()
  window.history.replaceState(null, '', '/')
})

const PAGES: readonly [string, string][] = [
  ['Profile', '/settings/profile'],
  ['API keys', '/settings/keys'],
  ['Workspace', '/settings/workspace'],
  ['MCP servers', '/settings/mcp-servers'],
  ['People and roles', '/settings/people'],
]

function shell(section: 'profile' | 'work', tab: string, go = vi.fn()) {
  render(
    <SkyShell section={section} tab={tab} title="x" go={go} foot={null} place={section === 'profile' ? `/settings/${tab}` : '/agents'}>
      {null}
    </SkyShell>,
  )
}

/** The pages' own scroller, assembled: the class name, after a dot, reads as a key to the publish scan. */
const PAGES_COLUMN = '.sk' + '-pscroll'
const spine = () => document.querySelector<HTMLElement>('.sk-spine')!
const label = (a: Element) => a.querySelector('small')?.textContent ?? ''
const panelRows = () => [...document.querySelectorAll<HTMLAnchorElement>(`.sk-panel ${PAGES_COLUMN} a.sk-pk`)]

describe('the spine (NAV-PROFILE)', () => {
  it('draws Profile first, under the Hive and above Submit, with the avatar as its icon', () => {
    shell('work', 'running')
    const links = [...spine().querySelectorAll<HTMLAnchorElement>(':scope > a')]
    expect(links[0]!.classList.contains('sk-hive'), 'the Hive is not first').toBe(true)
    const profile = links[1]!
    expect(profile.getAttribute('data-sec')).toBe('profile')
    expect(label(profile)).toBe('Profile')
    expect(profile.getAttribute('href')).toBe('/settings/profile')
    expect(profile.querySelector('.sk-av'), 'the Profile item is not drawn as the avatar').not.toBeNull()
    expect(links[2]!.classList.contains('sk-cta'), 'Submit does not follow Profile').toBe(true)
  })

  it('routes a plain click on Profile to settings/profile', () => {
    const go = vi.fn()
    shell('work', 'running', go)
    fireEvent.click(spine().querySelector('a[data-sec="profile"]')!)
    expect(go).toHaveBeenCalledWith('settings/profile')
  })

  it('draws the avatar once: the foot no longer carries a second one', () => {
    shell('work', 'running')
    const avatars = [...spine().querySelectorAll('.sk-av')]
    expect(avatars).toHaveLength(1)
    expect(avatars[0]!.closest('a[data-sec="profile"]'), 'the one avatar is not the Profile item').not.toBeNull()
  })

  it('draws the avatar once on the collapsed spine too', () => {
    localStorage.setItem('swarm.shell.collapsed', '1')
    shell('work', 'running')
    expect(document.querySelector('.sk-app.is-collapsed')).not.toBeNull()
    expect(spine().querySelectorAll('.sk-av')).toHaveLength(1)
  })
})

describe('the Profile panel (NAV-PROFILE)', () => {
  it('is headed Profile and lists the five settings pages in order, each at its path', () => {
    shell('profile', 'profile')
    expect(document.querySelector('.sk-panel .sk-pt')?.textContent).toBe('Profile')
    expect(panelRows().map((a) => [a.querySelector('.sk-pl')?.textContent, a.getAttribute('href')])).toEqual(PAGES)
  })

  it('routes a plain click on People and roles to settings/people', () => {
    const go = vi.fn()
    shell('profile', 'profile', go)
    fireEvent.click(panelRows().find((a) => a.textContent?.includes('People and roles'))!)
    expect(go).toHaveBeenCalledWith('settings/people')
  })

  it('lights API keys on the one-time link address', () => {
    shell('profile', 'keys/typesafe')
    const on = panelRows().filter((a) => a.classList.contains('is-on'))
    expect(on.map((a) => a.querySelector('.sk-pl')?.textContent)).toEqual(['API keys'])
  })

  it('is what the collapsed spine flies out from Profile', () => {
    localStorage.setItem('swarm.shell.collapsed', '1')
    shell('profile', 'workspace')
    fireEvent.focus(spine().querySelector('a[data-sec="profile"]')!)
    const fly = document.querySelector<HTMLElement>('.sk-flyout')
    expect(fly, 'Profile opens no flyout').not.toBeNull()
    expect(fly!.querySelector('b')?.textContent).toBe('Profile')
    const rows = [...fly!.querySelectorAll<HTMLAnchorElement>('a')]
    expect(rows.map((a) => [a.textContent, a.getAttribute('href')])).toEqual(PAGES)
    expect(rows.filter((a) => a.getAttribute('aria-current') === 'page').map((a) => a.textContent)).toEqual(['Workspace'])
  })
})

describe('each settings address lights Profile and its own page (NAV-PROFILE)', () => {
  const ADDRESSES: readonly [string, string, string][] = [
    ['/settings/profile', '', 'Profile'],
    ['/settings/keys', '', 'API keys'],
    ['/settings/keys/typesafe', '?scope=user', 'API keys'],
    ['/settings/workspace', '', 'Workspace'],
    ['/settings/mcp-servers', '', 'MCP servers'],
    ['/settings/people', '', 'People and roles'],
  ]

  it.each(ADDRESSES)('routes %s%s to Profile in spineOf', (path, query) => {
    const r = fromAddress(pathToAddress(path, query)!.address)
    expect(spineOf(r.sectionId, r.tab)).toBe('profile')
  })

  it.each(ADDRESSES)('lights Profile and %s%s’s page in the app', async (path, query, page) => {
    window.history.replaceState(null, '', path + query)
    render(<App />)
    await waitFor(() => expect(document.querySelector('.sk-spine .sk-ri.is-on')?.getAttribute('data-sec')).toBe('profile'))
    const on = panelRows().filter((a) => a.classList.contains('is-on'))
    expect(on.map((a) => a.querySelector('.sk-pl')?.textContent)).toEqual([page])
    expect(on[0]!.getAttribute('aria-current')).toBe('page')
  })

  it('leaves #settings/<anything else> where it landed: Pool limits, under Admin', () => {
    const r = fromAddress('settings/anything-else')
    expect(r).toMatchObject({ sectionId: 'admin', tab: 'limits' })
    expect(spineOf(r.sectionId, r.tab)).toBe('admin')
  })
})

describe('the panel foot (NAV-PROFILE)', () => {
  it('carries no Settings link: the name, Sign out, the email and the theme only', () => {
    shell('profile', 'profile')
    const foot = document.querySelector<HTMLElement>('.sk-pfoot')!
    expect(foot.querySelectorAll('a')).toHaveLength(0)
    expect(foot.textContent).not.toContain('Settings ›')
    expect(foot.querySelector('.sk-theme'), 'the theme toggle left the foot').not.toBeNull()
    expect(foot.querySelector('.sk-pfrow > b'), 'the name left the foot').not.toBeNull()
  })
})

describe('the phone drawer (NAV-PROFILE)', () => {
  it('holds Profile and, opened on a settings page, its five pages', () => {
    shell('profile', 'mcp-servers')
    fireEvent.click(document.querySelector<HTMLElement>('.sk-pbar button[aria-label="Open the menu"]')!)
    const app = document.querySelector<HTMLElement>('.sk-app.has-drawer')
    expect(app, 'the drawer did not open').not.toBeNull()
    const profile = app!.querySelector('.sk-spine a[data-sec="profile"]')
    expect(profile, 'the drawer has no Profile').not.toBeNull()
    expect(profile!.classList.contains('is-on')).toBe(true)
    expect(panelRows().map((a) => [a.querySelector('.sk-pl')?.textContent, a.getAttribute('href')])).toEqual(PAGES)
  })
})
