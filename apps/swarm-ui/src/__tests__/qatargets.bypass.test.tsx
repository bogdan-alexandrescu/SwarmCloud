// WORK › REPOSITORIES › QA TARGETS: a target's challenge bypass (lane
// CAPTCHA-BYPASS, part of #2058; docs/design/visual-qa-any-app.md §2.5).
//
// WHAT EACH CASE HOLDS:
//   * a target's card says its bypass's kind and name, and whether its value
//     is set (when, by whom), and never a value, even one a reply (wrongly)
//     carried;
//   * the edit form draws the bypass's kind and name and sends them as
//     `challenge_bypass: {kind, name}`, or null to remove it;
//   * its value field is write-only: uncontrolled, a password input, emptied
//     on submit, and its value goes in ONE request, the PUT to the target's
//     own `/challenge-bypass`, after the target is saved; never in the
//     registration or the change.
//
// The bodies are routes/qatargets.py's shapes. Every value is built at run
// time; nothing here is a credential.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { repo, serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const SHOP = 'qat_' + 'a1'.repeat(8)
const BLOG = 'qat_' + 'b2'.repeat(8)
const REPO_ID = 'repo_' + '0'.repeat(16)

type Json = Record<string, unknown>

/** A bypass value, built at runtime so nothing here is a literal credential. */
function runtimeValue(n = 32): string {
  return 'qa-' + Array.from({ length: n }, (_, i) => 'abcdefghjkmnpqrstuvwxyz23456789'[(i * 11 + n) % 31]).join('') + String(Date.now() % 997)
}

function me(): Json {
  return {
    tenant: { tenant_id: 'eng', kind: 'group', principal: 'eng@example.com', display_name: null },
    principal: { email: 'vera@example.com', domain: 'example.com', groups: [], is_admin: false, is_admin_unresolved: false },
    environment: 'dev',
    environment_declared: true,
    role: { name: 'admin', source: 'override' },
  }
}

const UNSET = { set: false, set_at: null, set_by: null, version: null }

function target(id: string, name: string, over: Json = {}): Json {
  return {
    id,
    tenant_id: 'eng',
    revision: 1,
    name,
    base_url: `https://${name}.example.test/`,
    host: `${name}.example.test`,
    host_state: 'approved',
    approved_by: 'ops@example.com',
    approved_at: '2026-10-22T09:00:00+00:00',
    approved_origins: [],
    repo_id: REPO_ID,
    repository: 'https://github.com/example-org/example-api',
    sign_in: { kind: 'none' },
    credential: UNSET,
    challenge_bypass_value: UNSET,
    authorized_by: "the app's owner",
    screens: [{ id: 'home', path: '/' }],
    ...over,
  }
}

