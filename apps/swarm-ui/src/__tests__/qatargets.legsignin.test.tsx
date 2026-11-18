// WORK › REPOSITORIES › QA TARGETS: a leg's sign-in in the edit form.
//
// A target stored with environments takes its legs' fields in `environments`
// alone (qatargets.patch: a `sign_in` beside them is refused), and each leg
// signs in its own way. The form therefore:
//   * draws no target-level Sign-in select for such a target;
//   * opens each leg's sign-in, folded in that leg's row, on the value stored;
//   * sends a change of one leg's sign-in inside `environments`, every other
//     entry exactly as it was read, and never a target-level `sign_in`;
//   * says beside a leg's extra origin, once it is changed, that the leg's host
//     approval is asked again (a host or extra origin change resets it).

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { repo, serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const APP = 'qat_' + '57'.repeat(8)
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

const USER_SIGN_IN = {
  kind: 'own_login',
  login_path: '/login',
  signed_in_selector: 'nav .me',
  username_selector: '#email',
  password_selector: '#password',
  submit_selector: 'button[type=submit]',
  start_clicks: [{ text: 'Production', css: 'button' }],
}
const ADMIN_SIGN_IN = {
  kind: 'own_login',
  login_path: '/',
  signed_in_selector: 'header .avatar',
  extra_origin: 'https://id.example.test',
  start_clicks: [{ text: 'Staff' }],
}

/** One entry as `_entry_api` serves it. */
function entry(name: string, signIn: Json, over: Json = {}): Json {
  return {
    name,
    base_url: 'https://studio.app.example.test',
    host: 'studio.app.example.test',
    environment: 'production',
    sign_in: signIn,
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

const USER = entry('prod-user', USER_SIGN_IN, { entry_query: 'env=production' })
const ADMIN = entry('prod-admin', ADMIN_SIGN_IN)
const PUBLIC = entry('prod-public', { kind: 'none' }, { environment: 'non_production' })

/** What the form must send back for an entry it did not change: its registration fields, as served. */
const USER_SPEC = { name: 'prod-user', base_url: 'https://studio.app.example.test', environment: 'production',
  sign_in: USER_SIGN_IN, rate: { navigation_ms: 3000 }, entry_query: 'env=production' }
const ADMIN_SPEC = { name: 'prod-admin', base_url: 'https://studio.app.example.test', environment: 'production',
  sign_in: ADMIN_SIGN_IN, rate: { navigation_ms: 3000 } }
const PUBLIC_SPEC = { name: 'prod-public', base_url: 'https://studio.app.example.test', environment: 'non_production',
  sign_in: { kind: 'none' }, rate: { navigation_ms: 3000 } }

function app(): Json {
  return {
    id: APP,
    tenant_id: 'eng',
    revision: 2,
    name: 'studio',
    repo_id: REPO_ID,
    repository: 'https://github.com/example-org/example-api',
    authorized_by: "the app's owner",
    screens: [{ id: 'home', path: '/' }],
    environments: [USER, ADMIN, PUBLIC],
  }
}

function live() {
  const target = app()
  const calls = serve((m, url) => {
    const path = url.split('?')[0] ?? ''
    if (m === 'GET' && path === '/v1/tenants/me') return { status: 200, body: me('admin') }
    if (m === 'GET' && path === '/v1/qa-targets') return { status: 200, body: { qa_targets: [target] } }
    if (m === 'GET' && path === '/v1/repositories') return { status: 200, body: { repositories: [repo({ repo_id: REPO_ID })] } }
    if (m === 'PATCH' && path.startsWith('/v1/qa-targets/')) return { status: 200, body: { qa_target: target } }
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

function field<T extends HTMLInputElement | HTMLSelectElement>(form: HTMLFormElement, name: string): T {
  const el = form.querySelector<T>(`[name="${name}"]`)
  if (el === null) throw new Error(`no field ${name}`)
  return el
}

async function patched(calls: ReturnType<typeof live>): Promise<Json> {
  await waitFor(() => expect(calls.filter((c) => c.method === 'PATCH').length).toBe(1), WAIT)
  return calls.find((c) => c.method === 'PATCH')!.body as Json
}

afterEach(() => {
  vi.unstubAllEnvs()
  window.localStorage.clear()
  window.sessionStorage.clear()
  cleanup()
})

describe('a QA leg’s sign-in in the edit form', () => {
  it('draws no target-level Sign-in and shows each leg’s sign-in as stored, folded in its row', async () => {
    live()
    await mountPage()
    const form = await openEdit()
    // MUTATION: draw the target-level select and it reads "Public pages, no sign-in" for a target whose legs sign in.
    expect(form.querySelector('select[name="kind"]')).toBeNull()
    expect(form.querySelector('input[name="signed_in_selector"]')).toBeNull()
    // Each leg's row folds its sign-in, the fold naming how it signs in.
    const folds = Array.from(form.querySelectorAll<HTMLDetailsElement>('details[data-leg-sign-in]'))
    expect(folds.map((f) => f.dataset.legSignIn)).toEqual(['prod-user', 'prod-admin', 'prod-public'])
    expect(folds.every((f) => !f.open)).toBe(true)
    expect(visible(folds[0]!.querySelector('summary')!)).toContain('The app’s own login')
    expect(visible(folds[2]!.querySelector('summary')!)).toContain('Public pages, no sign-in')
    // MUTATION: build a leg's draft from the target's sign_in and every selector opens empty.
    expect(field<HTMLSelectElement>(form, 'kind-prod-user').value).toBe('own_login')
    expect(field(form, 'login_path-prod-user').value).toBe('/login')
    expect(field(form, 'signed_in_selector-prod-user').value).toBe('nav .me')
    expect(field(form, 'username_selector-prod-user').value).toBe('#email')
    expect(field(form, 'password_selector-prod-user').value).toBe('#password')
    expect(field(form, 'submit_selector-prod-user').value).toBe('button[type=submit]')
    expect(field(form, 'start_click_text_0-prod-user').value).toBe('Production')
    expect(field(form, 'start_click_css_0-prod-user').value).toBe('button')
    expect(field(form, 'extra_origin-prod-user').value).toBe('')
    expect(field<HTMLSelectElement>(form, 'kind-prod-admin').value).toBe('own_login')
    expect(field(form, 'login_path-prod-admin').value).toBe('/')
    expect(field(form, 'signed_in_selector-prod-admin').value).toBe('header .avatar')
    expect(field(form, 'username_selector-prod-admin').value).toBe('')
    expect(field(form, 'extra_origin-prod-admin').value).toBe('https://id.example.test')
    expect(field(form, 'start_click_text_0-prod-admin').value).toBe('Staff')
    expect(field<HTMLSelectElement>(form, 'kind-prod-public').value).toBe('none')
    expect(form.querySelector('[name="signed_in_selector-prod-public"]')).toBeNull()
    // The same labels the one-origin form uses.
    const labels = Array.from(folds[0]!.querySelectorAll('label.ur-fl')).map((l) => visible(l))
    expect(labels).toEqual(expect.arrayContaining(['Sign-in', 'Login path', 'Username selector', 'Password selector', 'Submit selector']))
    expect(labels.some((l) => l.startsWith('Signed-in selector'))).toBe(true)
  })

  it('a change of one leg’s selector sends only that change, inside environments, the other legs as read', async () => {
    const calls = live()
    await mountPage()
    const form = await openEdit()
    fireEvent.change(field(form, 'signed_in_selector-prod-admin'), { target: { value: '  [data-me]  ' } })
    fireEvent.submit(form)
    const body = await patched(calls)
    // MUTATION: send a target-level `sign_in` and the API refuses a leg's field beside environments.
    expect(Object.keys(body)).toEqual(['environments'])
    const sent = body.environments as Json[]
    expect(sent.map((e) => e.name)).toEqual(['prod-user', 'prod-admin', 'prod-public'])
    // MUTATION: rebuild an unchanged leg's sign_in from its draft and its bytes may move.
    expect(JSON.stringify(sent[0])).toBe(JSON.stringify(USER_SPEC))
    expect(JSON.stringify(sent[2])).toBe(JSON.stringify(PUBLIC_SPEC))
    expect(sent[1]).toEqual({ ...ADMIN_SPEC, sign_in: { ...ADMIN_SIGN_IN, signed_in_selector: '[data-me]' } })
  })

  it('never sends a target-level sign_in for a target with environments', async () => {
    const calls = live()
    await mountPage()
    const form = await openEdit()
    // A change of the app's own field alone sends that field alone.
    fireEvent.change(field(form, 'name'), { target: { value: 'studio-app' } })
    // A leg switched to public pages carries nothing else in its sign_in.
    fireEvent.change(field(form, 'kind-prod-user'), { target: { value: 'none' } })
    fireEvent.submit(form)
    const body = await patched(calls)
    expect(Object.keys(body).sort()).toEqual(['environments', 'name'])
    expect(body).not.toHaveProperty('sign_in')
    const sent = body.environments as Json[]
    expect(sent[0]).toEqual({ ...USER_SPEC, sign_in: { kind: 'none' } })
    expect(JSON.stringify(sent[1])).toBe(JSON.stringify(ADMIN_SPEC))
  })

  it('a changed extra origin says the leg’s host approval is asked again, and is sent in that leg', async () => {
    const calls = live()
    await mountPage()
    const form = await openEdit()
    expect(form.querySelector('[data-extra-origin-reset]')).toBeNull()
    fireEvent.change(field(form, 'extra_origin-prod-admin'), { target: { value: 'https://sso.example.test' } })
    // MUTATION: drop the note and the reset of the leg's approval is a surprise.
    const notes = Array.from(form.querySelectorAll('[data-extra-origin-reset]'))
    expect(notes.length).toBe(1)
    expect(visible(notes[0]!)).toContain('prod-admin')
    expect(visible(notes[0]!)).toContain('approval')
    // Emptied back to what was stored, the note goes.
    fireEvent.change(field(form, 'extra_origin-prod-admin'), { target: { value: 'https://id.example.test' } })
    expect(form.querySelector('[data-extra-origin-reset]')).toBeNull()
    fireEvent.change(field(form, 'extra_origin-prod-admin'), { target: { value: ' https://sso.example.test ' } })
    fireEvent.submit(form)
    const body = await patched(calls)
    const sent = body.environments as Json[]
    expect(sent[1]).toEqual({ ...ADMIN_SPEC, sign_in: { ...ADMIN_SIGN_IN, extra_origin: 'https://sso.example.test' } })
    expect(JSON.stringify(sent[0])).toBe(JSON.stringify(USER_SPEC))
  })
})
