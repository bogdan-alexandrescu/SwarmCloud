// WORK › REPOSITORIES › QA TARGETS: a leg's entry query (QA-ENTRY-QUERY).
//
// An app that picks its environment from the address (`?env=production`)
// lands a fresh browser on its default one unless every address says which,
// and a target's address refuses a query. The leg's `entry_query` carries the
// pairs apart; the form shows and edits it:
//   * on a target of entries, one input per entry, opened on the served value,
//     and a change sends every entry back whole with only that entry's query
//     changed (a leg's field travels in `environments` alone);
//   * on a one-origin target, one input beside the address, sent as
//     `entry_query`, and emptied it is sent as null (cleared);
//   * the card shows each leg's query beside its address.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { repo, serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const APP = 'qat_' + '47'.repeat(8)
const SHOP = 'qat_' + '48'.repeat(8)
const REPO_ID = 'repo_' + '0'.repeat(16)

type Json = Record<string, unknown>

const UNSET = { set: false, set_at: null, set_by: null, version: null }

function me(role: string): Json {
  return {
    tenant: { tenant_id: 'eng', kind: 'group', principal: 'eng@example.com', display_name: null },
    principal: { email: 'vera@example.com', domain: 'example.com', groups: [], is_admin: false, is_admin_unresolved: false },
    environment: 'dev',
    environment_declared: true,
    role: { name: role, source: 'override' },
  }
}

const SIGN_IN = { kind: 'own_login', login_path: '/', signed_in_selector: 'nav .me', start_clicks: [{ text: 'Production' }] }

/** One entry as `_entry_api` serves it. */
function entry(name: string, over: Json = {}): Json {
  return {
    name,
    base_url: 'https://studio.app.example.test',
    host: 'studio.app.example.test',
    environment: 'production',
    sign_in: SIGN_IN,
    host_state: 'approved',
    approved_by: 'ops@example.com',
    approved_at: '2026-10-22T09:00:00+00:00',
    approved_origins: ['https://studio.app.example.test'],
    approved_idp_token_path: null,
    credential: UNSET,
    challenge_bypass_value: UNSET,
    rate: { navigation_ms: 3000 },
    discovered: null,
    new_since_last_run: [],
    discovered_at: null,
    discovered_by: null,
    discovered_workflow: null,
    discovery_error: null,
    discovering: null,
    ...over,
  }
}

function app(environments: Json[]): Json {
  return {
    id: APP,
    tenant_id: 'eng',
    revision: 2,
    name: 'studio',
    repo_id: REPO_ID,
    repository: 'https://github.com/example-org/example-api',
    authorized_by: "the app's owner",
    screens: [{ id: 'home', path: '/' }],
    environments,
  }
}

function shop(over: Json = {}): Json {
  return {
    id: SHOP,
    tenant_id: 'eng',
    revision: 1,
    name: 'shop',
    base_url: 'https://shop.app.example.test',
    host: 'shop.app.example.test',
    host_state: 'approved',
    approved_by: 'ops@example.com',
    approved_at: '2026-10-22T09:00:00+00:00',
    approved_origins: ['https://shop.app.example.test'],
    approved_idp_token_path: null,
    repo_id: REPO_ID,
    repository: 'https://github.com/example-org/example-api',
    authorized_by: "the app's owner",
    sign_in: { kind: 'none' },
    credential: UNSET,
    challenge_bypass_value: UNSET,
    screens: [{ id: 'home', path: '/' }],
    environment: 'production',
    ...over,
  }
}

