// THE KEY PAGE'S ADDRESSES (lane KEYS-UI). `/settings/keys` is Settings ›
// API keys; `/settings/keys/typesafe?scope=..&t=..` is the one-time link
// `POST /v1/provider-keys/typesafe/links` mints (docs/provider-keys.md §3).
// The link's token must never become part of the router's address: the
// router writes its address back into the bar, and `canonical` is what a
// copied link is made of.

import { describe, expect, it } from 'vitest'
import { addressToPath, pathToAddress } from '../paths'
import { canonical, fromAddress } from '../App'

const token = () => ['lnk', 'tok'].join('') + 'Rz4'.repeat(9)

describe('Settings › API keys', () => {
  it('is /settings/keys, both ways', () => {
    const p = pathToAddress('/settings/keys')
    expect(p).not.toBeNull()
    const r = fromAddress(p!.address)
    expect(r.sectionId).toBe('settings')
    expect(r.tab).toBe('keys')
    expect(addressToPath(canonical(r))).toBe('/settings/keys')
    // The bare head opens it too.
    expect(addressToPath(canonical(fromAddress(pathToAddress('/settings')!.address)))).toBe('/settings/keys')
  })

  it('keeps the old #settings hashes where they always landed', () => {
    expect(fromAddress('settings')).toMatchObject({ sectionId: 'admin', tab: 'limits' })
    expect(fromAddress('settings/anything-else')).toMatchObject({ sectionId: 'admin', tab: 'limits' })
    expect(fromAddress('settings/accounts')).toMatchObject({ sectionId: 'capacity', tab: 'accounts' })
  })
})

describe('the one-time link', () => {
  it('opens the TypeSafe form with its scope, and the token is in no address the router writes', () => {
    const t = token()
    for (const scope of ['user', 'tenant'] as const) {
      const p = pathToAddress('/settings/keys/typesafe', `?scope=${scope}&t=${t}`)
      expect(p).not.toBeNull()
      expect(p!.address).not.toContain(t)
      const r = fromAddress(p!.address)
      expect(r.sectionId).toBe('settings')
      expect(r.tab).toBe('keys/typesafe')
      expect(r.view).toBe(`scope=${scope}`)
      const path = addressToPath(canonical(r))
      expect(path).toBe(`/settings/keys/typesafe?scope=${scope}`)
      expect(path).not.toContain(t)
    }
  })

  it('drops a scope it does not know', () => {
    const r = fromAddress(pathToAddress('/settings/keys/typesafe', '?scope=everyone')!.address)
    expect(r.tab).toBe('keys/typesafe')
    expect(r.view ?? null).toBeNull()
    expect(addressToPath(canonical(r))).toBe('/settings/keys/typesafe')
  })
})
