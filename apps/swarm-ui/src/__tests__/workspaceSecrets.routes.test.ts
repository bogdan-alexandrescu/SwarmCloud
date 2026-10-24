// SETTINGS › WORKSPACE › SECRETS' ADDRESSES (lane SEC-UI-LIST, part of #1347;
// docs/design/workspace-secrets.md §5). `/settings/workspace/secrets` is the
// page; `/settings/workspace/secrets?action=add&t=..` is the one-time link
// `POST /v1/workspace/secrets/links` mints (swarm_api `mint_link`). The
// link's token must never become part of the router's address: the router
// writes its address back into the bar, and `canonical` is what a copied link
// is made of.

import { describe, expect, it } from 'vitest'
import { SECRETS_LINK_PATH, addressToPath, pathToAddress, secretsLinkToken } from '../paths'
import { canonical, fromAddress } from '../App'

const token = () => ['lnk', 'tok'].join('') + 'Hp3'.repeat(10)

describe('Settings › Workspace › Secrets', () => {
  it('is /settings/workspace/secrets, both ways', () => {
    const p = pathToAddress('/settings/workspace/secrets')
    expect(p).not.toBeNull()
    const r = fromAddress(p!.address)
    expect(r.sectionId).toBe('settings')
    // MUTATION: route the path to the Workspace page's tab -> fails.
    expect(r.tab).toBe('workspace/secrets')
    expect(addressToPath(canonical(r))).toBe('/settings/workspace/secrets')
    expect(SECRETS_LINK_PATH).toBe('/settings/workspace/secrets')
  })

  it('leaves the Workspace page where it was', () => {
    const r = fromAddress(pathToAddress('/settings/workspace')!.address)
    expect(r).toMatchObject({ sectionId: 'settings', tab: 'workspace' })
    expect(addressToPath(canonical(r))).toBe('/settings/workspace')
  })
})

describe('the one-time link', () => {
  it('keeps the action and the name, and the token is in no address the router writes', () => {
    const t = token()
    for (const [search, view] of [
      [`?action=add&t=${t}`, 'action=add'],
      [`?action=rotate&name=typesafe-key&t=${t}`, 'action=rotate&name=typesafe-key'],
    ] as const) {
      const p = pathToAddress('/settings/workspace/secrets', search)
      expect(p).not.toBeNull()
      // MUTATION: pass the query through as FIXED does -> the token is in the address and this fails.
      expect(p!.address).not.toContain(t)
      const r = fromAddress(p!.address)
      expect(r.tab).toBe('workspace/secrets')
      expect(r.view).toBe(view)
      const path = addressToPath(canonical(r))
      expect(path).toBe(`/settings/workspace/secrets?${view}`)
      expect(path).not.toContain(t)
    }
  })

  it('never writes a token an address carries by hand', () => {
    const t = token()
    // MUTATION: let addressToPath pass a `t` through -> fails.
    expect(addressToPath(`settings/workspace/secrets?action=add&t=${t}`)).toBe('/settings/workspace/secrets?action=add')
  })

  it('drops an action it does not know, and a name on an add', () => {
    expect(fromAddress(pathToAddress('/settings/workspace/secrets', '?action=everything')!.address).view ?? null).toBeNull()
    expect(fromAddress(pathToAddress('/settings/workspace/secrets', '?action=add&name=x')!.address).view).toBe('action=add')
  })

  it('reads the token off the secrets path only', () => {
    const t = token()
    expect(secretsLinkToken('/settings/workspace/secrets', `?action=add&t=${t}`)).toBe(t)
    // MUTATION: read `t` off any path -> fails.
    expect(secretsLinkToken('/settings/workspace', `?t=${t}`)).toBeNull()
    expect(secretsLinkToken('/settings/workspace/secrets', '?action=add')).toBeNull()
  })
})
