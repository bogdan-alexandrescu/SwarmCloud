// SETTINGS › WORKSPACE's ADDRESS (lane CTXQ-UI). `/settings/workspace` is the
// workspace's own settings page, beside Settings › API keys; it is reached
// from the panel's foot. The old `#settings/<anything else>` hashes still land
// on Pool limits: only this exact address is the new page.

import { describe, expect, it } from 'vitest'
import { addressToPath, pathToAddress } from '../paths'
import { canonical, fromAddress } from '../App'

describe('Settings › Workspace', () => {
  it('is /settings/workspace, both ways', () => {
    const p = pathToAddress('/settings/workspace')
    expect(p).not.toBeNull()
    const r = fromAddress(p!.address)
    expect(r.sectionId).toBe('settings')
    expect(r.tab).toBe('workspace')
    expect(addressToPath(canonical(r))).toBe('/settings/workspace')
  })

  it('leaves API keys and the old hashes where they were', () => {
    expect(fromAddress('settings/keys')).toMatchObject({ sectionId: 'settings', tab: 'keys' })
    expect(fromAddress('settings/anything-else')).toMatchObject({ sectionId: 'admin', tab: 'limits' })
  })
})
