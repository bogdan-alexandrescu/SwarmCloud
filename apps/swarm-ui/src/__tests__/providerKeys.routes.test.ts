// THE KEY PAGE'S ADDRESSES (lane KEYS-UI; moved by SSC-UI, part of #2158).
// `/settings/keys` was Settings › API keys; since BGD's decision of
// 2026-10-23T23:57Z its keys are Config › Secrets' "Keys for agents and MCP",
// and the address lands there. `/settings/keys/typesafe?scope=..&t=..` is
// still the one-time link `POST /v1/provider-keys/typesafe/links` mints
// (docs/provider-keys.md §3), and it lands on the scope its key is in. The
// link's token must never become part of the router's address: the router
// writes its address back into the bar, and `canonical` is what a copied
// link is made of.

import { describe, expect, it } from 'vitest'
import { addressToPath, pathToAddress } from '../paths'
import { canonical, fromAddress } from '../App'

const token = () => ['lnk', 'tok'].join('') + 'Rz4'.repeat(9)

describe('Settings › API keys, moved to Config › Secrets', () => {
  it('/settings/keys lands on Config › Secrets, workspace scope', () => {
    const p = pathToAddress('/settings/keys')
    expect(p).not.toBeNull()
    const r = fromAddress(p!.address)
    expect(r.sectionId).toBe('config')
    expect(r.tab).toBe('secrets')
    expect(addressToPath(canonical(r))).toBe('/config/secrets?scope=workspace')
    // The bare head opens the one Settings page left.
    expect(addressToPath(canonical(fromAddress(pathToAddress('/settings')!.address)))).toBe('/settings/profile')
  })

  it('keeps the old #settings hashes where they always landed', () => {
    expect(fromAddress('settings')).toMatchObject({ sectionId: 'admin', tab: 'limits' })
    expect(fromAddress('settings/anything-else')).toMatchObject({ sectionId: 'admin', tab: 'limits' })
    expect(fromAddress('settings/accounts')).toMatchObject({ sectionId: 'capacity', tab: 'accounts' })
  })
})

describe('the one-time link', () => {
  it('opens the scope its key is in, and the token is in no address the router writes', () => {
    const t = token()
    for (const [scope, view] of [['user', 'scope=personal&key=typesafe'], ['tenant', 'scope=workspace&key=typesafe']] as const) {
      const p = pathToAddress('/settings/keys/typesafe', `?scope=${scope}&t=${t}`)
      expect(p).not.toBeNull()
      expect(p!.address).not.toContain(t)
      const r = fromAddress(p!.address)
      expect(r.sectionId).toBe('config')
      expect(r.tab).toBe('secrets')
      expect(r.view).toBe(view)
      const path = addressToPath(canonical(r))
      expect(path).toBe(`/config/secrets?${view}`)
      expect(path).not.toContain(t)
    }
  })

  it('drops a scope it does not know, and lands on the workspace scope', () => {
    const r = fromAddress(pathToAddress('/settings/keys/typesafe', '?scope=everyone')!.address)
    expect(r.tab).toBe('secrets')
    expect(addressToPath(canonical(r))).toBe('/config/secrets?scope=workspace')
  })
})
