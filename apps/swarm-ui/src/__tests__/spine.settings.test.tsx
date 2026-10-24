/**
 * MCP SERVERS IS REACHABLE FROM THE CONSOLE (MCP-UI-NAV; part of #1433).
 *
 * MCP-UI routed `/settings/mcp-servers`, and the only way in was the Submit
 * picker's link. It was the panel foot's third "Settings ›" link, then the
 * fourth page of the Profile panel (NAV-PROFILE, part of #1633); since
 * CONFIG-NAV (part of #2157, BGD 2026-10-23) it is Config's fourth page, after
 * Secrets, at `/config/mcp-servers`, and the Profile panel lists it no more.
 *
 * MUTATION: drop the MCP servers row from PANEL_PAGES.config and the first two
 * cases go red; light it on any other place and the third does.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render } from '@testing-library/react'

import { SkyShell, type SpineSection } from '../Spine'

afterEach(() => {
  cleanup()
  window.history.pushState(null, '', '/')
})

/** The pages' own scroller, assembled: the class name, after a dot, reads as a key to the publish scan. */
const PAGES_COLUMN = '.sk' + '-pscroll'

function shell(section: SpineSection, tab: string, go = vi.fn()) {
  render(
    <SkyShell section={section} tab={tab} title="x" go={go} foot={null} place={`/${section}/${tab}`}>
      {null}
    </SkyShell>,
  )
  return [...document.querySelectorAll<HTMLAnchorElement>(`.sk-panel ${PAGES_COLUMN} a.sk-pk`)]
}

const name = (a: HTMLAnchorElement) => a.querySelector('.sk-pl')?.textContent

describe('MCP servers (MCP-UI-NAV, under Config since CONFIG-NAV)', () => {
  it('lists MCP servers after Secrets in Config, at its own path, and not under Profile', () => {
    const links = shell('config', 'github')
    const at = links.findIndex((a) => name(a) === 'MCP servers')
    expect(at, 'the MCP servers page is not in the Config panel').toBeGreaterThan(0)
    expect(name(links[at - 1]!)).toBe('Secrets')
    expect(links[at]!.getAttribute('href')).toBe('/config/mcp-servers')
    cleanup()
    expect(shell('profile', 'profile').map(name)).not.toContain('MCP servers')
    expect(document.querySelectorAll('.sk-pfoot a')).toHaveLength(0)
  })

  it('routes a plain click on MCP servers to config/mcp-servers', () => {
    const go = vi.fn()
    const mcp = shell('config', 'github', go).find((a) => name(a) === 'MCP servers')
    expect(mcp, 'the MCP servers link is not drawn').toBeDefined()
    fireEvent.click(mcp!)
    expect(go).toHaveBeenCalledWith('config/mcp-servers')
  })

  it('lights MCP servers, and only it, when /config/mcp-servers is open', () => {
    const links = shell('config', 'mcp-servers')
    const on = links.filter((a) => a.classList.contains('is-on'))
    expect(on.map(name)).toEqual(['MCP servers'])
    expect(on[0]!.getAttribute('aria-current')).toBe('page')
  })
})
