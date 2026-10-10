// ADMIN SETTINGS › GITHUB FALLBACK TOKEN (#780, D5; docs/onboarding.md §3.2).
//
// The owner's note on D5: "lets also allow it to be done via the UI admin
// settings page". The card posts {owner, token} ONCE to
// POST /v1/onboarding/github/token, as the signed-in person, for that owner.
//
// WHAT EACH CASE HOLDS:
//   * the form posts {owner, token} once, and a second click while it is out
//     posts nothing more;
//   * the token input is cleared after a success AND after a refusal, and the
//     value appears in no rendered text and no attribute -- not on success,
//     not in the refusal, not in the input's own value;
//   * a refusal draws the server's §2.3 copy word for word;
//   * nothing is posted without an owner and a token;
//   * the Admin settings screen carries the card.
//
// Fixtures only; no network. The fake token is built at runtime
// (`leakedValue()`), never written as one literal.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { leakedValue, serve, tokenShapedIn } from './repofixture'

const ROUTE = '/v1/onboarding/github/token'

const STORED = {
  org: {
    owner: 'example-org',
    method: 'pat',
    owner_type: 'Organization',
    installation_id: null,
    repository_selection: null,
    install_state: 'token',
    sso: 'ok',
    enabled: true,
    enabled_at: new Date().toISOString(),
    requested_at: null,
    checked_at: new Date().toISOString(),
  },
  token: {
    token_id: 'tok_owner',
    secret_name: 'swarm-tenant-eng-git-u-0123456789abcdef',
    kind: 'fine_grained',
    forge_login: 'octo-dev',
    state: 'active',
    expires_at: null,
  },
}

const CLASSIC_COPY =
  'example-org does not accept classic personal access tokens. Connect with the SwarmCloud GitHub App instead (recommended), or create a fine-grained token whose resource owner is example-org and store it with `uv run sc setup token --owner example-org`.'

async function mountCard() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { FallbackTokenCard } = await import('../AdminSettings')
  return render(<FallbackTokenCard />)
}

const ownerInput = () => screen.getByLabelText('Owner') as HTMLInputElement
const tokenInput = () => screen.getByLabelText('Personal access token') as HTMLInputElement
const store = () => screen.getByRole('button', { name: 'Store token' })

function fill(owner: string, token: string) {
  fireEvent.change(ownerInput(), { target: { value: owner } })
  fireEvent.change(tokenInput(), { target: { value: token } })
}

afterEach(() => vi.unstubAllEnvs())

describe('the fallback token form (D5)', () => {
  it('posts {owner, token} once, clears the input, and never draws the value', async () => {
    const value = leakedValue()
    const calls = serve((m, url) => (m === 'POST' && url === ROUTE ? { status: 200, body: STORED } : null))
    await mountCard()
    expect(tokenInput().type).toBe('password')
    fill('example-org', value)
    fireEvent.click(store())
    fireEvent.click(store())
    expect(await screen.findByText(/example-org is enabled through your token/)).toBeTruthy()
    const posts = calls.filter((c) => c.url === ROUTE)
    expect(posts).toEqual([expect.objectContaining({ method: 'POST', body: { owner: 'example-org', token: value } })])
    expect(tokenInput().value).toBe('')
    expect(tokenShapedIn(document.body.innerHTML)).toEqual([])
    expect(document.body.textContent ?? '').not.toContain(value)
  })

  it("clears the input after a refusal and draws the refusal's §2.3 copy word for word", async () => {
    const value = leakedValue()
    const calls = serve((m, url) =>
      m === 'POST' && url === ROUTE
        ? {
            status: 403,
            body: {
              code: 'authorisation_refused',
              message: 'example-org refused the token; nothing was stored',
              detail: { failure_code: 'CLASSIC_PAT_BLOCKED', recovery: CLASSIC_COPY },
            },
          }
        : null,
    )
    await mountCard()
    fill('example-org', value)
    fireEvent.click(store())
    expect(await screen.findByText(CLASSIC_COPY)).toBeTruthy()
    expect(screen.getByText(/CLASSIC_PAT_BLOCKED/)).toBeTruthy()
    expect(calls.filter((c) => c.url === ROUTE)).toHaveLength(1)
    expect(tokenInput().value).toBe('')
    expect(tokenShapedIn(document.body.innerHTML)).toEqual([])
  })

  it('posts nothing without an owner and a token', async () => {
    const calls = serve(() => null)
    await mountCard()
    fill('', leakedValue())
    fireEvent.click(store())
    fill('example-org', '')
    fireEvent.click(store())
    await new Promise((r) => setTimeout(r, 20))
    expect(calls.filter((c) => c.url === ROUTE)).toEqual([])
  })
})

describe('the Admin settings screen', () => {
  it('carries the GitHub fallback token card', async () => {
    serve(() => null)
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { AdminSettingsScreen } = await import('../AdminSettings')
    render(<AdminSettingsScreen />)
    await waitFor(() => expect(screen.getByText('GitHub fallback token')).toBeTruthy())
    expect(screen.getByLabelText('Personal access token')).toBeTruthy()
  })
})
