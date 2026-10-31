// ADMIN › SETUP'S ONE-TIME LINK (DAYONE-SETUP-UI-5B; part of #2688; Day One
// audit finding 13): `/admin/setup/link/<token>`, the page swarm-api's
// `setup_links.LINK_FORM` names.
//
// WHAT EACH CASE HOLDS:
//   * the route renders for someone who administers nothing (outside the
//     admin gate), and the token leaves the bar;
//   * one click redeems the token in a BODY (`POST /v1/setup/links/redeem`),
//     then runs the account sign-in (`/v1/accounts/authorize`, `/exchange`)
//     with the redeemed workspace as `X-Swarm-Tenant`;
//   * a spent, expired or unknown token (404) and someone else's (403) each
//     say why in one line and what to ask the admin for;
//   * a team-git link, refused without its value, asks for it and sends it;
//   * nothing is redeemed by merely opening the page.
//
// MUTATIONS, each of which turns a case red: drop the route from App
// (Admin › Setup or the not-found page draws instead); redeem on mount; put
// the token in a URL; skip `chooseTenant` before authorize; drop a refusal's
// wording.
//
// Stubbed fetch only; no network, no credentials. Tokens are built at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { visible } from './repofixture'
import { SETUP_LINK_PATH, setupLinkToken } from '../paths'

type Json = Record<string, unknown>
type Call = { method: string; url: string; body: Json | null; tenant: string | null }
type Answer = { status: number; body: unknown } | null

const rand = (n: number) => Array.from(crypto.getRandomValues(new Uint8Array(n)), (b) => (b % 36).toString(36)).join('')

function me(): Json {
  return {
    tenant: { tenant_id: 'ana', kind: 'user', display_name: 'ana.b@example.com' },
    principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: false },
    environment: 'dev',
    environment_declared: true,
  }
}

function stub(handler: (m: string, url: string, body: Json | null) => Answer): Call[] {
  const calls: Call[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    const body = init?.body ? (JSON.parse(String(init.body)) as Json) : null
    const headers = new Headers(init?.headers)
    calls.push({ method, url, body, tenant: headers.get('X-Swarm-Tenant') })
    let r = handler(method, url, body)
    if (r === null && url.includes('/v1/tenants/mine')) r = { status: 200, body: [] }
    if (r === null && url.includes('/v1/tenants/me')) r = { status: 200, body: me() }
    r ??= { status: 404, body: { detail: 'Not Found' } }
    return new Response(JSON.stringify(r.body), { status: r.status, headers: { 'content-type': 'application/json' } })
  }) as unknown as typeof fetch
  return calls
}

async function mount(path: string) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const mod = await import('../App')
  window.history.replaceState(null, '', path)
  render(<mod.App />)
}

// TEXT AND SELECTOR QUERIES, not `*ByRole`: a role query walks the whole
// app's accessibility tree, and one took 19 s here on the mounted App.
const WAIT = { timeout: 10_000 } as const
const button = async (name: string) => {
  const label = await screen.findByText(name, { selector: 'button, button *' }, WAIT)
  return label.closest('button')!
}
const alertText = async () => {
  await waitFor(() => expect(document.querySelector('[role="alert"]')).not.toBeNull(), WAIT)
  return visible(document.querySelector('[role="alert"]'))
}

const refused = (status: number, code: string, message: string): Answer => ({ status, body: { code, message } })

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  sessionStorage.clear()
  window.history.replaceState(null, '', '/')
})

