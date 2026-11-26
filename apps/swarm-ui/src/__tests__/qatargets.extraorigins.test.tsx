// WORK › REPOSITORIES › QA TARGETS: a leg's extra origins.
//
// A leg may declare `extra_origins`, other `https://host[:port]` origins its
// pages use, beside its sign-in: a top-level field of a one-origin target, a
// field of each entry of a target with environments. The console:
//   * shows the list served in the leg's "Extra origins" box, one a line, and
//     sends it back exactly as read, or as edited (null clears a one-origin
//     target's; an entry left without them omits the field);
//   * in Approve host, draws a box per declared extra origin, ticked when the
//     leg's approval already covers it, and sends the ticked ones alone.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { allIn, helpIn, oneIn, shownIn } from './proseruns'
import { repo, serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const ONE = 'qat_' + '61'.repeat(8)
const MANY = 'qat_' + '62'.repeat(8)
const REPO_ID = 'repo_' + '0'.repeat(16)

const API = 'https://api.example.com'
const FILES = 'https://files.example.com:8443'
const CDN = 'https://cdn.example.com'

type Json = Record<string, unknown>

const UNSET = { set: false, set_at: null, set_by: null, version: null }

function me(role: string, platformAdmin: boolean): Json {
  return {
    tenant: { tenant_id: 'eng', kind: 'group', principal: 'eng@example.com', display_name: null },
    principal: { email: 'vera@example.com', domain: 'example.com', groups: [], is_admin: platformAdmin, is_admin_unresolved: false },
    environment: 'dev',
    environment_declared: true,
    role: { name: role, source: 'override' },
  }
}

/** A target served in the one-origin shape, its extra origins top-level. */
function oneOrigin(over: Json = {}): Json {
  return {
    id: ONE,
    tenant_id: 'eng',
    revision: 1,
    name: 'shop',
    base_url: 'https://shop.example.com',
    host: 'shop.example.com',
    host_state: 'approved',
    approved_by: 'ops@example.com',
    approved_at: '2026-10-22T09:00:00+00:00',
    approved_origins: ['https://shop.example.com'],
    approved_idp_token_path: null,
    repo_id: REPO_ID,
    repository: 'https://github.com/example-org/example-api',
    authorized_by: "the app's owner",
    sign_in: { kind: 'none' },
    extra_origins: [API, FILES],
    credential: UNSET,
    challenge_bypass_value: UNSET,
    screens: [{ id: 'home', path: '/' }],
    environment: 'production',
    ...over,
  }
}

/** One entry as `_entry_api` serves it. */
function entry(name: string, over: Json = {}): Json {
  return {
    name,
    base_url: 'https://studio.example.com',
    host: 'studio.example.com',
    environment: 'production',
    sign_in: { kind: 'none' },
    host_state: 'approved',
    approved_by: 'ops@example.com',
    approved_at: '2026-10-22T09:00:00+00:00',
    approved_origins: ['https://studio.example.com'],
    approved_idp_token_path: null,
    credential: UNSET,
    challenge_bypass_value: UNSET,
    rate: { navigation_ms: 3000 },
    discovered: null,
    new_since_last_run: [],
    discovered_at: null,
    discovering: null,
    discovery_error: null,
    ...over,
  }
}

const PROD_SPEC = { name: 'prod', base_url: 'https://studio.example.com', environment: 'production',
  sign_in: { kind: 'none' }, extra_origins: [API, FILES], rate: { navigation_ms: 3000 } }
const STAGE_SPEC = { name: 'stage', base_url: 'https://studio.example.com', environment: 'production',
  sign_in: { kind: 'none' }, rate: { navigation_ms: 3000 } }

function ofEntries(): Json {
  return {
    id: MANY,
    tenant_id: 'eng',
    revision: 2,
    name: 'studio',
    repo_id: REPO_ID,
    repository: 'https://github.com/example-org/example-api',
    authorized_by: "the app's owner",
    screens: [{ id: 'home', path: '/' }],
    environments: [entry('prod', { extra_origins: [API, FILES] }), entry('stage')],
  }
}

function live(target: Json, opts: { role?: string; platformAdmin?: boolean } = {}) {
  const calls = serve((m, url) => {
    const path = url.split('?')[0] ?? ''
    if (m === 'GET' && path === '/v1/tenants/me') return { status: 200, body: me(opts.role ?? 'admin', opts.platformAdmin === true) }
    if (m === 'GET' && path === '/v1/qa-targets') return { status: 200, body: { qa_targets: [target] } }
    if (m === 'GET' && path === '/v1/repositories') return { status: 200, body: { repositories: [repo({ repo_id: REPO_ID })] } }
    if (m === 'PATCH' && path.startsWith('/v1/qa-targets/')) return { status: 200, body: { qa_target: target } }
    if (m === 'POST' && path.endsWith(':approve-host')) return { status: 200, body: { qa_target: { ...target, host_state: 'approved' } } }
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

function field<T extends HTMLInputElement | HTMLTextAreaElement>(form: HTMLFormElement, name: string): T {
  const el = form.querySelector<T>(`[name="${name}"]`)
  if (el === null) throw new Error(`no field ${name}`)
  return el
}

function labelOf(form: HTMLFormElement, el: Element): string {
  const label = Array.from(form.querySelectorAll('label')).find((l) => l.htmlFor === el.id)
  return label === undefined ? '' : visible(label)
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

describe('a QA leg’s extra origins in the edit form', () => {
  it('a one-origin target shows its list in Extra origins, and sends an edited list top-level', async () => {
    const calls = live(oneOrigin())
    await mountPage()
    const form = await openEdit()
    const box = field<HTMLTextAreaElement>(form, 'extra_origins')
    expect(labelOf(form, box)).toBe('Extra origins')
    // MUTATION: draft the field from nothing and the served list is lost on screen.
    expect(box.value.split('\n')).toEqual([API, FILES])
    expect(form.querySelector('[data-extra-origin-reset]')).toBeNull()
    fireEvent.change(box, { target: { value: `${API},\n ${CDN} ` } })
    expect(form.querySelectorAll('[data-extra-origin-reset]').length).toBe(1)
    fireEvent.submit(form)
    const body = await patched(calls)
    // MUTATION: put it in the sign-in, or send it untrimmed, and the API reads another field.
    expect(body).toEqual({ extra_origins: [API, CDN] })
  })

  it('a one-origin target’s other change leaves the list alone; an emptied box sends null', async () => {
    const calls = live(oneOrigin())
    await mountPage()
    let form = await openEdit()
    fireEvent.change(field(form, 'name'), { target: { value: 'shop-app' } })
    fireEvent.submit(form)
    // MUTATION: send the unchanged list on every change and each save resets the host approval.
    expect(await patched(calls)).toEqual({ name: 'shop-app' })
    cleanup()
    const again = live(oneOrigin())
    await mountPage()
    form = await openEdit()
    fireEvent.change(field(form, 'extra_origins'), { target: { value: '  ' } })
    fireEvent.submit(form)
    expect(await patched(again)).toEqual({ extra_origins: null })
  })

  it('a leg of a target with environments shows its list, sends it back exactly as read, and edited when edited', async () => {
    const calls = live(ofEntries())
    await mountPage()
    let form = await openEdit()
    const box = field<HTMLTextAreaElement>(form, 'extra_origins-prod')
    expect(labelOf(form, box)).toBe('Extra origins')
    expect(box.value.split('\n')).toEqual([API, FILES])
    expect(field<HTMLTextAreaElement>(form, 'extra_origins-stage').value).toBe('')
    // No top-level box beside the legs.
    expect(form.querySelector('[name="extra_origins"]')).toBeNull()
    // A change of the other leg sends every entry back whole.
    fireEvent.change(field(form, 'entry_query-stage'), { target: { value: 'env=stage' } })
    fireEvent.submit(form)
    const body = await patched(calls)
    expect(Object.keys(body)).toEqual(['environments'])
    const sent = body.environments as Json[]
    // MUTATION: drop extra_origins from the entry's spec and an unrelated change clears them.
    expect(sent[0]).toEqual(PROD_SPEC)
    expect(sent[1]).toEqual({ ...STAGE_SPEC, entry_query: 'env=stage' })

    cleanup()
    const again = live(ofEntries())
    await mountPage()
    form = await openEdit()
    fireEvent.change(field(form, 'extra_origins-prod'), { target: { value: `${FILES}\n${CDN}` } })
    fireEvent.change(field(form, 'extra_origins-stage'), { target: { value: API } })
    const notes = Array.from(form.querySelectorAll('[data-extra-origin-reset]'))
    expect(notes.map((n) => visible(n))).toEqual([
      'prod’s host approval will be asked again.',
      'stage’s host approval will be asked again.',
    ])
    fireEvent.submit(form)
    const edited = (await patched(again)).environments as Json[]
    expect(edited[0]).toEqual({ ...PROD_SPEC, extra_origins: [FILES, CDN] })
    expect(edited[1]).toEqual({ ...STAGE_SPEC, extra_origins: [API] })
  })

  it('an entry’s emptied list leaves the field out of that entry', async () => {
    const calls = live(ofEntries())
    await mountPage()
    const form = await openEdit()
    fireEvent.change(field(form, 'extra_origins-prod'), { target: { value: '' } })
    fireEvent.submit(form)
    const sent = (await patched(calls)).environments as Json[]
    expect(sent[0]).not.toHaveProperty('extra_origins')
    const { extra_origins: _gone, ...rest } = PROD_SPEC
    expect(sent[0]).toEqual(rest)
  })
})

describe('Approve host names each declared extra origin', () => {
  function approveForm(): HTMLFormElement {
    const f = oneIn<HTMLFormElement>(document, 'form.qa-approve')
    if (f === null) throw new Error('no approve form')
    return f
  }

  function boxes(form: HTMLFormElement): HTMLInputElement[] {
    return Array.from(form.querySelectorAll<HTMLInputElement>('input[type="checkbox"][name="confirm_extra_origins"]'))
  }

  it('draws a box per origin, labelled with it, and sends only the ticked one', async () => {
    const calls = live(oneOrigin({ host_state: 'pending', approved_by: null, approved_at: null, approved_origins: [] }), { role: 'member', platformAdmin: true })
    await mountPage()
    await waitFor(() => expect(button(document, 'Approve host')).not.toBeNull(), WAIT)
    fireEvent.click(button(document, 'Approve host')!)
    const form = approveForm()
    const list = boxes(form)
    expect(list.map((b) => b.value)).toEqual([API, FILES])
    expect(list.map((b) => visible(b.closest('label')))).toEqual([API, FILES])
    expect(list.every((b) => !b.checked)).toBe(true)
    fireEvent.click(list[1]!)
    fireEvent.change(form.querySelector<HTMLInputElement>('input[name="confirm_host"]')!, { target: { value: 'shop.example.com' } })
    fireEvent.submit(form)
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST').length).toBe(1), WAIT)
    const sent = calls.find((c) => c.method === 'POST')!
    expect(sent.url).toBe(`/v1/qa-targets/${ONE}:approve-host`)
    // MUTATION: send every declared origin, or none, and the admin's choice is not what is approved.
    expect(sent.body).toEqual({ host: 'shop.example.com', extra_origins: [FILES] })
  })

  it('an approved leg with extra origins can be approved again, its approved ones ticked', async () => {
    const calls = live(ofEntries(), { role: 'member', platformAdmin: true })
    await mountPage()
    // MUTATION: keep canApprove to a pending host or a login's extra origin and an approved leg never offers it.
    await waitFor(() => expect(button(document, 'Approve again')).not.toBeNull(), WAIT)
    fireEvent.click(button(document, 'Approve again')!)
    const form = approveForm()
    expect(form.getAttribute('data-environment-approve')).toBe('prod')
    expect(boxes(form).map((b) => b.value)).toEqual([API, FILES])
    fireEvent.click(boxes(form)[0]!)
    fireEvent.change(form.querySelector<HTMLInputElement>('input[name="confirm_host"]')!, { target: { value: 'studio.example.com' } })
    fireEvent.submit(form)
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST').length).toBe(1), WAIT)
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ host: 'studio.example.com', environment: 'prod', extra_origins: [API] })
  })

  it('ticks an origin its approval already covers', async () => {
    live(oneOrigin({ approved_origins: ['https://shop.example.com', FILES] }), { role: 'member', platformAdmin: true })
    await mountPage()
    await waitFor(() => expect(button(document, 'Approve again')).not.toBeNull(), WAIT)
    fireEvent.click(button(document, 'Approve again')!)
    expect(boxes(approveForm()).map((b) => [b.value, b.checked])).toEqual([
      [API, false],
      [FILES, true],
    ])
  })
})

// Epic 21: a mutating flow on a non-production leg writes to each approved
// extra origin as well as the base one, so the approval says so, once.
describe('Approve host says where a non-production leg’s writes reach', () => {
  const PENDING = { host_state: 'pending', approved_by: null, approved_at: null, approved_origins: [] }

  async function openApprove(target: Json): Promise<HTMLFormElement> {
    live(target, { role: 'member', platformAdmin: true })
    await mountPage()
    await waitFor(() => expect(button(document, 'Approve host')).not.toBeNull(), WAIT)
    fireEvent.click(button(document, 'Approve host')!)
    const f = oneIn<HTMLFormElement>(document, 'form.qa-approve')
    if (f === null) throw new Error('no approve form')
    return f
  }

  function notices(form: HTMLFormElement): string[] {
    return allIn(form, '[data-writes-notice]').map((n) => visible(n))
  }

  it('shows one sentence for a non-production leg with extra origins', async () => {
    const form = await openApprove(oneOrigin({ ...PENDING, environment: 'non_production' }))
    // MUTATION: drop the sentence, or show it for every leg, and this or the next test fails.
    const notice = oneIn(form, '[data-writes-notice]')
    expect(shownIn(notice)).toBe('Flows here may write to each ticked host.')
    // Its detail sits behind the line's (i) (issue 81).
    expect(helpIn(notice)).toBe(
      'Flows on this environment may write, and their writes reach each ticked host as well as shop.example.com, a shared API among them.',
    )
    expect(notices(form)).toHaveLength(1)
  })

  it('is absent for a production leg', async () => {
    const form = await openApprove(oneOrigin({ ...PENDING, environment: 'production' }))
    expect(notices(form)).toEqual([])
  })

  it('is absent for a non-production leg that declares no extra origins', async () => {
    const form = await openApprove(oneOrigin({ ...PENDING, environment: 'non_production', extra_origins: [] }))
    expect(notices(form)).toEqual([])
  })
})
