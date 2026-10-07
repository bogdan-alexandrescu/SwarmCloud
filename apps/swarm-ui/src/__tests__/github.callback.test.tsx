// THE GITHUB APP'S CALLBACK PAGE, /onboarding/github/callback (#780, OB3).
//
// WHAT EACH CASE HOLDS:
//   * success: the page posts `{state, code}` to the exchange and says
//     "Connected as @<login>" with the accounts and orgs `GET /v1/onboarding`
//     says the connection reaches;
//   * a refusal (GitHub's `?error=`, or the server refusing the code) shows
//     the §2.3 recovery copy the server sent, and Try again starts a fresh
//     authorisation and sends the browser to GitHub;
//   * ONE POST, under React strict mode (every effect twice) and across a
//     remount: the state is single use, so a second post would be refused and
//     would overwrite the success with "expired";
//   * the bar is scrubbed of `code` and `state` BEFORE the answer arrives, and
//     neither value is ever drawn -- not in text and not in an attribute;
//   * the router resolves the path to this page (not "No page at …"), and its
//     query never re-enters the bar.
//
// The fake code and state are built at runtime: nothing added to this
// repository may look like a credential (CLAUDE.md, "Lanes").

import { StrictMode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { serve } from './repofixture'

const CALLBACK = '/onboarding/github/callback'
const CODE = ['cb', 'code', 'x'.repeat(12)].join('-')
const STATE = ['cb', 'state', 'y'.repeat(20)].join('-')

const CONNECTION = {
  connection_id: 'github:eng:abc',
  forge: 'github',
  method: 'app_user',
  forge_login: 'octo-dev',
  forge_user_id: 42,
  token_id: 'tok_u',
  secret_name: 'swarm-tenant-eng-git-u-89abcdef01234567',
  state: 'active',
  failure: null,
  access_expires_at: new Date(Date.now() + 8 * 3600_000).toISOString(),
  refresh_expires_at: null,
  refreshed_at: null,
  refreshing: false,
  created_at: new Date().toISOString(),
  connected_at: new Date().toISOString(),
  revoked_at: null,
}

function onboardingDoc(owners: { owner: string; owner_type: 'User' | 'Organization'; reach?: string }[]) {
  return {
    tenant_id: 'eng',
    user: 'dev@swarm.example.com',
    user_hash: '89abcdef01234567',
    derived_at: new Date().toISOString(),
    next_step: 'repos_chosen',
    complete: false,
    source: 'derived on this read',
    steps: [
      { step: 'signed_in', state: 'done', code: null, copy: null, checked_at: null, evidence: {}, issues: [] },
      {
        step: 'github_connected',
        state: 'done',
        code: null,
        copy: null,
        checked_at: null,
        evidence: { via: 'user', kind: 'app_user', forge_login: 'octo-dev', token_state: 'active' },
        issues: [],
      },
      {
        step: 'orgs_enabled',
        state: owners.length === 0 ? 'in_progress' : 'done',
        code: null,
        copy: null,
        checked_at: null,
        evidence: {
          owners: owners.map((o) => ({ source: 'orgs', registered: 0, reach: 'reachable', ...o })),
          orgs_read: owners.length > 0,
          orgs_capped: false,
          sso_hidden_orgs: 0,
          read_at: null,
        },
        issues: [],
      },
    ],
  }
}

const OWNERS = [
  { owner: 'octo-dev', owner_type: 'User' as const },
  { owner: 'example-org', owner_type: 'Organization' as const },
]

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../GitHubConnect')
}

async function mount(go = vi.fn()) {
  const { GitHubCallbackScreen } = await load()
  const utils = render(
    <StrictMode>
      <GitHubCallbackScreen go={go} />
    </StrictMode>,
  )
  return { ...utils, go }
}

const at = (search: string) => window.history.replaceState({ kept: true }, '', `${CALLBACK}${search}`)

/** Every attribute and every text node on the page, so a value cannot hide in a title or a data-*. */
function everything(): string {
  const attrs = [...document.querySelectorAll('*')].flatMap((el) => [...el.attributes].map((a) => a.value))
  return `${document.body.textContent ?? ''} ${attrs.join(' ')}`
}

const exchanges = (calls: { method: string; url: string }[]) =>
  calls.filter((c) => c.method === 'POST' && c.url === '/v1/onboarding/github/exchange')

afterEach(() => vi.unstubAllEnvs())

