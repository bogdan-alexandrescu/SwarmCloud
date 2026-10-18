/**
 * SETTINGS › MCP SERVERS IS REACHABLE FROM THE CONSOLE (MCP-UI-NAV; part of #1433).
 *
 * MCP-UI routed `/settings/mcp-servers`, and the only way in was the Submit
 * picker's link. The design files it under Settings beside Workspace
 * (docs/design/registered-mcp-servers.md §4.11), so the panel's foot carries
 * it with API keys and Workspace, in the same style.
 *
 * MUTATION: drop the MCP servers anchor from Spine.tsx and the first two
 * cases go red; light it on any other place and the third does.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render } from '@testing-library/react'

import { SkyShell } from '../Spine'

afterEach(() => {
  cleanup()
  window.history.pushState(null, '', '/')
})

function shell(place: string, go = vi.fn()) {
  render(
    <SkyShell section="work" tab="running" title="x" go={go} foot={null} place={place}>
      {null}
    </SkyShell>,
  )
  return [...document.querySelectorAll<HTMLAnchorElement>('.sk-pfoot a.sk-pset')]
}

describe('the Settings links (MCP-UI-NAV)', () => {
  it('lists API keys, Workspace and MCP servers, in that order, each at its own path', () => {
    const links = shell('/overview')
    expect(links.map((a) => [a.textContent, a.getAttribute('href')])).toEqual([
      ['Settings › API keys', '/settings/keys'],
      ['Settings › Workspace', '/settings/workspace'],
      ['Settings › MCP servers', '/settings/mcp-servers'],
    ])
  })

  it('routes a plain click on MCP servers to settings/mcp-servers', () => {
    const go = vi.fn()
    const mcp = shell('/overview', go).find((a) => a.textContent === 'Settings › MCP servers')
    expect(mcp, 'the MCP servers link is not drawn').toBeDefined()
    fireEvent.click(mcp!)
    expect(go).toHaveBeenCalledWith('settings/mcp-servers')
  })

  it('lights MCP servers, and only it, when /settings/mcp-servers is open', () => {
    const links = shell('/settings/mcp-servers')
    const on = links.filter((a) => a.classList.contains('is-on'))
    expect(on.map((a) => a.textContent)).toEqual(['Settings › MCP servers'])
    expect(on[0]!.getAttribute('aria-current')).toBe('page')
  })
})
