// THE WORKSPACE'S OWN SETTINGS PAGE'S ADDRESS (lane CTXQ-UI). It was
// `/settings/workspace`, beside Settings › API keys; since CONFIG-NAV (part of
// #2157) it is Config › Workspace Settings, `/config/workspace`, and the old
// path lands there. The old `#settings/<anything else>` hashes still land on
// Pool limits.

import { describe, expect, it } from 'vitest'
import { addressToPath, pathToAddress } from '../paths'
import { canonical, fromAddress } from '../App'

describe('Config › Workspace Settings', () => {
  it('is /config/workspace, both ways, and /settings/workspace lands there', () => {
    for (const path of ['/config/workspace', '/settings/workspace']) {
      const p = pathToAddress(path)
      expect(p).not.toBeNull()
      const r = fromAddress(p!.address)
      expect(r.sectionId).toBe('config')
      expect(r.tab).toBe('workspace')
      expect(addressToPath(canonical(r))).toBe('/config/workspace')
    }
  })

  it('leaves API keys and the old hashes where they were', () => {
    // API keys moved to Config › Secrets (SSC-UI, part of #2158).
    expect(fromAddress('settings/keys')).toMatchObject({ sectionId: 'config', tab: 'secrets', view: 'scope=workspace' })
    expect(fromAddress('settings/anything-else')).toMatchObject({ sectionId: 'admin', tab: 'limits' })
  })
})
