// THE KEY PAGE'S OLD ADDRESSES (lane KEYS-UI, then SSC-UI, part of #2158).
// `/settings/keys` was Settings › API keys until BGD moved it into Config ›
// Secrets as Keys for agents and MCP (2026-10-23 23:57Z), and
// `/settings/keys/typesafe?scope=..&t=..` is the one-time link
// `POST /v1/provider-keys/typesafe/links` still mints (docs/provider-keys.md
// §3). Both land on `/config/secrets`, the link on its TypeSafe key form in
// its scope. The link's token must never become part of the router's
// address: the router writes its address back into the bar, and `canonical`
// is what a copied link is made of. secrets.routes.test.tsx holds the rest.

import { describe, expect, it } from 'vitest'
import { addressToPath, pathToAddress } from '../paths'
import { canonical, fromAddress } from '../App'

const token = () => ['lnk', 'tok'].join('') + 'Rz4'.repeat(9)

describe('Settings › API keys, moved', () => {
  it('/settings/keys lands on Config › Secrets, and the bare head on Profile', () => {
    const p = pathToAddress('/settings/keys')
    expect(p).not.toBeNull()
    const r = fromAddress(p!.address)
    // MUTATION: keep Settings › API keys a page -> fails.
    expect(r.sectionId).toBe('config')
    expect(r.tab).toBe('secrets')
    expect(addressToPath(canonical(r))).toBe('/config/secrets')
    expect(addressToPath(canonical(fromAddress(pathToAddress('/settings')!.address)))).toBe('/settings/profile')
  })

  it('keeps the old #settings hashes where they always landed', () => {
    expect(fromAddress('settings')).toMatchObject({ sectionId: 'admin', tab: 'limits' })
    expect(fromAddress('settings/anything-else')).toMatchObject({ sectionId: 'admin', tab: 'limits' })
    expect(fromAddress('settings/accounts')).toMatchObject({ sectionId: 'capacity', tab: 'accounts' })
  })
})

describe('the one-time link', () => {
  it('opens the TypeSafe form in its scope, and the token is in no address the router writes', () => {
    const t = token()
    for (const [scope, kept] of [['user', 'personal'], ['tenant', 'workspace']] as const) {
      const p = pathToAddress('/settings/keys/typesafe', `?scope=${scope}&t=${t}`)
      expect(p).not.toBeNull()
      expect(p!.address).not.toContain(t)
      const r = fromAddress(p!.address)
      expect(r.sectionId).toBe('config')
      expect(r.tab).toBe('secrets')
      expect(r.view).toBe(`scope=${kept}&key=typesafe`)
      const path = addressToPath(canonical(r))
      expect(path).toBe(`/config/secrets?scope=${kept}&key=typesafe`)
      expect(path).not.toContain(t)
    }
  })

  it('drops a scope it does not know, and still opens the form', () => {
    const r = fromAddress(pathToAddress('/settings/keys/typesafe', '?scope=everyone')!.address)
    expect(r.tab).toBe('secrets')
    expect(r.view).toBe('key=typesafe')
    expect(addressToPath(canonical(r))).toBe('/config/secrets?key=typesafe')
  })
})