function live(list: Json[]) {
  const calls = serve((m, url) => {
    const path = url.split('?')[0] ?? ''
    if (m === 'GET' && path === '/v1/tenants/me') return { status: 200, body: me() }
    if (m === 'GET' && path === '/v1/qa-targets') return { status: 200, body: { qa_targets: list } }
    if (m === 'GET' && path === '/v1/repositories') return { status: 200, body: { repositories: [repo({ repo_id: REPO_ID })] } }
    if (m === 'PATCH' && path.startsWith('/v1/qa-targets/')) return { status: 200, body: { qa_target: list[0] } }
    if (m === 'PUT' && path.endsWith('/challenge-bypass')) {
      return { status: 200, body: { challenge_bypass_value: { set: true, set_at: '2026-10-23T09:30:00+00:00', set_by: 'vera@example.com', version: '4' } } }
    }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return calls
}

async function mountPage(n: number) {
  const { RepositoriesScreen } = await import('../Repositories')
  render(<RepositoriesScreen view="page=qa-targets" go={vi.fn()} />)
  await waitFor(() => expect(document.querySelectorAll('.qa-target').length).toBe(n), WAIT)
}

function card(id: string): HTMLElement {
  const el = document.querySelector<HTMLElement>(`.qa-target[data-target-id="${id}"]`)
  if (el === null) throw new Error(`no card for ${id}`)
  return el
}

function button(root: ParentNode, label: string): HTMLButtonElement | null {
  return Array.from(root.querySelectorAll<HTMLButtonElement>('button')).find((b) => visible(b) === label) ?? null
}

async function edit(id: string): Promise<HTMLFormElement> {
  await waitFor(() => expect(button(card(id), 'Edit')).not.toBeNull(), WAIT)
  fireEvent.click(button(card(id), 'Edit')!)
  return waitFor(() => {
    const f = document.querySelector<HTMLFormElement>('form.qa-edit')
    if (f === null) throw new Error('no edit form')
    return f
  }, WAIT)
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.localStorage.clear()
  window.sessionStorage.clear()
  window.history.replaceState(null, '', '/')
})

describe('QA targets: the challenge bypass (CAPTCHA-BYPASS, §2.5)', () => {
  it('a card says the bypass and whether its value is set, and never a value', async () => {
    const leaked = runtimeValue()
    live([
      target(SHOP, 'shop', {
        challenge_bypass: { kind: 'header', name: 'X-QA-Bypass' },
        // A reply that (wrongly) carried a value beside the status.
        challenge_bypass_value: { set: true, set_at: '2026-10-23T09:00:00+00:00', set_by: 'owner@example.com', version: '2', value: leaked },
      }),
      target(BLOG, 'blog', { challenge_bypass: { kind: 'cookie', name: 'qa_bypass' } }),
    ])
    await mountPage(2)
    const shop = visible(card(SHOP))
    // MUTATION: drop the bypass line from TargetCard and the owner cannot tell a bypass is configured.
    expect(shop).toContain('Challenge bypass · header X-QA-Bypass · value set')
    expect(shop).toContain('by owner@example.com')
    // MUTATION: say "set" whatever the status and an unset value reads as configured.
    expect(visible(card(BLOG))).toContain('Challenge bypass · cookie qa_bypass · value not set')
    // MUTATION: copy the reply's status object whole in `normQaTarget` and the value reaches the page.
    expect(document.documentElement.outerHTML).not.toContain(leaked)
  })

  it('the edit form sends the kind and name, then the value alone to the bypass PUT, and empties the field', async () => {
    const calls = live([target(SHOP, 'shop')])
    await mountPage(1)
    const form = await edit(SHOP)
    fireEvent.change(form.querySelector('[name="bypass_kind"]')!, { target: { value: 'header' } })
    fireEvent.change(form.querySelector('[name="bypass_name"]')!, { target: { value: ' X-QA-Bypass ' } })
    const field = form.querySelector<HTMLInputElement>('input[name="bypass_value"]')!
    // MUTATION: make the value field a text input and it is drawn as typed.
    expect(field.type).toBe('password')
    const value = runtimeValue()
    field.value = value
    fireEvent.submit(form)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PUT').length).toBe(1), WAIT)
    const patch = calls.find((c) => c.method === 'PATCH')!
    // MUTATION: send the bypass under another field, or untrimmed.
    expect(patch.body).toEqual({ challenge_bypass: { kind: 'header', name: 'X-QA-Bypass' } })
    const sent = calls.find((c) => c.method === 'PUT')!
    // MUTATION: PUT the value to the credential route, or another target's.
    expect(sent.url).toBe(`/v1/qa-targets/${SHOP}/challenge-bypass`)
    expect(sent.body).toEqual({ value })
    // MUTATION: put the value in the change's body too and it travels twice.
    expect(JSON.stringify(calls.filter((c) => c.method !== 'PUT').map((c) => c.body))).not.toContain(value)
    // MUTATION: keep the typed value in the field after submit.
    expect(field.value).toBe('')
    expect(document.documentElement.outerHTML).not.toContain(value)
  })

  it('an edit that leaves the value empty sends no PUT, and None removes the bypass', async () => {
    const calls = live([target(SHOP, 'shop', { challenge_bypass: { kind: 'header', name: 'X-QA-Bypass' } })])
    await mountPage(1)
    const form = await edit(SHOP)
    // MUTATION: drop the bypass from `draftOf` and the form opens on None.
    expect(form.querySelector<HTMLSelectElement>('[name="bypass_kind"]')!.value).toBe('header')
    expect(form.querySelector<HTMLInputElement>('[name="bypass_name"]')!.value).toBe('X-QA-Bypass')
    expect(visible(form)).toContain('value not set')
    fireEvent.change(form.querySelector('[name="bypass_kind"]')!, { target: { value: 'none' } })
    fireEvent.submit(form)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PATCH').length).toBe(1), WAIT)
    // MUTATION: leave the bypass out of the change when it is removed and it is never cleared.
    expect(calls.find((c) => c.method === 'PATCH')!.body).toEqual({ challenge_bypass: null })
    // MUTATION: PUT an empty value and the API refuses it.
    expect(calls.filter((c) => c.method === 'PUT')).toEqual([])
  })
})
