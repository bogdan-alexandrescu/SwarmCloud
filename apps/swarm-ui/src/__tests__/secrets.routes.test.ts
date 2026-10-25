// CONFIG › SECRETS' ADDRESSES (lane SSC-UI, part of #2158;
// docs/design/secrets-scopes.md §6.1, §6.3). The page takes
// `?scope=workspace|personal|repository&repo=<repo_id>&dest=agents|actions`,
// and every one-time link the API mints lands on it with its token, which
// never becomes part of the router's address (the router writes its address
// back into the bar, and `canonical` is what a copied link is made of):
//
//   /settings/workspace/secrets?action=add&t=..            (WorkspaceSecrets.mint_link)
//   /config/secrets?scope=repository&repo=..&action=..&t=..  (a repository's store link)
//   /config/secrets?scope=repository&repo=..&dest=actions&name=..&env=..&t=..  (ActionsSecrets.mint_link)
//   /config/secrets?scope=workspace&runner_key=<provider>&t=..  (routes/tenants.py RUNNER_KEY_FORM)
//   /settings/keys/typesafe?scope=user|tenant&t=..          (ProviderKeys.mint_link)
//
// Settings › API keys moved here (BGD 2026-10-23T23:57Z): `/settings/keys`
// lands on the workspace scope. Admin › Platform secrets is
// `/admin/platform-secrets`, an admin tab.
//
// Every token is built at runtime.

import { describe, expect, it } from 'vitest'
import { CONFIG_SECRETS_PATH, KEY_LINK_PATH, addressToPath, keyLinkToken, pathToAddress, secretsLinkToken } from '../paths'
import { SECTIONS, canonical, fromAddress } from '../App'

const token = () => ['lnk', 'tok'].join('') + 'Wm7'.repeat(10)
const REPO = 'repo_0a1b2c3d4e5f6071'

/** The path the router writes for a path the bar was opened at. */
function written(path: string, search = ''): string {
  const p = pathToAddress(path, search)
  expect(p, path).not.toBeNull()
  return addressToPath(canonical(fromAddress(p!.address)))
}

describe('the scope, repository and destination ride on the address', () => {
  it('keeps scope, repo and dest, both ways', () => {
    for (const q of [
      'scope=workspace',
      'scope=personal',
      `scope=repository&repo=${REPO}`,
      `scope=repository&repo=${REPO}&dest=actions`,
      `scope=repository&repo=${REPO}&dest=agents`,
    ]) {
      // MUTATION: keep only `action` and `name` (the view before SSC-UI) -> the scope is lost and this fails.
      expect(written(CONFIG_SECRETS_PATH, `?${q}`)).toBe(`/config/secrets?${q}`)
      expect(fromAddress(`config/secrets?${q}`)).toMatchObject({ sectionId: 'config', tab: 'secrets', view: q })
    }
  })

  it('drops what the page does not serve', () => {
    // A repo only with the repository scope, and only a registration id.
    expect(written(CONFIG_SECRETS_PATH, `?scope=workspace&repo=${REPO}`)).toBe('/config/secrets?scope=workspace')
    expect(written(CONFIG_SECRETS_PATH, '?scope=repository&repo=../../etc')).toBe('/config/secrets?scope=repository')
    expect(written(CONFIG_SECRETS_PATH, '?scope=everyone')).toBe('/config/secrets')
    expect(written(CONFIG_SECRETS_PATH, `?scope=repository&repo=${REPO}&dest=elsewhere`)).toBe(`/config/secrets?scope=repository&repo=${REPO}`)
  })
})

describe('the old addresses redirect with their link tokens', () => {
  it('Settings › API keys lands on Config › Secrets, workspace scope', () => {
    // MUTATION: leave `settings/keys` in FIXED as its own page -> fails.
    expect(written('/settings/keys')).toBe('/config/secrets?scope=workspace')
    expect(fromAddress('settings/keys')).toMatchObject({ sectionId: 'config', tab: 'secrets', view: 'scope=workspace' })
    expect(addressToPath('settings/keys')).toBe('/config/secrets?scope=workspace')
    // The bare head opens the one Settings page left.
    expect(written('/settings')).toBe('/settings/profile')
  })

  it('the TypeSafe link lands on the scope its key is in, and its token is in no address', () => {
    const t = token()
    for (const [scope, view] of [['user', 'scope=personal&key=typesafe'], ['tenant', 'scope=workspace&key=typesafe']] as const) {
      const p = pathToAddress(KEY_LINK_PATH, `?scope=${scope}&t=${t}`)
      expect(p).not.toBeNull()
      expect(p!.address).not.toContain(t)
      const r = fromAddress(p!.address)
      // MUTATION: route the link to Settings › API keys -> fails.
      expect(r).toMatchObject({ sectionId: 'config', tab: 'secrets', view })
      const path = addressToPath(canonical(r))
      expect(path).toBe(`/config/secrets?${view}`)
      expect(path).not.toContain(t)
      // App reads the token off the old path before the bar is rewritten.
      expect(keyLinkToken(KEY_LINK_PATH, `?scope=${scope}&t=${t}`)).toBe(t)
    }
  })

  it('the store link keeps its action and name, and its token is read off its path', () => {
    const t = token()
    expect(written('/settings/workspace/secrets', `?action=rotate&name=typesafe-key&t=${t}`)).toBe('/config/secrets?action=rotate&name=typesafe-key')
    expect(secretsLinkToken('/settings/workspace/secrets', `?action=add&t=${t}`)).toBe(t)
  })

  it('a repository store link, an Actions link and a runner-key link keep their view and never their token', () => {
    const t = token()
    for (const view of [
      `scope=repository&repo=${REPO}&action=add`,
      `scope=repository&repo=${REPO}&action=rotate&name=test-db-password`,
      `scope=repository&repo=${REPO}&dest=actions&env=repo-secrets&name=MERGE_APP_PRIVATE_KEY`,
      'scope=workspace&runner_key=anthropic',
    ]) {
      const path = written(CONFIG_SECRETS_PATH, `?${view}&t=${t}`)
      // MUTATION: drop `runner_key` or `dest`/`env` from the view -> the link opens the wrong form and this fails.
      expect(path).toBe(`/config/secrets?${view}`)
      // MUTATION: pass `t` through -> fails.
      expect(path).not.toContain(t)
      expect(secretsLinkToken(CONFIG_SECRETS_PATH, `?${view}&t=${t}`)).toBe(t)
    }
  })
})

describe('Admin › Platform secrets', () => {
  it('is /admin/platform-secrets, an admin-only tab', () => {
    const tab = SECTIONS.find((s) => s.id === 'admin')!.tabs.find((t) => t.id === 'platform-secrets')
    // MUTATION: drop the tab -> fails; drop `admin: true` -> fails.
    expect(tab).toMatchObject({ label: 'Platform secrets', admin: true })
    expect(written('/admin/platform-secrets')).toBe('/admin/platform-secrets')
    expect(fromAddress('admin/platform-secrets')).toMatchObject({ sectionId: 'admin', tab: 'platform-secrets' })
  })
})
