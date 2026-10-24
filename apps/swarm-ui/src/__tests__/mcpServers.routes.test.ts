// MCP SERVERS' ADDRESS (lane MCP-UI; part of #1433). The workspace's
// registered MCP servers were Settings › MCP servers, `/settings/mcp-servers`;
// since CONFIG-NAV (part of #2157) they are Config › MCP servers,
// `/config/mcp-servers`, and the old path and address land there. The old
// `#settings/<anything else>` hashes still land on Pool limits.

import { describe, expect, it } from 'vitest'
import { addressToPath, pathToAddress } from '../paths'
import { canonical, fromAddress } from '../App'

describe('Config › MCP servers', () => {
  it('is /config/mcp-servers, both ways, and /settings/mcp-servers lands there', () => {
    for (const path of ['/config/mcp-servers', '/settings/mcp-servers']) {
      const p = pathToAddress(path)
      expect(p).not.toBeNull()
      const r = fromAddress(p!.address)
      expect(r.sectionId).toBe('config')
      expect(r.tab).toBe('mcp-servers')
      expect(addressToPath(canonical(r))).toBe('/config/mcp-servers')
    }
  })

  it('leaves API keys and the old hashes where they were, and sends Workspace to Config', () => {
    expect(fromAddress('settings/workspace')).toMatchObject({ sectionId: 'config', tab: 'workspace' })
    expect(fromAddress('settings/keys')).toMatchObject({ sectionId: 'settings', tab: 'keys' })
    expect(fromAddress('settings/anything-else')).toMatchObject({ sectionId: 'admin', tab: 'limits' })
  })
})
