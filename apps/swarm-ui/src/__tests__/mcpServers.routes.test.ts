// SETTINGS › MCP SERVERS' ADDRESS (lane MCP-UI; part of #1433). The
// workspace's registered MCP servers, beside Settings › Workspace. The old
// `#settings/<anything else>` hashes still land on Pool limits: only this
// exact address is the new page.

import { describe, expect, it } from 'vitest'
import { addressToPath, pathToAddress } from '../paths'
import { canonical, fromAddress } from '../App'

describe('Settings › MCP servers', () => {
  it('is /settings/mcp-servers, both ways', () => {
    const p = pathToAddress('/settings/mcp-servers')
    expect(p).not.toBeNull()
    const r = fromAddress(p!.address)
    expect(r.sectionId).toBe('settings')
    expect(r.tab).toBe('mcp-servers')
    expect(addressToPath(canonical(r))).toBe('/settings/mcp-servers')
  })

  it('leaves Workspace, API keys and the old hashes where they were', () => {
    expect(fromAddress('settings/workspace')).toMatchObject({ sectionId: 'settings', tab: 'workspace' })
    expect(fromAddress('settings/keys')).toMatchObject({ sectionId: 'settings', tab: 'keys' })
    expect(fromAddress('settings/anything-else')).toMatchObject({ sectionId: 'admin', tab: 'limits' })
  })
})