describe('the callback finishes the connection', () => {
  it('posts state and code once and says "Connected as @<login>" with the orgs the connection reaches', async () => {
    at(`?code=${CODE}&state=${STATE}`)
    const calls = serve((m, url) => {
      if (m === 'POST' && url === '/v1/onboarding/github/exchange') return { status: 200, body: { connection: CONNECTION } }
      if (m === 'GET' && url === '/v1/onboarding') return { status: 200, body: onboardingDoc(OWNERS) }
      return null
    })
    await mount()
    expect(await screen.findByText('Connected as @octo-dev')).toBeTruthy()
    const reaches = await screen.findByRole('list', { name: 'Accounts and orgs this connection reaches' })
    expect(reaches.textContent).toContain('octo-dev')
    expect(reaches.textContent).toContain('example-org')
    expect(exchanges(calls)).toHaveLength(1)
    expect(exchanges(calls)[0]).toMatchObject({ body: { state: STATE, code: CODE } })
    expect(everything()).not.toContain(CODE)
    expect(everything()).not.toContain(STATE)
  })

  it('says the orgs are not read yet rather than drawing none, while the new token is unverified', async () => {
    at(`?code=${CODE}&state=${STATE}`)
    serve((m, url) => {
      if (m === 'POST' && url === '/v1/onboarding/github/exchange') return { status: 200, body: { connection: CONNECTION } }
      if (m === 'GET' && url === '/v1/onboarding') return { status: 200, body: onboardingDoc([]) }
      return null
    })
    await mount()
    expect(await screen.findByText('Connected as @octo-dev')).toBeTruthy()
    expect(await screen.findByText(/appear here once it has/)).toBeTruthy()
    expect(screen.queryByRole('list', { name: 'Accounts and orgs this connection reaches' })).toBeNull()
  })
})

describe('a refusal says why and offers Try again', () => {
  const RECOVERY = 'GitHub says the authorisation was cancelled, so SwarmCloud has no access. Press Connect GitHub to start again; nothing was stored.'

  it("posts GitHub's error with the state, shows the server's recovery copy, and Try again restarts authorize", async () => {
    at(`?error=access_denied&error_description=The+user+has+denied&state=${STATE}`)
    const calls = serve((m, url) => {
      if (m === 'POST' && url === '/v1/onboarding/github/exchange') {
        return {
          status: 400,
          body: {
            code: 'authorisation_refused',
            message: 'GitHub says the authorisation was cancelled',
            detail: { failure_code: 'AUTHORISATION_DENIED', recovery: RECOVERY },
          },
        }
      }
      if (m === 'POST' && url === '/v1/onboarding/github/authorize') {
        return { status: 200, body: { authorize_url: 'https://github.com/login/oauth/authorize?client_id=Iv1.example', expires_in_seconds: 600 } }
      }
      return null
    })
    const mod = await load()
    const assign = vi.spyOn(mod.browser, 'assign').mockImplementation(() => {})
    render(
      <StrictMode>
        <mod.GitHubCallbackScreen go={vi.fn()} />
      </StrictMode>,
    )
    expect(await screen.findByText(RECOVERY)).toBeTruthy()
    expect(screen.getByText('GitHub was not connected')).toBeTruthy()
    expect(screen.getByText(/AUTHORISATION_DENIED/)).toBeTruthy()
    // GitHub's error code, and nothing of its free-text description.
    expect(exchanges(calls)).toEqual([expect.objectContaining({ body: { state: STATE, error: 'access_denied' } })])
    expect(everything()).not.toContain(STATE)

    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(assign).toHaveBeenCalledTimes(1))
    expect(assign).toHaveBeenCalledWith('https://github.com/login/oauth/authorize?client_id=Iv1.example')
    expect(calls.filter((c) => c.url === '/v1/onboarding/github/authorize')).toEqual([
      expect.objectContaining({ method: 'POST', body: { surface: 'console' } }),
    ])
  })

  it('shows a refusal without §2.3 detail by its heading and message, and never opens a non-GitHub URL', async () => {
    at(`?code=${CODE}&state=${STATE}`)
    serve((m, url) => {
      if (m === 'POST' && url === '/v1/onboarding/github/exchange') {
        return { status: 503, body: { code: 'github_app_not_configured', message: 'the SwarmCloud GitHub App is not configured: SWARM_GITHUB_APP_CLIENT_ID' } }
      }
      if (m === 'POST' && url === '/v1/onboarding/github/authorize') {
        return { status: 200, body: { authorize_url: 'https://evil.example/authorize', expires_in_seconds: 600 } }
      }
      return null
    })
    const mod = await load()
    const assign = vi.spyOn(mod.browser, 'assign').mockImplementation(() => {})
    render(<mod.GitHubCallbackScreen go={vi.fn()} />)
    expect(await screen.findByText(/is not configured/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByText("GitHub's sign-in page was not opened")).toBeTruthy()
    expect(assign).not.toHaveBeenCalled()
  })
})

