// SETTINGS › PROFILE KEEPS AN ACCOUNT'S NAME (ACCOUNT-NAME-3; part of #1913).
//
// ACCOUNT-NAME-2 (#1940) gave the Profile payload's accounts `display_name`
// and named each of 'Your Claude accounts' through `accountName`, but
// `normAccount` in `api.ts` rebuilds each account from its own allow-list and
// dropped the Name, so the page still printed the label. BGD, 2026-10-22:
// "Yes, in a follow-up".
//
// `normAccount` is private; these read it through the real `loadProfile`, over
// a faked GET /v1/profile and over the fixture build.
//
// Placeholders only; nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { serve } from './repofixture'

type Json = Record<string, unknown>

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

function raw(over: Json): Json {
  return { account_id: 'u-ada:main', label: 'main', relation: 'own', state: 'AVAILABLE', windows: {}, lent_to_count: 0, stale: false, ...over }
}

async function accountsServed(accounts: Json[]) {
  serve((m, url) =>
    m === 'GET' && url === '/v1/profile'
      ? { status: 200, body: { identity: { email: 'ada@example.com' }, claude_accounts: { personal_workspace: 'u-ada', accounts, unreadable_document_count: 0 }, unavailable: [] } }
      : null,
  )
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { loadProfile } = await import('../api')
  const r = await loadProfile()
  if (r.status !== 'ok' || r.data.claude_accounts === undefined) throw new Error(JSON.stringify(r))
  return r.data.claude_accounts.accounts
}

describe("normAccount carries an account's Name", () => {
  it('keeps a served Name, and reads an absent or null one as null', async () => {
    const accounts = await accountsServed([
      raw({ display_name: 'My main one' }),
      raw({ account_id: 'eng:shared', label: 'shared', relation: 'borrowed', lent_to_count: null, display_name: null }),
      raw({ account_id: 'eng:other', label: 'other', relation: 'borrowed', lent_to_count: null }),
    ])
    // MUTATION: drop `display_name` from normAccount's returned object.
    expect(accounts[0]!.display_name).toBe('My main one')
    // The id and label are untouched by the Name.
    expect([accounts[0]!.account_id, accounts[0]!.label]).toEqual(['u-ada:main', 'main'])
    // MUTATION: carry `r.display_name` as served (undefined for an absent one) instead of `pfStr(r.display_name)`.
    expect(accounts.slice(1).map((a) => a.display_name)).toEqual([null, null])
  })

  it('reads a Name that is not a string as null', async () => {
    const accounts = await accountsServed([raw({ display_name: 42 })])
    // MUTATION: carry `r.display_name` unchecked instead of `pfStr(r.display_name)`.
    expect(accounts[0]!.display_name).toBeNull()
  })
})

describe("the fixture build's Profile draws a named and an unnamed account", () => {
  it('names one of its Claude accounts and leaves another on its label', async () => {
    vi.stubEnv('VITE_LIVE', '')
    vi.resetModules()
    const real = await vi.importActual<typeof import('../api')>('../api')
    expect(real.USE_FIXTURES).toBe(true)
    const r = await real.loadProfile()
    if (r.status !== 'ok' || r.data.claude_accounts === undefined) throw new Error(JSON.stringify(r))
    const accounts = r.data.claude_accounts.accounts
    // MUTATION: the fixture Profile's `claude_accounts` without a `display_name` on any account.
    expect(accounts.some((a) => typeof a.display_name === 'string' && a.display_name !== a.label)).toBe(true)
    // MUTATION: the fixture Profile's `claude_accounts` giving every account a Name.
    expect(accounts.some((a) => a.display_name === null)).toBe(true)
  })
})