function live(targets: Json[]) {
  const calls = serve((m, url) => {
    const path = url.split('?')[0] ?? ''
    if (m === 'GET' && path === '/v1/tenants/me') return { status: 200, body: me('admin') }
    if (m === 'GET' && path === '/v1/qa-targets') return { status: 200, body: { qa_targets: targets } }
    if (m === 'GET' && path === '/v1/repositories') return { status: 200, body: { repositories: [repo({ repo_id: REPO_ID })] } }
    if (m === 'PATCH' && path.startsWith('/v1/qa-targets/')) return { status: 200, body: { qa_target: targets[0] } }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return calls
}

async function mountPage() {
  const { RepositoriesScreen } = await import('../Repositories')
  render(<RepositoriesScreen view="page=qa-targets" go={vi.fn()} />)
  await waitFor(() => expect(document.querySelectorAll('.qa-target').length).toBe(1), WAIT)
}

function button(root: ParentNode, label: string): HTMLButtonElement | null {
  return Array.from(root.querySelectorAll<HTMLButtonElement>('button')).find((b) => visible(b) === label) ?? null
}

async function openEdit(): Promise<HTMLFormElement> {
  await waitFor(() => expect(button(document, 'Edit')).not.toBeNull(), WAIT)
  fireEvent.click(button(document, 'Edit')!)
  return waitFor(() => {
    const f = document.querySelector<HTMLFormElement>('form.qa-edit')
    if (f === null) throw new Error('no form')
    return f
  }, WAIT)
}

afterEach(() => {
  vi.unstubAllEnvs()
  window.localStorage.clear()
  window.sessionStorage.clear()
  cleanup()
})

describe('a QA leg’s entry query', () => {
  it('a target of entries edits one entry’s query and sends every entry back whole', async () => {
    const user = entry('prod-user', { entry_query: 'env=production' })
    const reader = entry('prod-reader')
    const calls = live([app([user, reader])])
    await mountPage()
    // The card says it beside the leg's address.
    await waitFor(() => expect(document.querySelector('[data-entry-query]')).not.toBeNull(), WAIT)
    expect(visible(document.querySelector('[data-entry-query]')!)).toContain('?env=production')
    const form = await openEdit()
    const mine = form.querySelector<HTMLInputElement>('input[name="entry_query-prod-user"]')
    const theirs = form.querySelector<HTMLInputElement>('input[name="entry_query-prod-reader"]')
    // MUTATION: leave `entry_query` out of `normEnvironment` and this opens empty.
    expect(mine?.value).toBe('env=production')
    expect(theirs?.value).toBe('')
    fireEvent.change(theirs!, { target: { value: '?env=production' } })
    fireEvent.submit(form)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PATCH').length).toBe(1), WAIT)
    const body = calls.find((c) => c.method === 'PATCH')!.body as Json
    // MUTATION: send `entry_query` beside the entries and the API refuses it on a target of entries.
    expect(Object.keys(body)).toEqual(['environments'])
    const sent = body.environments as Json[]
    expect(sent.map((e) => e.name)).toEqual(['prod-user', 'prod-reader'])
    expect(sent[0]).toEqual({ name: 'prod-user', base_url: 'https://studio.app.example.test', environment: 'production',
      sign_in: SIGN_IN, rate: { navigation_ms: 3000 }, entry_query: 'env=production' })
    // Stored without its `?`; nothing of status (approval, credential) is sent back.
    expect(sent[1]).toEqual({ name: 'prod-reader', base_url: 'https://studio.app.example.test', environment: 'production',
      sign_in: SIGN_IN, rate: { navigation_ms: 3000 }, entry_query: 'env=production' })
  })

  it('a one-origin target sends it beside the address, and an emptied one as null', async () => {
    const calls = live([shop({ entry_query: 'env=production' })])
    await mountPage()
    const form = await openEdit()
    const input = form.querySelector<HTMLInputElement>('input[name="entry_query"]')
    expect(input?.value).toBe('env=production')
    fireEvent.change(input!, { target: { value: '' } })
    fireEvent.submit(form)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PATCH').length).toBe(1), WAIT)
    // MUTATION: leave the field out of the edit's diff and the change is never sent.
    expect(calls.find((c) => c.method === 'PATCH')!.body).toEqual({ entry_query: null })
  })

  it('a one-origin target with none sends a typed one', async () => {
    const calls = live([shop()])
    await mountPage()
    const form = await openEdit()
    fireEvent.change(form.querySelector('input[name="entry_query"]')!, { target: { value: 'env=production&lang=en' } })
    fireEvent.submit(form)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PATCH').length).toBe(1), WAIT)
    expect(calls.find((c) => c.method === 'PATCH')!.body).toEqual({ entry_query: 'env=production&lang=en' })
  })
})
