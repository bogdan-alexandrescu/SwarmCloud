/**
 * SETTINGS › MCP SERVERS IS REACHABLE FROM THE CONSOLE (MCP-UI-NAV; part of #1433).
 *
 * MCP-UI routed `/settings/mcp-servers`, and the only way in was the Submit
 * picker's link. The design files it under Settings beside Workspace
 * (docs/design/registered-mcp-servers.md §4.11). It was the panel foot's third
 * "Settings ›" link; since NAV-PROFILE (part of #1633) it is the fourth page of
 * the Profile panel, after Workspace, and the foot carries no Settings link.
 *
 * MUTATION: drop the MCP servers row from PANEL_PAGES.profile and the first two
 * cases go red; light it on any other place and the third does.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render } from '@testing-library/react'

import { SkyShell } from '../Spine'

afterEach(() => {
  cleanup()
  window.history.pushState(null, '', '/')
})

/** The pages' own scroller, assembled: the class name, after a dot, reads as a key to the publish scan. */
const PAGES_COLUMN = '.sk' + '-pscroll'

function shell(tab: string, go = vi.fn()) {
  render(
    <SkyShell section="profile" tab={tab} title="x" go={go} foot={null} place={`/settings/${tab}`}>
      {null}
    </SkyShell>,
  )
  return [...document.querySelectorAll<HTMLAnchorElement>(`.sk-panel ${PAGES_COLUMN} a.sk-pk`)]
}

const name = (a: HTMLAnchorElement) => a.querySelector('.sk-pl')?.textContent

describe('the Settings pages (MCP-UI-NAV, under Profile since NAV-PROFILE)', () => {
  it('lists MCP servers after Workspace, at its own path, and the foot no longer does', () => {
    const links = shell('profile')
    const at = links.findIndex((a) => name(a) === 'MCP servers')
    expect(at, 'the MCP servers page is not in the Profile panel').toBeGreaterThan(0)
    expect(name(links[at - 1]!)).toBe('Workspace')
    expect(links[at]!.getAttribute('href')).toBe('/settings/mcp-servers')
    expect(document.querySelectorAll('.sk-pfoot a')).toHaveLength(0)
  })

  it('routes a plain click on MCP servers to settings/mcp-servers', () => {
    const go = vi.fn()
    const mcp = shell('profile', go).find((a) => name(a) === 'MCP servers')
    expect(mcp, 'the MCP servers link is not drawn').toBeDefined()
    fireEvent.click(mcp!)
    expect(go).toHaveBeenCalledWith('settings/mcp-servers')
  })

  it('lights MCP servers, and only it, when /settings/mcp-servers is open', () => {
    const links = shell('mcp-servers')
    const on = links.filter((a) => a.classList.contains('is-on'))
    expect(on.map(name)).toEqual(['MCP servers'])
    expect(on[0]!.getAttribute('aria-current')).toBe('page')
  })
})
