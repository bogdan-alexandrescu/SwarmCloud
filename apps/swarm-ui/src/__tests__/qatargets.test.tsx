// WORK › REPOSITORIES › QA TARGETS (lane VQ-6 CONSOLE-PLUGIN, part of #1933;
// docs/design/visual-qa-any-app.md §1, §2.1, §6).
//
// WHAT EACH CASE HOLDS:
//   * the page is mounted under Repositories (`page=qa-targets`) and the list
//     links to it;
//   * a workspace admin sees Register, Edit and Set credential; a member sees
//     Run and not Edit;
//   * a pending host shows why it cannot run, and Run sends nothing;
//   * Run POSTs `{file}` alone to the target's own `:run`;
//   * the credential form reads its link token from the URL and removes it
//     from the address bar, as ProviderKeys.tsx does, and sends it once as
//     `link_token` with the username and password, which never stay in the DOM;
//   * an admin's Set credential mints a link and opens the same form with the
//     token held in memory, never drawn;
//   * the registration form POSTs the fields VQ-2's `TargetBody` takes;
//   * a PLATFORM admin (`principal.is_admin`, whatever the workspace role)
//     sees Approve host on a pending target, types the host (and a login's
//     extra origin) to confirm, and only then POSTs `:approve-host`; anyone
//     else never sees it, and the API's refusal is said in its own words
//     (lane VQ6-APPROVE, §1.2, VQ-Q1).
//
// The bodies are routes/qatargets.py's and routes/repositories.py's shapes.
// Every token-shaped value is built at runtime; nothing here is a credential.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/react'
import { repo, serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const APPROVED = 'qat_' + 'a1'.repeat(8)
const PENDING = 'qat_' + 'b2'.repeat(8)
const LOGIN = 'qat_' + 'c3'.repeat(8)
const REPO_ID = 'repo_' + '0'.repeat(16)

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null

/** A link token or a password, built at runtime so nothing here is a literal credential. */
function runtimeValue(n = 24): string {
  return Array.from({ length: n }, (_, i) => 'abcdefghjkmnpqrstuvwxyz23456789'[(i * 7 + n) % 31]).join('') + String(Date.now() % 997)
}

function me(role: string, platformAdmin = false): Json {
  return {
    tenant: { tenant_id: 'eng', kind: 'group', principal: 'eng@example.com', display_name: null },
    principal: { email: 'vera@example.com', domain: 'example.com', groups: [], is_admin: platformAdmin, is_admin_unresolved: false },
    environment: 'dev',
    environment_declared: true,
    role: { name: role, source: 'override' },
  }
}

/** A target as VQ-2's `to_api` serves it. */
function target(id: string, name: string, over: Json = {}): Json {
  const approved = over.host_state !== 'pending'
  return {
    id,
    tenant_id: 'eng',
    revision: 1,
    name,
    base_url: `https://${name}.example.test/`,
    host: `${name}.example.test`,
    host_state: 'approved',
    approved_by: approved ? 'ops@example.com' : null,
    approved_at: approved ? '2026-10-22T09:00:00+00:00' : null,
    approved_origins: [],
    repo_id: REPO_ID,
    repository: 'https://github.com/example-org/example-api',
    sign_in: { kind: 'none' },
    credential: { set: false, set_at: null, set_by: null, version: null },
    authorized_by: "the app's owner",
    screens: [{ id: 'home', path: '/' }],
    ...over,
  }
}

function targets(pendingOver: Json = {}): Json {
  return {
    qa_targets: [
      target(APPROVED, 'shop', { themes: ['light'] }),
      target(PENDING, 'blog', { host_state: 'pending', ...pendingOver }),
      target(LOGIN, 'portal', { sign_in: { kind: 'own_login', login_path: '/login', signed_in_selector: '#me' } }),
    ],
  }
}

function live(
  role: string,
  opts: { run?: Reply; link?: Reply; put?: Reply; create?: Reply; platformAdmin?: boolean; approve?: Reply; pending?: Json } = {},
) {
  const calls = serve((m, url) => {
    const path = url.split('?')[0] ?? ''
    if (m === 'GET' && path === '/v1/tenants/me') return { status: 200, body: me(role, opts.platformAdmin === true) }
    if (m === 'GET' && path === '/v1/qa-targets') return { status: 200, body: targets(opts.pending) }
    if (m === 'POST' && path.endsWith(':approve-host')) {
      return opts.approve ?? { status: 200, body: { qa_target: target(PENDING, 'blog', { approved_by: 'root@example.com', ...opts.pending, host_state: 'approved' }) } }
    }
    if (m === 'GET' && path === '/v1/repositories') return { status: 200, body: { repositories: [repo({ repo_id: REPO_ID })] } }
    if (m === 'POST' && path.endsWith(':run')) return opts.run ?? { status: 201, body: { workflow_id: 'wf_' + 'd4'.repeat(8), qa_target: {}, file: 'issues', browser_steps: [] } }
    if (m === 'POST' && path.endsWith('/credential-links')) return opts.link ?? null
    if (m === 'PUT' && path.endsWith('/credential')) return opts.put ?? { status: 200, body: { credential: { set: true, set_at: '2026-10-22T09:30:00+00:00', set_by: 'vera@example.com', version: '3' } } }
    if (m === 'PATCH' && path.startsWith('/v1/qa-targets/')) return { status: 200, body: { qa_target: target(APPROVED, 'shop-2') } }
    if (m === 'POST' && path === '/v1/qa-targets') return opts.create ?? { status: 201, body: { qa_target: target('qat_' + 'e5'.repeat(8), 'new', { host_state: 'pending' }) } }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return calls
}

async function mountPage() {
  const { RepositoriesScreen } = await import('../Repositories')
  render(<RepositoriesScreen view="page=qa-targets" go={vi.fn()} />)
  await waitFor(() => expect(document.querySelectorAll('.qa-target').length).toBe(3), WAIT)
}

function card(id: string): HTMLElement {
  const el = document.querySelector<HTMLElement>(`.qa-target[data-target-id="${id}"]`)
  if (el === null) throw new Error(`no card for ${id}`)
  return el
}

function button(root: ParentNode, label: string): HTMLButtonElement | null {
  return Array.from(root.querySelectorAll<HTMLButtonElement>('button')).find((b) => visible(b) === label) ?? null
}

afterEach(() => {
  vi.unstubAllEnvs()
  window.localStorage.clear()
  window.sessionStorage.clear()
  window.history.replaceState(null, '', '/')
})

describe('QA targets: the page and who sees what', () => {
  it('the Repositories list links to the QA targets page', async () => {
    live('member')
    const { RepositoriesScreen } = await import('../Repositories')
    render(<RepositoriesScreen view={null} go={vi.fn()} />)
    // MUTATION: drop the QA targets button from the list's head and this is null.
    await waitFor(() => expect(document.querySelector('a[href="/repositories?page=qa-targets"]')).not.toBeNull(), WAIT)
  })

  it('an admin sees Register, Run, Edit and Set credential', async () => {
    live('admin')
    await mountPage()
    await waitFor(() => expect(button(card(APPROVED), 'Edit')).not.toBeNull(), WAIT)
    expect(button(card(APPROVED), 'Run')).not.toBeNull()
    // MUTATION: offer Set credential on a target with no own login and this is not null.
    expect(button(card(APPROVED), 'Set credential')).toBeNull()
    expect(button(card(LOGIN), 'Set credential')).not.toBeNull()
    expect(button(document, 'Register QA target')).not.toBeNull()
  })

  it('a member sees Run and not Edit', async () => {
    const calls = live('member')
    await mountPage()
    await waitFor(() => expect(document.querySelector('[data-role-gate="admin"]')).not.toBeNull(), WAIT)
    const shop = card(APPROVED)
    // MUTATION: gate Run on admin too and a member cannot run.
    expect(button(shop, 'Run')).not.toBeNull()
    expect(button(shop, 'Run')!.disabled).toBe(false)
    // MUTATION: drop the admin gate on Edit (draw it for every role) and these are not null.
    expect(button(shop, 'Edit')).toBeNull()
    expect(button(card(LOGIN), 'Set credential')).toBeNull()
    expect(button(document, 'Register QA target')).toBeNull()
    expect(button(shop, 'Delete')).toBeNull()
    expect(calls.filter((c) => c.method !== 'GET')).toEqual([])
  })

  it('a pending host shows why it cannot run, and Run sends nothing', async () => {
    const calls = live('admin')
    await mountPage()
    const blog = card(PENDING)
    expect(blog.getAttribute('data-host-state')).toBe('pending')
    const why = blog.querySelector('[data-cannot-run]')
    // MUTATION: drop the pending reason and the card says nothing about why.
    expect(why).not.toBeNull()
    expect(visible(why)).toContain('blog.example.test')
    expect(visible(why)).toContain('platform admin')
    // MUTATION: leave Run enabled on a pending host and the click would POST :run.
    const run = button(blog, 'Run')!
    expect(run.disabled).toBe(true)
    fireEvent.click(run)
    expect(calls.filter((c) => c.method === 'POST')).toEqual([])
  })

  it('an own-login target with no credential says so instead of running', async () => {
    live('member')
    await mountPage()
    const portal = card(LOGIN)
    // MUTATION: treat an unset credential as runnable and Run is enabled.
    expect(button(portal, 'Run')!.disabled).toBe(true)
    expect(visible(portal.querySelector('[data-cannot-run]'))).toContain('credential')
  })

  it('Run posts `{file}` alone to the target’s own :run and names the workflow', async () => {
    const calls = live('member')
    await mountPage()
    fireEvent.click(button(card(APPROVED), 'Run')!)
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST').length).toBe(1), WAIT)
    const sent = calls.find((c) => c.method === 'POST')!
    // MUTATION: post to another target's id, or to the list route.
    expect(sent.url).toBe(`/v1/qa-targets/${APPROVED}:run`)
    // MUTATION: send anything besides `file` (an image, a command, the target's spec).
    expect(sent.body).toEqual({ file: 'issues' })
    await waitFor(() => expect(visible(card(APPROVED))).toContain('wf_' + 'd4'.repeat(8)), WAIT)
  })

  it('a refused run is said with the API’s own words', async () => {
    live('member', { run: { status: 403, body: { code: 'issue_writes_off', message: 'agents_may_file_issues is off' } } })
    await mountPage()
    fireEvent.click(button(card(APPROVED), 'Run')!)
    // MUTATION: swallow a refused run and nothing is said.
    await waitFor(() => expect(visible(card(APPROVED))).toContain('agents_may_file_issues is off'), WAIT)
  })
})

describe('QA targets: the credential form', () => {
  it('reads its link token from the URL, removes it from the bar and sends it once with the credential', async () => {
    const token = runtimeValue()
    window.history.replaceState(null, '', `/qa-targets/${LOGIN}/credential?t=${token}`)
    const calls = live('admin')
    const { QaTargetsScreen } = await import('../QaTargets')
    render(<QaTargetsScreen go={vi.fn()} />)
    // MUTATION: drop `stripLinkToken` and the token stays in the address bar.
    await waitFor(() => expect(window.location.search).not.toContain(token), WAIT)
    expect(window.location.href).not.toContain(token)
    const form = await waitFor(() => {
      const f = document.querySelector<HTMLFormElement>('form.qa-cred')
      if (f === null) throw new Error('no credential form')
      return f
    }, WAIT)
    expect(form.getAttribute('data-target-id')).toBe(LOGIN)
    const user = form.querySelector<HTMLInputElement>('input[name="username"]')!
    const pass = form.querySelector<HTMLInputElement>('input[name="password"]')!
    expect(pass.type).toBe('password')
    const password = runtimeValue(30)
    user.value = 'qa-bot@example.com'
    pass.value = password
    fireEvent.submit(form)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PUT').length).toBe(1), WAIT)
    const sent = calls.find((c) => c.method === 'PUT')!
    // MUTATION: PUT another target's id, or the links route.
    expect(sent.url).toBe(`/v1/qa-targets/${LOGIN}/credential`)
    // MUTATION: send the link token under another name, or not at all.
    expect(sent.body).toEqual({ username: 'qa-bot@example.com', password, link_token: token })
    // MUTATION: keep the typed values in the fields after submit.
    expect(pass.value).toBe('')
    expect(user.value).toBe('')
    await waitFor(() => expect(visible(document.body)).toContain('Credential saved'), WAIT)
    // MUTATION: draw the password or the token anywhere on the page.
    expect(document.documentElement.outerHTML).not.toContain(password)
    expect(document.documentElement.outerHTML).not.toContain(token)
  })

  it('an admin’s Set credential mints a link and holds its token in memory, never on the page', async () => {
    const token = runtimeValue()
    const calls = live('admin', {
      link: { status: 201, body: { url: null, path: `/qa-targets/${LOGIN}/credential?t=${token}`, expires_at: '2026-10-22T10:15:00+00:00' } },
    })
    await mountPage()
    await waitFor(() => expect(button(card(LOGIN), 'Set credential')).not.toBeNull(), WAIT)
    fireEvent.click(button(card(LOGIN), 'Set credential')!)
    await waitFor(() => expect(document.querySelector('form.qa-cred')).not.toBeNull(), WAIT)
    // MUTATION: mint for another target, or send a body.
    expect(calls.find((c) => c.method === 'POST')).toMatchObject({ url: `/v1/qa-targets/${LOGIN}/credential-links`, body: null })
    // MUTATION: draw the minted path (and its token) as a link on the page.
    expect(document.documentElement.outerHTML).not.toContain(token)
    const form = document.querySelector<HTMLFormElement>('form.qa-cred')!
    form.querySelector<HTMLInputElement>('input[name="username"]')!.value = 'qa-bot@example.com'
    form.querySelector<HTMLInputElement>('input[name="password"]')!.value = runtimeValue(18)
    fireEvent.submit(form)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PUT').length).toBe(1), WAIT)
    // MUTATION: drop the minted token from the PUT and the API refuses the write.
    expect((calls.find((c) => c.method === 'PUT')!.body as Json).link_token).toBe(token)
  })

  it('the Repositories mount hands the shell\u2019s link to the form, so a link read before the bar was rewritten still opens it', async () => {
    const token = runtimeValue()
    // The shell has already rewritten the bar to the page's own address.
    window.history.replaceState(null, '', '/repositories?page=qa-targets')
    const calls = live('admin')
    const { RepositoriesScreen } = await import('../Repositories')
    render(<RepositoriesScreen view="page=qa-targets" go={vi.fn()} qaLink={{ targetId: LOGIN, token }} />)
    // MUTATION: drop `link={qaLink}` from Repositories' mount and no form opens.
    const form = await waitFor(() => {
      const f = document.querySelector<HTMLFormElement>('form.qa-cred')
      if (f === null) throw new Error('no credential form')
      return f
    }, WAIT)
    expect(form.getAttribute('data-target-id')).toBe(LOGIN)
    form.querySelector<HTMLInputElement>('input[name="username"]')!.value = 'qa-bot@example.com'
    form.querySelector<HTMLInputElement>('input[name="password"]')!.value = runtimeValue(18)
    fireEvent.submit(form)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PUT').length).toBe(1), WAIT)
    // MUTATION: pass the mount a link without its token and the API refuses the write.
    expect((calls.find((c) => c.method === 'PUT')!.body as Json).link_token).toBe(token)
    expect(document.documentElement.outerHTML).not.toContain(token)
  })

  // KNOWN GAP, EXPECTED TO FAIL (review of VQ-6): the plugin's link is
  // `/qa-targets/<id>/credential?t=<token>` (VQ-2's `mint_link`), and
  // paths.ts `pathToAddress` has no route for it, so the console answers it
  // with Not found and the form never opens. Routing it needs paths.ts and
  // App.tsx, outside this lane's territory, and is asked of the owner in the
  // PR's questions. `it.fails` keeps CI green while the route is missing and
  // turns red the day it lands: then make this a plain `it`.
  it.fails('the console routes the plugin\u2019s credential link to the QA targets page, without its token', async () => {
    const { pathToAddress } = await import('../paths')
    const token = runtimeValue()
    const route = pathToAddress(`/qa-targets/${LOGIN}/credential`, `?t=${token}`)
    // MUTATION: route `/qa-targets/<id>/credential` in paths.ts and this passes (so `it.fails` goes red).
    expect(route).not.toBeNull()
    expect(route!.address).toBe('work/repositories?page=qa-targets')
    expect(route!.address).not.toContain(token)
  })

  it('the link parser reads only the credential path and never another `t`', async () => {
    const { qaCredentialLinkFrom } = await import('../qaTargetsApi')
    const token = runtimeValue()
    // MUTATION: accept any path with a `t` and these are not null.
    expect(qaCredentialLinkFrom('/settings/keys/typesafe', `?t=${token}`)).toBeNull()
    expect(qaCredentialLinkFrom(`/qa-targets/${LOGIN}`, `?t=${token}`)).toBeNull()
    expect(qaCredentialLinkFrom('/qa-targets/not-an-id/credential', `?t=${token}`)).toBeNull()
    expect(qaCredentialLinkFrom(`/qa-targets/${LOGIN}/credential`, `?t=${token}`)).toEqual({ targetId: LOGIN, token })
    expect(qaCredentialLinkFrom(`/qa-targets/${LOGIN}/credential`, '')).toEqual({ targetId: LOGIN, token: null })
  })
})

describe('QA targets: registration', () => {
  it('Register posts the fields VQ-2 takes, the repository as owner/repo', async () => {
    const calls = live('admin')
    await mountPage()
    await waitFor(() => expect(button(document, 'Register QA target')).not.toBeNull(), WAIT)
    fireEvent.click(button(document, 'Register QA target')!)
    const form = await waitFor(() => {
      const f = document.querySelector<HTMLFormElement>('form.qa-edit')
      if (f === null) throw new Error('no registration form')
      return f
    }, WAIT)
    await waitFor(() => expect(form.querySelectorAll('select[name="repository"] option').length).toBeGreaterThan(1), WAIT)
    const set = (name: string, value: string) => {
      const el = form.querySelector<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(`[name="${name}"]`)!
      fireEvent.change(el, { target: { value } })
    }
    set('name', 'new')
    set('base_url', 'https://new.example.test/')
    set('repository', 'example-org/example-api')
    set('authorized_by', "the app's owner")
    set('screens', 'home /\npricing /pricing')
    fireEvent.submit(form)
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST').length).toBe(1), WAIT)
    const sent = calls.find((c) => c.method === 'POST')!
    expect(sent.url).toBe('/v1/qa-targets')
    // MUTATION: send the repository as its URL or its id, or drop the screens' paths.
    expect(sent.body).toEqual({
      name: 'new',
      base_url: 'https://new.example.test/',
      repository: 'example-org/example-api',
      authorized_by: "the app's owner",
      sign_in: { kind: 'none' },
      screens: [
        { id: 'home', path: '/' },
        { id: 'pricing', path: '/pricing' },
      ],
      environment: 'production',
    })
    // MUTATION: never say a new host waits for approval.
    await waitFor(() => expect(visible(document.body)).toContain('waits for a platform admin'), WAIT)
  })
  it('More settings carries the other §1 fields, and an unknown one is refused before sending', async () => {
    const calls = live('admin')
    await mountPage()
    await waitFor(() => expect(button(document, 'Register QA target')).not.toBeNull(), WAIT)
    fireEvent.click(button(document, 'Register QA target')!)
    const form = await waitFor(() => {
      const f = document.querySelector<HTMLFormElement>('form.qa-edit')
      if (f === null) throw new Error('no registration form')
      return f
    }, WAIT)
    await waitFor(() => expect(form.querySelectorAll('select[name="repository"] option').length).toBeGreaterThan(1), WAIT)
    const set = (name: string, value: string) => {
      const el = form.querySelector<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(`[name="${name}"]`)!
      fireEvent.change(el, { target: { value } })
    }
    set('name', 'new')
    set('base_url', 'https://new.example.test/')
    set('repository', 'example-org/example-api')
    set('authorized_by', "the app's owner")
    set('screens', 'home /')
    // MUTATION: let an unknown key through (or send an image/command) and a POST is made.
    set('more', '{"image": "x"}')
    fireEvent.submit(form)
    await waitFor(() => expect(visible(form.parentElement)).toContain('image'), WAIT)
    expect(calls.filter((c) => c.method === 'POST')).toEqual([])
    set('more', '{"widths": ["desktop"], "rate": {"navigation_ms": 3000}, "file": {"max_issues": 5}}')
    fireEvent.submit(form)
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST').length).toBe(1), WAIT)
    const body = calls.find((c) => c.method === 'POST')!.body as Json
    // MUTATION: drop the More settings merge and these fields never reach the API.
    expect(body.widths).toEqual(['desktop'])
    expect(body.rate).toEqual({ navigation_ms: 3000 })
    expect(body.file).toEqual({ max_issues: 5 })
  })

  it('an edit sends only what changed, and clears a More setting that was removed', async () => {
    const calls = live('admin')
    const { RepositoriesScreen } = await import('../Repositories')
    render(<RepositoriesScreen view="page=qa-targets" go={vi.fn()} />)
    await waitFor(() => expect(button(card(APPROVED), 'Edit')).not.toBeNull(), WAIT)
    fireEvent.click(button(card(APPROVED), 'Edit')!)
    const form = await waitFor(() => {
      const f = document.querySelector<HTMLFormElement>('form.qa-edit')
      if (f === null) throw new Error('no edit form')
      return f
    }, WAIT)
    const more = form.querySelector<HTMLTextAreaElement>('[name="more"]')!
    // MUTATION: drop the More fields from `normQaTarget` and the box opens empty.
    expect(JSON.parse(more.value)).toEqual({ themes: ['light'] })
    fireEvent.change(form.querySelector('[name="name"]')!, { target: { value: 'shop-2' } })
    fireEvent.change(more, { target: { value: '' } })
    fireEvent.submit(form)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PATCH').length).toBe(1), WAIT)
    const sent = calls.find((c) => c.method === 'PATCH')!
    expect(sent.url).toBe(`/v1/qa-targets/${APPROVED}`)
    // MUTATION: send the whole form on an edit, or leave a removed setting out instead of null.
    expect(sent.body).toEqual({ name: 'shop-2', themes: null })
  })
})

describe('QA targets: a platform admin approves a pending host (VQ6-APPROVE, §1.2)', () => {
  function typeInto(root: ParentNode, name: string, value: string) {
    const el = root.querySelector<HTMLInputElement>(`input[name="${name}"]`)
    if (el === null) throw new Error(`no ${name} field`)
    fireEvent.change(el, { target: { value } })
  }

  function approveForm(root: ParentNode): HTMLFormElement {
    const f = root.querySelector<HTMLFormElement>('form.qa-approve')
    if (f === null) throw new Error('no approve form')
    return f
  }

  it('a platform admin sees Approve host on a pending target and not on an approved one', async () => {
    // A workspace MEMBER who is a platform admin: the platform role decides, not the workspace one.
    live('member', { platformAdmin: true })
    await mountPage()
    // MUTATION: gate Approve host on the workspace admin role (or draw it for nobody) and this never appears.
    await waitFor(() => expect(button(card(PENDING), 'Approve host')).not.toBeNull(), WAIT)
    // MUTATION: draw Approve host whatever the host's state and the approved target shows it too.
    expect(button(card(APPROVED), 'Approve host')).toBeNull()
  })

  it('a member who is not a platform admin never sees it, even a workspace admin', async () => {
    const calls = live('admin', { platformAdmin: false })
    await mountPage()
    // The workspace-admin controls have drawn, so the session read has landed.
    await waitFor(() => expect(button(card(APPROVED), 'Edit')).not.toBeNull(), WAIT)
    await waitFor(() => expect(calls.some((c) => c.url.split('?')[0] === '/v1/tenants/me')).toBe(true), WAIT)
    // MUTATION: draw Approve host for any workspace admin (`admin` instead of `principal.is_admin`) and this is not null.
    expect(button(card(PENDING), 'Approve host')).toBeNull()
    expect(document.querySelector('form.qa-approve')).toBeNull()
  })

  it('posts :approve-host only once the typed host is the target’s host, then shows it approved', async () => {
    const calls = live('member', { platformAdmin: true })
    await mountPage()
    await waitFor(() => expect(button(card(PENDING), 'Approve host')).not.toBeNull(), WAIT)
    fireEvent.click(button(card(PENDING), 'Approve host')!)
    const form = approveForm(card(PENDING))
    typeInto(form, 'confirm_host', 'blog.example.test.evil')
    fireEvent.submit(form)
    // MUTATION: drop the typed-host check and a wrong host is posted.
    expect(calls.filter((c) => c.method === 'POST')).toEqual([])
    typeInto(form, 'confirm_host', ' Blog.Example.Test ')
    fireEvent.submit(form)
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST').length).toBe(1), WAIT)
    const sent = calls.find((c) => c.method === 'POST')!
    // MUTATION: post to another target's id, or to another route.
    expect(sent.url).toBe(`/v1/qa-targets/${PENDING}:approve-host`)
    // MUTATION: send anything besides the confirmed host (an extra origin the target never declared, the spec).
    expect(sent.body).toEqual({ host: 'blog.example.test' })
    // MUTATION: ignore the API's answer and the row still says pending.
    await waitFor(() => expect(card(PENDING).getAttribute('data-host-state')).toBe('approved'), WAIT)
    expect(visible(card(PENDING))).toContain('approved by root@example.com')
    expect(button(card(PENDING), 'Approve host')).toBeNull()
  })

  it('a login’s extra origin must be typed too, and is sent with the host', async () => {
    const origin = 'https://login.example.test'
    const calls = live('member', {
      platformAdmin: true,
      pending: { sign_in: { kind: 'own_login', login_path: '/login', signed_in_selector: '#me', extra_origin: origin } },
    })
    await mountPage()
    await waitFor(() => expect(button(card(PENDING), 'Approve host')).not.toBeNull(), WAIT)
    fireEvent.click(button(card(PENDING), 'Approve host')!)
    const form = approveForm(card(PENDING))
    typeInto(form, 'confirm_host', 'blog.example.test')
    fireEvent.submit(form)
    // MUTATION: drop the extra-origin check and the host is approved without the origin it signs in through.
    expect(calls.filter((c) => c.method === 'POST')).toEqual([])
    typeInto(form, 'confirm_extra_origin', origin)
    fireEvent.submit(form)
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST').length).toBe(1), WAIT)
    // MUTATION: leave `extra_origin` out of the body and the API approves the host alone.
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ host: 'blog.example.test', extra_origin: origin })
  })

  it.each([
    [403, 'forbidden', 'platform admin group membership is needed to approve a host'],
    [409, 'conflict', "the host named is not this QA target's host now; read it again"],
    [422, 'invalid', 'host: String should have at most 253 characters'],
  ])('a %i refusal is said in the API’s own words', async (status, code, message) => {
    live('member', { platformAdmin: true, approve: { status, body: { code, message } } })
    await mountPage()
    await waitFor(() => expect(button(card(PENDING), 'Approve host')).not.toBeNull(), WAIT)
    fireEvent.click(button(card(PENDING), 'Approve host')!)
    const form = approveForm(card(PENDING))
    typeInto(form, 'confirm_host', 'blog.example.test')
    fireEvent.submit(form)
    // MUTATION: swallow the refusal (or say a word of our own instead of the API's) and this never appears.
    await waitFor(() => expect(visible(card(PENDING))).toContain(message), WAIT)
    // MUTATION: mark the row approved on a refusal and the state flips.
    expect(card(PENDING).getAttribute('data-host-state')).toBe('pending')
  })
})
