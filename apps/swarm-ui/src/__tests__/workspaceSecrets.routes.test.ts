// WORKSPACE SECRETS' ADDRESSES (lane SEC-UI-LIST, part of #1347;
// docs/design/workspace-secrets.md §5). The page was Settings › Workspace ›
// Secrets, `/settings/workspace/secrets`; since CONFIG-NAV (part of #2157) it
// is Config › Secrets, `/config/secrets`. The one-time link keeps its path:
// `POST /v1/workspace/secrets/links` still mints
// `/settings/workspace/secrets?action=add&t=..` (swarm_api `mint_link`), which
// lands on `/config/secrets?action=add`. The link's token must never become
// part of the router's address: the router writes its address back into the
// bar, and `canonical` is what a copied link is made of.

import { describe, expect, it } from 'vitest'
import { CONFIG_SECRETS_PATH, SECRETS_LINK_PATH, addressToPath, pathToAddress, secretsLinkToken } from '../paths'
import { canonical, fromAddress } from '../App'

const token = () => ['lnk', 'tok'].join('') + 'Hp3'.repeat(10)

describe('Config › Secrets', () => {
  it('is /config/secrets, both ways, and the link\'s path lands there', () => {
    expect(CONFIG_SECRETS_PATH).toBe('/config/secrets')
    // MUTATION: change the path the API mints -> every link already sent breaks, and this fails.
    expect(SECRETS_LINK_PATH).toBe('/settings/workspace/secrets')
    for (const path of [CONFIG_SECRETS_PATH, SECRETS_LINK_PATH]) {
      const p = pathToAddress(path)
      expect(p).not.toBeNull()
      const r = fromAddress(p!.address)
      expect(r.sectionId).toBe('config')
      // MUTATION: route the path to Workspace Settings' tab -> fails.
      expect(r.tab).toBe('secrets')
      expect(addressToPath(canonical(r))).toBe('/config/secrets')
    }
  })

  it('leaves Workspace Settings a page of its own', () => {
    const r = fromAddress(pathToAddress('/settings/workspace')!.address)
    expect(r).toMatchObject({ sectionId: 'config', tab: 'workspace' })
    expect(addressToPath(canonical(r))).toBe('/config/workspace')
  })
})

describe('the one-time link', () => {
  it('keeps the action and the name, and the token is in no address the router writes', () => {
    const t = token()
    for (const path of [SECRETS_LINK_PATH, CONFIG_SECRETS_PATH]) {
      for (const [search, view] of [
        [`?action=add&t=${t}`, 'action=add'],
        [`?action=rotate&name=typesafe-key&t=${t}`, 'action=rotate&name=typesafe-key'],
      ] as const) {
        const p = pathToAddress(path, search)
        expect(p).not.toBeNull()
        // MUTATION: pass the query through as FIXED does -> the token is in the address and this fails.
        expect(p!.address).not.toContain(t)
        const r = fromAddress(p!.address)
        expect(r.tab).toBe('secrets')
        expect(r.view).toBe(view)
        const written = addressToPath(canonical(r))
        expect(written).toBe(`/config/secrets?${view}`)
        expect(written).not.toContain(t)
      }
    }
  })

  it('never writes a token an address carries by hand', () => {
    const t = token()
    // MUTATION: let addressToPath pass a `t` through -> fails.
    expect(addressToPath(`settings/workspace/secrets?action=add&t=${t}`)).toBe('/config/secrets?action=add')
    expect(addressToPath(`config/secrets?action=add&t=${t}`)).toBe('/config/secrets?action=add')
  })

  it('drops an action it does not know, and a name on an add', () => {
    expect(fromAddress(pathToAddress('/settings/workspace/secrets', '?action=everything')!.address).view ?? null).toBeNull()
    expect(fromAddress(pathToAddress('/settings/workspace/secrets', '?action=add&name=x')!.address).view).toBe('action=add')
  })

  it('reads the token off the secrets page\'s two paths only', () => {
    const t = token()
    expect(secretsLinkToken('/settings/workspace/secrets', `?action=add&t=${t}`)).toBe(t)
    expect(secretsLinkToken('/config/secrets', `?action=add&t=${t}`)).toBe(t)
    // MUTATION: read `t` off any path -> fails.
    expect(secretsLinkToken('/settings/workspace', `?t=${t}`)).toBeNull()
    expect(secretsLinkToken('/config/workspace', `?t=${t}`)).toBeNull()
    expect(secretsLinkToken('/settings/workspace/secrets', '?action=add')).toBeNull()
  })
})
