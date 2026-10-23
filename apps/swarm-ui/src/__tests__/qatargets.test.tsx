// WORK › REPOSITORIES › QA TARGETS (lane VQ-6 CONSOLE-PLUGIN, part of #1933;
// docs/design/visual-qa-any-app.md §1, §2.1, §6).
//
// WHAT EACH CASE HOLDS:
//   * the page is mounted under Repositories (`page=qa-targets`) and the list
//     links to it;
//   * a workspace admin sees Register, Edit and Set credential; a member sees
//     Run and not Edit;
//   * a pending host shows why it cannot run, and Run sends nothing;
//   * Run POSTs `{file}` alone to the target's own `:run`, and the flow
//     browser chosen for that one run beside it (lane VQ9-TOOL);
//   * the form shows "Flow browser", Playwright by default or Jev, sends it,
//     and an edit never drops it (lane VQ9-TOOL; BGD, 2026-10-23);
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
//     (lane VQ6-APPROVE, §1.2, VQ-Q1);
//   * the register/edit, credential and approve forms are laid out with the
//     repository form's classes, a label cell beside each control, each
//     start click a row of its own, and a card names its start clicks
//     (lane QA-TARGETS-FORM; CLD at 1512x806, 2026-10-23).
//
// The bodies are routes/qatargets.py's and routes/repositories.py's shapes.
// Every token-shaped value is built at runtime; nothing here is a credential.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { repo, serve, visible } from './repofixture'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

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

function targets(
  pendingOver: Json = {},
  loginSignIn: Json = { kind: 'own_login', login_path: '/login', signed_in_selector: '#me' },
  shopOver: Json = {},
): Json {
  return {
    qa_targets: [
      target(APPROVED, 'shop', { themes: ['light'], ...shopOver }),
      target(PENDING, 'blog', { host_state: 'pending', ...pendingOver }),
      target(LOGIN, 'portal', { sign_in: loginSignIn }),
    ],
  }
}