describe('one exchange per page load', () => {
  it('posts once under strict mode and once across a remount, and both mounts show the one answer', async () => {
    at(`?code=${CODE}&state=${STATE}`)
    const calls = serve((m, url) => {
      if (m === 'POST' && url === '/v1/onboarding/github/exchange') return { status: 200, body: { connection: CONNECTION } }
      if (m === 'GET' && url === '/v1/onboarding') return { status: 200, body: onboardingDoc(OWNERS) }
      return null
    })
    const { GitHubCallbackScreen } = await load()
    const first = render(
      <StrictMode>
        <GitHubCallbackScreen go={vi.fn()} />
      </StrictMode>,
    )
    expect(await screen.findByText('Connected as @octo-dev')).toBeTruthy()
    first.unmount()
    render(
      <StrictMode>
        <GitHubCallbackScreen go={vi.fn()} />
      </StrictMode>,
    )
    // The remount reads a bar that no longer carries the code; it still shows
    // the one answer rather than "nothing to finish" or a second post.
    expect(await screen.findByText('Connected as @octo-dev')).toBeTruthy()
    expect(exchanges(calls)).toHaveLength(1)
  })
})

describe('the address bar never keeps the code or the state', () => {
  it('is rewritten to the bare path before the exchange answers, keeping the history entry', async () => {
    at(`?code=${CODE}&state=${STATE}#frag`)
    let answer: (v: Response) => void = () => {}
    globalThis.fetch = vi.fn(() => new Promise<Response>((r) => (answer = r))) as unknown as typeof fetch
    const before = window.history.length
    await mount()
    // Still posting: the answer has not arrived, and the bar is already clean.
    expect(screen.getByText(/Finishing the connection with GitHub/)).toBeTruthy()
    await waitFor(() => expect(window.location.href).toBe(`${window.location.origin}${CALLBACK}`))
    expect(window.location.search).toBe('')
    expect(window.location.hash).toBe('')
    // Replaced, not pushed: Back must not return to the address with the code.
    expect(window.history.length).toBe(before)
    expect(everything()).not.toContain(CODE)
    answer(new Response(JSON.stringify({ connection: CONNECTION }), { status: 200, headers: { 'content-type': 'application/json' } }))
  })

  it('a visit with no state posts nothing and offers Connect GitHub', async () => {
    at('')
    const calls = serve(() => null)
    await mount()
    expect(await screen.findByText('Nothing to finish here')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Connect GitHub' })).toBeTruthy()
    expect(calls).toHaveLength(0)
  })
})

describe('the router serves the callback path', () => {
  it('resolves /onboarding/github/callback to this page, and its query never re-enters the bar', async () => {
    at(`?code=${CODE}&state=${STATE}`)
    const calls = serve((m, url) => {
      if (m === 'POST' && url === '/v1/onboarding/github/exchange') return { status: 200, body: { connection: CONNECTION } }
      if (m === 'GET' && url === '/v1/onboarding') return { status: 200, body: onboardingDoc(OWNERS) }
      return null
    })
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { App } = await import('../App')
    render(<App />)
    expect(await screen.findByText('Connected as @octo-dev')).toBeTruthy()
    expect(screen.queryByText(/No page at/)).toBeNull()
    expect(window.location.pathname).toBe(CALLBACK)
    expect(window.location.search).toBe('')
    expect(exchanges(calls)).toHaveLength(1)
    expect(everything()).not.toContain(CODE)
  })

  it('maps the path both ways without its query', async () => {
    const { pathToAddress, addressToPath, GITHUB_CALLBACK_ADDRESS } = await import('../paths')
    expect(pathToAddress(CALLBACK, `?code=${CODE}&state=${STATE}`)).toEqual({ address: GITHUB_CALLBACK_ADDRESS, agentTab: null })
    expect(addressToPath(GITHUB_CALLBACK_ADDRESS)).toBe(CALLBACK)
  })
})
