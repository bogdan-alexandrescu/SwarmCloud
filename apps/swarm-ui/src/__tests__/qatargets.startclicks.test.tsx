// WORK › REPOSITORIES › QA TARGETS: a leg's start clicks.
//
// Reopening a leg draws each start click's text in its text field and its css
// in its css field; a save of the form as shown keeps both, and removing one
// row leaves the others with their own values.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { repo, serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const APP = 'qat_' + '47'.repeat(8)
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

// Two clicks whose text and css differ, so a field showing the other's value is red.
const CLICKS = [
  { text: 'Staging', css: 'button.env' },
  { text: 'Sign in', css: 'a.sso' },
]
const SIGN_IN = { kind: 'own_login', login_path: '/', signed_in_selector: 'nav .me', start_clicks: CLICKS }

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

describe('a QA leg’s start clicks', () => {
  const field = (form: HTMLFormElement, name: string) => form.querySelector<HTMLInputElement>(`input[name="${name}-prod-user"]`)
  const shown = (form: HTMLFormElement) =>
    [0, 1, 2].flatMap((i) => {
      const text = field(form, `start_click_text_${i}`)
      return text === null ? [] : [{ text: text.value, css: field(form, `start_click_css_${i}`)?.value }]
    })

  it('reopens each click with its text in the text field and its css in the css field, and a save keeps both', async () => {
    const calls = live([app([entry('prod-user'), entry('prod-reader')])])
    await mountPage()
    const form = await openEdit()
    // MUTATION: draft a click's css from its text and the css fields show "Staging" and "Sign in".
    expect(shown(form)).toEqual(CLICKS)
    // A change elsewhere in the leg's sign-in sends it back rebuilt from the fields as shown.
    fireEvent.change(field(form, 'signed_in_selector')!, { target: { value: 'nav .account' } })
    fireEvent.submit(form)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PATCH').length).toBe(1), WAIT)
    const sent = (calls.find((c) => c.method === 'PATCH')!.body as Json).environments as Json[]
    // MUTATION: map css from text on save and the css values are overwritten with the text.
    expect((sent[0]!.sign_in as Json).start_clicks).toEqual(CLICKS)
    expect(sent[1]!.sign_in).toEqual(SIGN_IN)
  })

  it('removing the first click leaves the second with its own text and css', async () => {
    const calls = live([app([entry('prod-user'), entry('prod-reader')])])
    await mountPage()
    const form = await openEdit()
    const row = form.querySelector('[data-leg-sign-in="prod-user"]')!
    const secondText = field(form, 'start_click_text_1')!
    const secondCss = field(form, 'start_click_css_1')!
    fireEvent.click(row.querySelectorAll<HTMLButtonElement>('button[name="remove_start_click-prod-user"]')[0]!)
    // MUTATION: key the rows by index and the remaining row's fields shift.
    expect(shown(form)).toEqual([CLICKS[1]])
    // The second row keeps its own inputs: an index key unmounts them and writes its values into the first row's.
    expect([secondText.isConnected, secondText.value, secondCss.isConnected, secondCss.value]).toEqual([true, 'Sign in', true, 'a.sso'])
    fireEvent.submit(form)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PATCH').length).toBe(1), WAIT)
    const sent = (calls.find((c) => c.method === 'PATCH')!.body as Json).environments as Json[]
    expect((sent[0]!.sign_in as Json).start_clicks).toEqual([CLICKS[1]])
  })
})