function live(
  role: string,
  opts: { run?: Reply; link?: Reply; put?: Reply; create?: Reply; platformAdmin?: boolean; approve?: Reply; pending?: Json; login?: Json; shop?: Json } = {},
) {
  const calls = serve((m, url) => {
    const path = url.split('?')[0] ?? ''
    if (m === 'GET' && path === '/v1/tenants/me') return { status: 200, body: me(role, opts.platformAdmin === true) }
    if (m === 'GET' && path === '/v1/qa-targets') return { status: 200, body: targets(opts.pending, opts.login, opts.shop) }
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

  it('Run sends the flow browser chosen for that one run, and nothing when none is chosen', async () => {
    const calls = live('member')
    await mountPage()
    const choice = card(APPROVED).querySelector<HTMLSelectElement>('select[data-run-browser]')
    // MUTATION: draw no per-run choice beside Run and this is null.
    expect(choice).not.toBeNull()
    expect(choice!.value).toBe('')
    expect(Array.from(choice!.options).map((o) => o.value)).toEqual(['', 'playwright', 'jev'])
    fireEvent.change(choice!, { target: { value: 'jev' } })
    fireEvent.click(button(card(APPROVED), 'Run')!)
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST').length).toBe(1), WAIT)
    // MUTATION: drop `browser_tool` from `runQaTarget`'s body and the run uses the target's tool.
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ file: 'issues', browser_tool: 'jev' })
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

  // THE ROUTE (lane VQ6-ROUTE, part of #1933; decided 2026-10-22, VQ-6's
  // question 1): the plugin's link is `/qa-targets/<id>/credential?t=<token>`
  // (VQ-2's `mint_link`), and paths.ts resolves it to the QA targets page with
  // the token dropped, the way it drops the key link's.
  it('the console routes the plugin\u2019s credential link to the QA targets page, without its token', async () => {
    const { pathToAddress, addressToPath } = await import('../paths')
    const token = runtimeValue()
    const route = pathToAddress(`/qa-targets/${LOGIN}/credential`, `?t=${token}`)
    // MUTATION: drop the `/qa-targets/<id>/credential` case from paths.ts `pathToAddress` and this is null.
    expect(route).not.toBeNull()
    expect(route!.address).toBe('work/repositories?page=qa-targets')
    // MUTATION: keep the link's query in the address (`?page=qa-targets&t=..`) and the token survives into it.
    expect(route!.address).not.toContain(token)
    expect(addressToPath(route!.address, 'live')).toBe('/repositories?page=qa-targets')
    // A trailing slash is the same link; anything else under `/qa-targets` is not served.
    expect(pathToAddress(`/qa-targets/${LOGIN}/credential/`, `?t=${token}`)?.address).toBe('work/repositories?page=qa-targets')
    // MUTATION: route any `/qa-targets/...` path and these are not null.
    expect(pathToAddress(`/qa-targets/${LOGIN}`, `?t=${token}`)).toBeNull()
    expect(pathToAddress(`/qa-targets/${LOGIN}/credential/more`, `?t=${token}`)).toBeNull()
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
      // MUTATION: leave the Flow browser out of `bodyOf` and a registration never says it.
      flows_browser_tool: 'playwright',
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

describe('QA targets: the flow browser (VQ9-TOOL; BGD, 2026-10-23)', () => {
  async function openForm(label: string, root: ParentNode = document): Promise<HTMLFormElement> {
    await waitFor(() => expect(button(root, label)).not.toBeNull(), WAIT)
    fireEvent.click(button(root, label)!)
    return waitFor(() => {
      const f = document.querySelector<HTMLFormElement>('form.qa-edit')
      if (f === null) throw new Error('no form')
      return f
    }, WAIT)
  }

  it('the form shows Flow browser, Playwright by default, and a registration choosing Jev sends it', async () => {
    const calls = live('admin')
    await mountPage()
    const form = await openForm('Register QA target')
    await waitFor(() => expect(form.querySelectorAll('select[name="repository"] option').length).toBeGreaterThan(1), WAIT)
    const tool = form.querySelector<HTMLSelectElement>('select[name="flows_browser_tool"]')
    // MUTATION: draw no Flow browser select and this is null.
    expect(tool).not.toBeNull()
    expect(visible(tool!.closest('label')!)).toContain('Flow browser')
    // MUTATION: default the select to Jev and this fails.
    expect(tool!.value).toBe('playwright')
    expect(Array.from(tool!.options).map((o) => o.value)).toEqual(['playwright', 'jev'])
    const set = (name: string, value: string) => {
      fireEvent.change(form.querySelector(`[name="${name}"]`)!, { target: { value } })
    }
    set('name', 'new')
    set('base_url', 'https://new.example.test/')
    set('repository', 'example-org/example-api')
    set('authorized_by', "the app's owner")
    set('screens', 'home /')
    set('flows_browser_tool', 'jev')
    fireEvent.submit(form)
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST').length).toBe(1), WAIT)
    // MUTATION: send the draft's default instead of the choice and this is playwright.
    expect((calls.find((c) => c.method === 'POST')!.body as Json).flows_browser_tool).toBe('jev')
  })

  it('an edit opens on the target’s tool and a save of anything else never drops it', async () => {
    const calls = live('admin', { shop: { flows_browser_tool: 'jev' } })
    await mountPage()
    const form = await openForm('Edit', card(APPROVED))
    // MUTATION: leave `flows_browser_tool` out of `normQaTarget` and this opens on playwright.
    expect(form.querySelector<HTMLSelectElement>('select[name="flows_browser_tool"]')!.value).toBe('jev')
    fireEvent.change(form.querySelector('[name="name"]')!, { target: { value: 'shop-2' } })
    fireEvent.submit(form)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PATCH').length).toBe(1), WAIT)
    // MUTATION: draft every target as playwright and the save sends `flows_browser_tool: "playwright"`.
    expect(calls.find((c) => c.method === 'PATCH')!.body).toEqual({ name: 'shop-2' })
  })

  it('an edit that changes the tool sends it', async () => {
    const calls = live('admin')
    await mountPage()
    const form = await openForm('Edit', card(APPROVED))
    expect(form.querySelector<HTMLSelectElement>('select[name="flows_browser_tool"]')!.value).toBe('playwright')
    fireEvent.change(form.querySelector('[name="flows_browser_tool"]')!, { target: { value: 'jev' } })
    fireEvent.submit(form)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PATCH').length).toBe(1), WAIT)
    // MUTATION: leave the field out of the edit's diff and the change is never sent.
    expect(calls.find((c) => c.method === 'PATCH')!.body).toEqual({ flows_browser_tool: 'jev' })
  })
})

describe('QA targets: sign-in start clicks (VQ8-START-CLICKS, §2.2)', () => {
  // AI Studio's chooser: a Staging | Production toggle, then "Sign in" before the IdP.
  const CHOOSER = [{ text: 'Staging', css: 'button' }, { text: 'Sign in' }]
  const SSO = 'https://sso.portal.example.test'
  const login = { kind: 'own_login', login_path: '/', signed_in_selector: '#me', extra_origin: SSO, start_clicks: CHOOSER }

  async function editLogin(calls: ReturnType<typeof live>) {
    void calls
    const { RepositoriesScreen } = await import('../Repositories')
    render(<RepositoriesScreen view="page=qa-targets" go={vi.fn()} />)
    await waitFor(() => expect(button(card(LOGIN), 'Edit')).not.toBeNull(), WAIT)
    fireEvent.click(button(card(LOGIN), 'Edit')!)
    return waitFor(() => {
      const f = document.querySelector<HTMLFormElement>('form.qa-edit')
      if (f === null) throw new Error('no edit form')
      return f
    }, WAIT)
  }

  it('the edit form draws each start click, and a sign-in change keeps them and the extra origin', async () => {
    const calls = live('admin', { login })
    const form = await editLogin(calls)
    const value = (name: string) => form.querySelector<HTMLInputElement>(`[name="${name}"]`)?.value
    // MUTATION: drop `start_clicks` from `normSignIn` (or the draft) and the rows open empty.
    expect([value('start_click_text_0'), value('start_click_css_0'), value('start_click_text_1'), value('start_click_css_1')]).toEqual([
      'Staging',
      'button',
      'Sign in',
      '',
    ])
    fireEvent.change(form.querySelector('[name="signed_in_selector"]')!, { target: { value: '#account' } })
    fireEvent.submit(form)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PATCH').length).toBe(1), WAIT)
    const sent = calls.find((c) => c.method === 'PATCH')!.body as Json
    // MUTATION: rebuild sign_in from the drawn fields alone and the clicks and the extra origin are dropped.
    expect(sent).toEqual({ sign_in: { ...login, signed_in_selector: '#account' } })
  })

  it('an admin adds up to three start clicks and removes one; a save sends them in order', async () => {
    const calls = live('admin', { login })
    const form = await editLogin(calls)
    // MUTATION: offer a fourth row and the API's 1-to-3 bound is met only by a refusal.
    fireEvent.click(button(form, 'Add start click')!)
    expect(form.querySelector('[name="start_click_text_2"]')).not.toBeNull()
    expect(button(form, 'Add start click')).toBeNull()
    fireEvent.change(form.querySelector('[name="start_click_text_2"]')!, { target: { value: ' Continue ' } })
    fireEvent.click(form.querySelectorAll<HTMLButtonElement>('button[name="remove_start_click"]')[0]!)
    fireEvent.submit(form)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PATCH').length).toBe(1), WAIT)
    const sent = calls.find((c) => c.method === 'PATCH')!.body as Json
    // MUTATION: send an empty css, keep the removed row, or leave the text untrimmed and this fails.
    expect((sent.sign_in as Json).start_clicks).toEqual([{ text: 'Sign in' }, { text: 'Continue' }])
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

describe('QA targets: the credential link, opened in the console (VQ6-ROUTE)', () => {
  /** The console, opened at the plugin's link, live, as a workspace admin. */
  async function openLink(token: string) {
    window.history.replaceState(null, '', `/qa-targets/${LOGIN}/credential?t=${token}`)
    const calls = live('admin')
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    const logs = (['log', 'info', 'warn', 'error', 'debug'] as const).map((k) => vi.spyOn(console, k))
    const { App } = await import('../App')
    render(<App />)
    // Read straight after the first render's effects, before any read lands.
    const firstBar = window.location.href
    return { calls, setItem, logs, firstBar }
  }

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('lands on the QA targets page with the credential form open for the link\u2019s target', async () => {
    const token = runtimeValue()
    const { calls } = await openLink(token)
    // MUTATION: drop the credential path from paths.ts and the console draws Not found; no form.
    // MUTATION: drop App's capture (or pass `qaLink` as null) and the page mounts after the bar was rewritten; no form.
    const form = await waitFor(() => {
      const f = document.querySelector<HTMLFormElement>('form.qa-cred')
      if (f === null) throw new Error('no credential form')
      return f
    }, WAIT)
    expect(form.getAttribute('data-target-id')).toBe(LOGIN)
    expect(window.location.pathname).toBe('/repositories')
    expect(new URLSearchParams(window.location.search).get('page')).toBe('qa-targets')
    form.querySelector<HTMLInputElement>('input[name="username"]')!.value = 'qa-bot@example.com'
    form.querySelector<HTMLInputElement>('input[name="password"]')!.value = runtimeValue(18)
    fireEvent.submit(form)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PUT').length).toBe(1), WAIT)
    // MUTATION: hand the page the link without its token (`token: null`) and the form refuses before the PUT.
    const put = calls.find((c) => c.method === 'PUT')!
    expect(put.url.split('?')[0]).toBe(`/v1/qa-targets/${LOGIN}/credential`)
    expect((put.body as Json).link_token).toBe(token)
  })

  it('App hands the page the link it read off the bar, once', async () => {
    // The page reads the bar itself when no link is handed to it, and here it
    // mounts in App's first render, so the cases above pass on that read
    // alone. This one stands in for RepositoriesScreen and reads what App hands it.
    const token = runtimeValue()
    window.history.replaceState(null, '', `/qa-targets/${LOGIN}/credential?t=${token}`)
    live('admin')
    const handed: unknown[] = []
    vi.doMock('../Repositories', () => ({
      RepositoriesScreen: ({ view, go, qaLink }: { view: string | null; go: (to: string) => void; qaLink?: unknown }) => {
        handed.push({ view, qaLink })
        return (
          <div>
            <button type="button" onClick={() => go('work/repositories')}>away</button>
            <button type="button" onClick={() => go('work/repositories?page=qa-targets')}>back</button>
          </div>
        )
      },
    }))
    try {
      const { App } = await import('../App')
      render(<App />)
      // MUTATION: drop App's capture (pass no `qaLink` to RepositoriesScreen) and the first mount is handed undefined.
      expect(handed[0]).toEqual({ view: 'page=qa-targets', qaLink: { targetId: LOGIN, token } })
      fireEvent.click(button(document, 'away')!)
      await waitFor(() => expect(handed.some((h) => (h as { view: string | null }).view === null)).toBe(true), WAIT)
      const before = handed.length
      fireEvent.click(button(document, 'back')!)
      await waitFor(() => expect(handed.length).toBeGreaterThan(before), WAIT)
      // MUTATION: never clear App's ref once handed over and the page, opened again, is handed the used token.
      expect(handed.slice(before)).toEqual(handed.slice(before).map(() => ({ view: 'page=qa-targets', qaLink: null })))
    } finally {
      vi.doUnmock('../Repositories')
    }
  })

  it('the bar no longer carries `t` after the first render', async () => {
    const token = runtimeValue()
    const { firstBar } = await openLink(token)
    // MUTATION: keep the query in paths.ts's route (`${QA_TARGETS_PAGE}&${query}`) and normalise writes `t` back into the bar.
    expect(firstBar).not.toContain(token)
    expect(new URL(firstBar).searchParams.has('t')).toBe(false)
    await waitFor(() => expect(document.querySelector('form.qa-cred')).not.toBeNull(), WAIT)
    expect(window.location.href).not.toContain(token)
    // Never in another URL either: no link, no request's address, no history state.
    expect(document.documentElement.outerHTML).not.toContain(token)
    expect(JSON.stringify(window.history.state ?? null)).not.toContain(token)
  })

  it('nothing writes the token to storage or to the console', async () => {
    const token = runtimeValue()
    const { calls, setItem, logs } = await openLink(token)
    await waitFor(() => expect(document.querySelector('form.qa-cred')).not.toBeNull(), WAIT)
    // MUTATION: keep the captured link in sessionStorage (or localStorage) for a later mount and these see it.
    expect(setItem.mock.calls.filter((c) => String(c[1]).includes(token))).toEqual([])
    const stored = [window.localStorage, window.sessionStorage].flatMap((s) => Object.keys(s).map((k) => `${k}=${s.getItem(k)}`))
    expect(stored.filter((kv) => kv.includes(token))).toEqual([])
    // MUTATION: log the captured link in App and this sees it.
    for (const spy of logs) expect(spy.mock.calls.filter((c) => c.some((a) => JSON.stringify(a ?? null)?.includes(token)))).toEqual([])
    // MUTATION: put the token in a read's address and this sees it.
    expect(calls.filter((c) => c.url.includes(token))).toEqual([])
  })
})

describe('QA targets: the forms are laid out as the repository form is (QA-TARGETS-FORM; CLD, 2026-10-23)', () => {
  const WIDE: CascadeEnv = { width: 1512 }
  const PHONE: CascadeEnv = { width: 390 }
  const CHOOSER = [{ text: 'Staging', css: 'button' }, { text: 'Sign in' }]
  const login = { kind: 'own_login', login_path: '/', signed_in_selector: '#me', start_clicks: CHOOSER }

  async function edit(id: string) {
    const { RepositoriesScreen } = await import('../Repositories')
    render(<RepositoriesScreen view="page=qa-targets" go={vi.fn()} />)
    await waitFor(() => expect(button(card(id), 'Edit')).not.toBeNull(), WAIT)
    fireEvent.click(button(card(id), 'Edit')!)
    return waitFor(() => {
      const f = document.querySelector<HTMLFormElement>('form.qa-edit')
      if (f === null) throw new Error('no edit form')
      return f
    }, WAIT)
  }

  /** The `ur-fgrid` cell a control sits in, and the label cell before it. */
  function cellOf(form: HTMLElement, name: string): { grid: Element; label: Element | null; cell: Element } {
    const control = form.querySelector(`[name="${name}"]`)
    if (control === null) throw new Error(`no control named ${name}`)
    let cell: Element = control
    while (cell.parentElement !== null && !cell.parentElement.classList.contains('ur-fgrid')) cell = cell.parentElement
    if (cell.parentElement === null) throw new Error(`${name} is in no ur-fgrid`)
    return { grid: cell.parentElement, label: cell.previousElementSibling, cell }
  }

  function expectLaidOut(form: HTMLElement, fields: [string, string][]) {
    for (const [name, words] of fields) {
      const { grid, label, cell } = cellOf(form, name)
      // MUTATION: wrap a control in its own <label> again (no ur-fl cell before it) and the label flows inline with it: red.
      expect(label?.classList.contains('ur-fl'), name).toBe(true)
      expect(visible(label!).startsWith(words), name).toBe(true)
      // MUTATION: drop htmlFor/id and the label cell names no control: red.
      const control = form.querySelector<HTMLInputElement>(`[name="${name}"]`)!
      expect(Array.from(control.labels ?? []), name).toContain(label)
      expect(cell.classList.contains('ur-fl'), name).toBe(false)
      // MUTATION: drop the 200 px label column from `.ur-fgrid` and the label sits over its box at full width: red.
      expect(painted(grid, 'grid-template-columns', WIDE)).toBe('200px minmax(0, 1fr)')
      // MUTATION: drop the 719 px media rule and the 200 px column squeezes the field to 150 px on a phone: red.
      expect(painted(grid, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
    }
  }

  it('the edit form puts each label in an ur-fl cell beside its control inside an ur-fgrid, Save and Cancel in ur-formacts', async () => {
    live('admin', { login })
    const form = await edit(LOGIN)
    // MUTATION: drop `ur-form` from the form's card and it spans the page: red.
    expect(form.closest('.ur-form')).not.toBeNull()
    expectLaidOut(form, [
      ['name', 'Name'],
      ['base_url', 'Address'],
      ['repository', 'Files findings in'],
      ['authorized_by', 'Who said this application may be tested'],
      ['environment', 'Environment'],
      ['flows_browser_tool', 'Flow browser'],
      ['kind', 'Sign-in'],
      ['login_path', 'Login path'],
      ['signed_in_selector', 'Signed-in selector'],
      ['username_selector', 'Username selector'],
      ['password_selector', 'Password selector'],
      ['submit_selector', 'Submit selector'],
      ['bypass_kind', 'Challenge bypass'],
      ['screens', 'Screens'],
      ['more', 'More settings'],
    ])
    for (const name of ['screens', 'more']) {
      const box = form.querySelector<HTMLTextAreaElement>(`textarea[name="${name}"]`)!
      // MUTATION: leave rows at 4 and the box holds four lines: red.
      expect(box.rows, name).toBeGreaterThanOrEqual(6)
      // MUTATION: drop `.ur-fgrid textarea` and the box is about 100 px wide: red.
      expect(painted(box, 'width', WIDE), name).toBe('100%')
      // MUTATION: put the help after the box again and the label cell has no <small>: red.
      expect(cellOf(form, name).label!.querySelector('small'), name).not.toBeNull()
      expect(box.nextSibling, name).toBeNull()
    }
    // MUTATION: keep Save and Cancel in a `ur-cacts` span and they sit left under the last field: red.
    expect(button(form, 'Save changes')!.closest('.ur-formacts')).not.toBeNull()
    expect(button(form, 'Cancel')!.closest('.ur-formacts')).not.toBeNull()
  })

  it('the approve form is laid out the same way', async () => {
    live('admin', { login, platformAdmin: true })
    const { RepositoriesScreen } = await import('../Repositories')
    render(<RepositoriesScreen view="page=qa-targets" go={vi.fn()} />)
    await waitFor(() => expect(button(card(PENDING), 'Approve host')).not.toBeNull(), WAIT)
    fireEvent.click(button(card(PENDING), 'Approve host')!)
    const approve = await waitFor(() => {
      const f = document.querySelector<HTMLFormElement>('form.qa-approve')
      if (f === null) throw new Error('no approve form')
      return f
    }, WAIT)
    // MUTATION: leave the approve form's label wrapping its input and it flows inline: red.
    expectLaidOut(approve, [['confirm_host', 'Type the host to confirm']])
    expect(button(approve, 'Approve blog.example.test')!.closest('.ur-formacts')).not.toBeNull()
  })

  it('the credential form puts Username and Password in an ur-fgrid', async () => {
    const token = runtimeValue()
    window.history.replaceState(null, '', `/qa-targets/${LOGIN}/credential?t=${token}`)
    live('admin', { login })
    const { QaTargetsScreen } = await import('../QaTargets')
    render(<QaTargetsScreen go={vi.fn()} />)
    const form = await waitFor(() => {
      const f = document.querySelector<HTMLFormElement>('form.qa-cred')
      if (f === null) throw new Error('no credential form')
      return f
    }, WAIT)
    // MUTATION: leave the credential form's labels wrapping their inputs and the two flow as one line: red.
    expectLaidOut(form, [
      ['username', 'Username'],
      ['password', 'Password'],
    ])
    expect(form.closest('.ur-form')).not.toBeNull()
    expect(button(form, 'Save credential')!.closest('.ur-formacts')).not.toBeNull()
  })

  it('two start clicks render as two rows, each Click N, its text, its CSS and Remove, Add start click under the last', async () => {
    live('admin', { login })
    const form = await edit(LOGIN)
    const rows = Array.from(form.querySelectorAll<HTMLElement>('.qa-start-click'))
    expect(rows).toHaveLength(2)
    rows.forEach((row, i) => {
      // MUTATION: drop the `.qa-start-click` rule and each row is an inline span, both on one line: red.
      expect(painted(row, 'display', WIDE)).toBe('flex')
      expect(visible(row).startsWith(`Click ${i + 1}`)).toBe(true)
      // MUTATION: put a row's fields anywhere but its own row and this fails.
      expect(row.querySelector(`[name="start_click_text_${i}"]`)).not.toBeNull()
      expect(row.querySelector(`[name="start_click_css_${i}"]`)).not.toBeNull()
      expect(button(row, 'Remove')).not.toBeNull()
    })
    const add = button(form, 'Add start click')!
    // MUTATION: draw Add start click before the rows (or inside one) and it is not under the last: red.
    expect(add.closest('.qa-start-click')).toBeNull()
    expect(rows[1]!.compareDocumentPosition(add) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('a card says its start clicks in order, as stored, and says nothing of them without any', async () => {
    live('admin', { login })
    await mountPage()
    // MUTATION: drop the card's start-clicks line and the list never says the gate clicks first: red.
    expect(card(LOGIN).querySelector('[data-start-clicks]')?.textContent).toBe('Start clicks: Staging \u2192 Sign in')
    // MUTATION: draw the line on every target and a target with no clicks says "Start clicks:": red.
    expect(visible(card(APPROVED))).not.toContain('Start clicks')
    expect(card(APPROVED).querySelector('[data-start-clicks]')).toBeNull()
  })

  it('saving after editing the first click\u2019s text sends both clicks in order', async () => {
    const calls = live('admin', { login })
    const form = await edit(LOGIN)
    fireEvent.change(form.querySelector('[name="start_click_text_0"]')!, { target: { value: 'Production' } })
    fireEvent.submit(form)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PATCH').length).toBe(1), WAIT)
    const sent = calls.find((c) => c.method === 'PATCH')!.body as Json
    // MUTATION: rename a row's input (or drop a row on re-layout) and the second click is lost or unchanged: red.
    expect(sent).toEqual({ sign_in: { ...login, start_clicks: [{ text: 'Production', css: 'button' }, { text: 'Sign in' }] } })
  })
})