describe('/admin/setup/link/<token>', () => {
  it('redeems the token in a body and signs the pool account in, in the redeemed workspace', async () => {
    const token = rand(43)
    const state = rand(24)
    const code = rand(20)
    const authorizeUrl = `https://claude.example.test/oauth/authorize?state=${state}`
    const calls = stub((m, url) => {
      if (m === 'POST' && url === '/v1/setup/links/redeem') {
        return { status: 200, body: { kind: 'pool-account', label: 'claude-1', tenant: 'first-team', sign_in: {} } }
      }
      if (m === 'POST' && url === '/v1/accounts/authorize') return { status: 200, body: { authorize_url: authorizeUrl, state, expires_in_seconds: 600 } }
      if (m === 'POST' && url === '/v1/accounts/exchange') {
        return { status: 201, body: { account: { account_id: 'acct-1', label: 'claude-1', owner_tenant: 'first-team' }, expires_at: null } }
      }
      return null
    })
    await mount(`/admin/setup/link/${token}`)

    // MUTATION: drop the route -> Admin › Setup's gate or the not-found page draws, and this fails.
    const open = await button('Open the link')
    expect(visible(document.body)).not.toMatch(/Setup is for platform admins/)
    // The token leaves the bar; nothing was redeemed by opening the page.
    await waitFor(() => expect(window.location.pathname).toBe(SETUP_LINK_PATH), WAIT)
    expect(calls.filter((c) => c.url.includes('/links/redeem'))).toHaveLength(0)

    fireEvent.click(open)
    const link = (await screen.findByText('Open the Claude sign-in page', {}, WAIT)).closest('a')!
    expect(link.getAttribute('href')).toBe(authorizeUrl)

    const redeems = calls.filter((c) => c.url === '/v1/setup/links/redeem')
    expect(redeems).toHaveLength(1)
    expect(redeems[0]!.body).toEqual({ token })
    // MUTATION: put the token in a URL -> fails.
    expect(calls.some((c) => c.url.includes(token))).toBe(false)
    const authorize = calls.filter((c) => c.url === '/v1/accounts/authorize')
    expect(authorize).toHaveLength(1)
    expect(authorize[0]!.body).toEqual({ label: 'claude-1', lend_to: [] })
    // MUTATION: skip chooseTenant -> the account lands in the default workspace, and this fails.
    expect(authorize[0]!.tenant).toBe('first-team')

    fireEvent.change(screen.getByLabelText('The code Claude shows'), { target: { value: code } })
    fireEvent.click(await button('Finish the sign-in'))
    await waitFor(() => expect(visible(document.body)).toMatch(/claude-1 is in the first-team workspace's Claude pool/), WAIT)
    const exchange = calls.filter((c) => c.url === '/v1/accounts/exchange')
    expect(exchange).toHaveLength(1)
    expect(exchange[0]!.body).toEqual({ state, code })
    expect(exchange[0]!.tenant).toBe('first-team')
  })

  it('a spent, expired or unknown token says why in one line and what to ask the admin for', async () => {
    stub((m, url) =>
      m === 'POST' && url === '/v1/setup/links/redeem' ? refused(404, 'not_found', 'this link is not valid: ask the admin for a new one') : null,
    )
    await mount(`/admin/setup/link/${rand(43)}`)
    fireEvent.click(await button('Open the link'))
    expect(await alertText()).toBe(
      'This link has already been used, has expired (a link lasts 24 hours) or was never issued: ask the admin who sent it for a new link from Admin › Setup.',
    )
    // No sign-in is offered on a dead link.
    expect(screen.queryByText('Open the link')).toBeNull()
  })

  it("someone else's link says so, and that it is still unused", async () => {
    stub((m, url) => (m === 'POST' && url === '/v1/setup/links/redeem' ? refused(403, 'forbidden', 'this link was made for someone else') : null))
    await mount(`/admin/setup/link/${rand(43)}`)
    fireEvent.click(await button('Open the link'))
    expect(await alertText()).toMatch(/^This link was made for someone else and is still unused: .*ask the admin for a link in your own name\.$/)
  })

  it('a team-git link asks for its value and sends it in the body', async () => {
    const token = rand(43)
    const value = rand(30)
    const calls = stub((m, url, body) => {
      if (m !== 'POST' || url !== '/v1/setup/links/redeem') return null
      if (body?.value === undefined) {
        return refused(422, 'validation_failed', 'a team-git link is opened with its value; the link is still unused, open it again with the value')
      }
      return { status: 200, body: { kind: 'team-git', label: 'eng', secret: 'swarm-tenant-eng-git', written: true } }
    })
    await mount(`/admin/setup/link/${token}`)
    fireEvent.click(await button('Open the link'))
    fireEvent.change(await screen.findByLabelText("The team's GitHub token", {}, WAIT), { target: { value } })
    fireEvent.click(await button('Store it'))
    await waitFor(() => expect(visible(document.body)).toMatch(/Stored in swarm-tenant-eng-git\./), WAIT)
    const redeems = calls.filter((c) => c.url === '/v1/setup/links/redeem')
    expect(redeems.map((c) => c.body)).toEqual([{ token }, { token, value }])
    expect(visible(document.body)).not.toContain(value)
  })

  it('reads the token only off its own path', () => {
    const token = rand(43)
    expect(setupLinkToken(`/admin/setup/link/${token}`)).toBe(token)
    expect(setupLinkToken(`/admin/setup/link/${token}/`)).toBe(token)
    expect(setupLinkToken('/admin/setup/link')).toBeNull()
    expect(setupLinkToken('/admin/setup')).toBeNull()
    expect(setupLinkToken('/admin/setup/link/a%20b')).toBe('')
  })
})

// DAYONE-SETUP-UI-8 (part of #2688; audit 4d, F5 and F6).
//
// THE FAKE REFUSES WHAT THE REAL SERVICE REFUSES: `liveLinks` keeps the link
// unused and answers 409 while its workspace is not `ready`, spends it on the
// first 200 (404 after), and `/v1/accounts/authorize` answers swarm-api's 403
// ("not a verified member") until the workspace's membership is readable. A
// page that spent the link early, or gave up on the first 403, fails here.
//
// WHAT EACH CASE HOLDS:
//   * a 409 leaves the link unused, says the workspace is still being set up,
//     and the page opens it again by itself once it is ready;
//   * a 403 just after is started again by itself;
//   * the tab Admin › Setup opened for the installer's own link (named
//     OWN_SIGN_IN_WINDOW) opens it with no click, once; any other tab waits.

function liveLinks(token: string) {
  const world = { ready: false, member: false, spent: false }
  const calls = stub((m, url, body) => {
    if (m === 'POST' && url === '/v1/setup/links/redeem') {
      if (body?.token !== token || world.spent) return refused(404, 'not_found', 'this link is not valid: ask the admin for a new one')
      if (!world.ready) return refused(409, 'conflict', 'the workspace u-ana is not ready yet; the link is still unused, open it again shortly')
      world.spent = true
      return { status: 200, body: { kind: 'pool-account', label: 'ana', tenant: 'u-ana' } }
    }
    if (m === 'POST' && url === '/v1/accounts/authorize') {
      if (!world.member) return refused(403, 'forbidden', 'X-Swarm-Tenant names a tenant you are not a verified member of')
      return { status: 200, body: { authorize_url: 'https://claude.example.test/oauth/authorize', state: rand(24), expires_in_seconds: 600 } }
    }
    return null
  })
  return { world, calls }
}

/** The page's 10 s retries, run at 40 ms so a case waits for them in real time. */
function quickRetries() {
  const real = globalThis.setTimeout
  return vi.spyOn(globalThis, 'setTimeout').mockImplementation(((fn: () => void, ms?: number, ...rest: unknown[]) =>
    real(fn, ms === 10_000 ? 40 : ms, ...rest)) as unknown as typeof setTimeout)
}

describe('a link whose workspace is not ready (audit 4d, F6)', () => {
  it('keeps the link unused on 409, says why, and opens it by itself once the workspace is ready', async () => {
    const token = rand(43)
    const { world, calls } = liveLinks(token)
    const timers = quickRetries()
    await mount(`/admin/setup/link/${token}`)
    fireEvent.click(await button('Open the link'))
    // MUTATION: read the 409 as a dead link -> the person is sent back to the admin for a new one.
    await screen.findByText('Your workspace is still being set up.', {}, WAIT)
    expect(visible(document.body)).not.toMatch(/already been used/)
    world.ready = true
    // MUTATION: wait for another click -> the page sits on the 409.
    await waitFor(() => expect(calls.filter((c) => c.url === '/v1/setup/links/redeem' && c.method === 'POST').length).toBeGreaterThanOrEqual(2), WAIT)
    await waitFor(() => expect(calls.some((c) => c.url === '/v1/accounts/authorize')).toBe(true), WAIT)
    world.member = true
    // MUTATION: give up on the first 403 -> "Start the sign-in again" is a click the person must find.
    const link = (await screen.findByText('Open the Claude sign-in page', {}, WAIT)).closest('a')!
    expect(link.getAttribute('href')).toBe('https://claude.example.test/oauth/authorize')
    // The link was spent exactly once.
    expect(world.spent).toBe(true)
    expect(visible(document.body)).not.toMatch(/already been used/)
    timers.mockRestore()
  })
})

describe('the installer’s own link, opened by Admin › Setup (audit 4d, F5)', () => {
  afterEach(() => {
    window.name = ''
  })

  it('opens the link with no click in the tab Admin › Setup named, once', async () => {
    const token = rand(43)
    const { OWN_SIGN_IN_WINDOW } = await import('../SetupLink')
    const { world, calls } = liveLinks(token)
    world.ready = true
    world.member = true
    window.name = OWN_SIGN_IN_WINDOW
    await mount(`/admin/setup/link/${token}`)
    // MUTATION: wait for "Open the link" in the installer's own tab -> a click that is no approval, credential or choice.
    await screen.findByText('Open the Claude sign-in page', {}, WAIT)
    const redeems = calls.filter((c) => c.url === '/v1/setup/links/redeem')
    expect(redeems).toHaveLength(1)
    expect(redeems[0]!.body).toEqual({ token })
    // MUTATION: keep the name -> a reload of this tab would open a link again by itself.
    expect(window.name).toBe('')
  })

  it('any other tab still waits for the click', async () => {
    const token = rand(43)
    const { world, calls } = liveLinks(token)
    world.ready = true
    window.name = 'some-other-tab'
    await mount(`/admin/setup/link/${token}`)
    await button('Open the link')
    await new Promise((r) => setTimeout(r, 50))
    // MUTATION: open on mount for every tab -> a preview or a prefetch spends the link.
    expect(calls.filter((c) => c.url === '/v1/setup/links/redeem')).toHaveLength(0)
  })
})
