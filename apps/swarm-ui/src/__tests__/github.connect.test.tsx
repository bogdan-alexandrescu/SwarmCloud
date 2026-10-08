// CONNECT GITHUB, THE CARD on Work › Repositories › Git tokens (#780, OB3).
//
// WHAT EACH CASE HOLDS:
//   * the card says how the caller's tasks reach GitHub today, read from
//     `GET /v1/onboarding`'s `github_connected` evidence: through the tenant
//     token, through a token of their own, or through their App connection;
//   * Connect posts `authorize` with surface `console` and sends the browser
//     to the authorise URL it answered -- a github.com one, nothing else;
//   * Disconnect is two presses: the first only asks, Keep connected backs
//     out, and only the confirm sends `DELETE /v1/onboarding/github`; then the
//     card and the page's token records are read again;
//   * a connection whose refresh failed says so with the §2.3 copy and offers
//     Reconnect;
//   * at 390 the card's actions stack, one per row.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, token } from './repofixture'
import { cascade } from './cssgate'
import { SHEETS as ALL } from './sheets'

type Evidence = Record<string, unknown>

function doc(connected: Evidence, connectedState = 'done', copy: string | null = null) {
  return {
    tenant_id: 'eng',
    user: 'dev@swarm.example.com',
    user_hash: '89abcdef01234567',
    derived_at: new Date().toISOString(),
    next_step: 'orgs_enabled',
    complete: false,
    source: 'derived on this read',
    steps: [
      { step: 'signed_in', state: 'done', code: null, copy: null, checked_at: null, evidence: {}, issues: [] },
      { step: 'github_connected', state: connectedState, code: copy === null ? null : 'REFRESH_FAILED', copy, checked_at: null, evidence: connected, issues: [] },
      {
        step: 'orgs_enabled',
        state: 'done',
        code: null,
        copy: null,
        checked_at: null,
        evidence: {
          owners: [{ owner: 'octo-dev', owner_type: 'User', source: 'account', reach: 'reachable', registered: 0 }],
          orgs_read: true,
          orgs_capped: false,
          sso_hidden_orgs: 0,
          read_at: null,
        },
        issues: [],
      },
    ],
  }
}

const TENANT = doc({ via: 'tenant', kind: 'classic_pat', forge_login: 'eng-bot', token_state: 'active' })
const APP = doc({ via: 'user', kind: 'app_user', forge_login: 'octo-dev', token_state: 'active' })

async function mount() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const mod = await import('../GitHubConnect')
  const { RepositoriesScreen } = await import('../Repositories')
  render(<RepositoriesScreen view="page=tokens" go={vi.fn()} />)
  return mod
}

afterEach(() => vi.unstubAllEnvs())

const card = async () => {
  const heading = await screen.findByText('Connect GitHub', { selector: 'h2, h3' })
  return heading.closest<HTMLElement>('.ur-gh')!
}

function routes(onboarding: () => unknown, extra: (m: string, url: string) => { status: number; body: unknown } | null = () => null) {
  return serve((m, url) => {
    if (m === 'GET' && url === '/v1/onboarding') return { status: 200, body: onboarding() }
    if (m === 'GET' && url === '/v1/git-tokens') return { status: 200, body: { git_tokens: [token()] } }
    if (m === 'GET' && url === '/v1/repositories') return { status: 200, body: { repositories: [] } }
    return extra(m, url)
  })
}

describe('the card says how your tasks reach GitHub', () => {
  it('through the tenant token: says so and offers Connect, which opens GitHub', async () => {
    const calls = routes(
      () => TENANT,
      (m, url) =>
        m === 'POST' && url === '/v1/onboarding/github/authorize'
          ? { status: 200, body: { authorize_url: 'https://github.com/login/oauth/authorize?client_id=Iv1.example', expires_in_seconds: 600 } }
          : null,
    )
    const mod = await mount()
    const assign = vi.spyOn(mod.browser, 'assign').mockImplementation(() => {})
    const c = await card()
    await waitFor(() => expect(c.textContent).toContain('through the tenant token'))
    expect(c.textContent).toContain('@eng-bot')
    expect(within(c).queryByRole('button', { name: 'Disconnect' })).toBeNull()
    fireEvent.click(within(c).getByRole('button', { name: 'Connect GitHub' }))
    await waitFor(() => expect(assign).toHaveBeenCalledWith('https://github.com/login/oauth/authorize?client_id=Iv1.example'))
    expect(calls.filter((x) => x.url === '/v1/onboarding/github/authorize')).toEqual([
      expect.objectContaining({ method: 'POST', body: { surface: 'console' } }),
    ])
  })

  it('a refused authorize is shown, and the browser stays', async () => {
    routes(
      () => doc({ via: null }, 'todo'),
      (m, url) =>
        m === 'POST' && url === '/v1/onboarding/github/authorize'
          ? { status: 503, body: { code: 'github_app_not_configured', message: 'the SwarmCloud GitHub App is not configured: SWARM_GITHUB_APP_CLIENT_ID' } }
          : null,
    )
    const mod = await mount()
    const assign = vi.spyOn(mod.browser, 'assign').mockImplementation(() => {})
    const c = await card()
    await waitFor(() => expect(c.textContent).toContain('No GitHub account is connected'))
    fireEvent.click(within(c).getByRole('button', { name: 'Connect GitHub' }))
    expect(await within(c).findByText(/is not configured/)).toBeTruthy()
    expect(assign).not.toHaveBeenCalled()
  })

  it('through your App connection: "Connected as @<login>" and the accounts it reaches', async () => {
    routes(() => APP)
    await mount()
    const c = await card()
    await waitFor(() => expect(c.textContent).toContain('Connected as @octo-dev'))
    expect(within(c).getByRole('list', { name: 'Accounts and orgs this connection reaches' }).textContent).toContain('octo-dev')
    expect(within(c).queryByRole('button', { name: 'Connect GitHub' })).toBeNull()
  })

  it('a connection whose refresh failed says so in §2.3 words and offers Reconnect', async () => {
    const copy = "SwarmCloud's access as octo-dev has ended at GitHub, so tasks you submit will wait instead of running. Press Reconnect to authorise again; your orgs and repositories are kept."
    routes(() => doc({ via: 'user', kind: 'app_user', forge_login: 'octo-dev', token_state: 'expired' }, 'failed', copy))
    await mount()
    const c = await card()
    expect(await within(c).findByText(copy)).toBeTruthy()
    expect(within(c).getByRole('button', { name: 'Reconnect' })).toBeTruthy()
    expect(within(c).getByRole('button', { name: 'Disconnect' })).toBeTruthy()
  })
})

describe('Disconnect asks first', () => {
  it('sends nothing on the first press, backs out on Keep connected, and DELETEs only on the confirm', async () => {
    let connected = true
    const calls = routes(
      () => (connected ? APP : TENANT),
      (m, url) => {
        if (m === 'DELETE' && url === '/v1/onboarding/github') {
          connected = false
          return {
            status: 200,
            body: { connection: {}, github_revoked: true, github: 'revoked at GitHub', slot_versions_disabled: {}, grants_deleted: 2 },
          }
        }
        return null
      },
    )
    await mount()
    const c = await card()
    await waitFor(() => expect(c.textContent).toContain('Connected as @octo-dev'))
    const deletes = () => calls.filter((x) => x.method === 'DELETE')

    fireEvent.click(within(c).getByRole('button', { name: 'Disconnect' }))
    expect(within(c).getByText('Disconnect @octo-dev?')).toBeTruthy()
    expect(deletes()).toHaveLength(0)
    fireEvent.click(within(c).getByRole('button', { name: 'Keep connected' }))
    expect(within(c).queryByText('Disconnect @octo-dev?')).toBeNull()
    expect(deletes()).toHaveLength(0)

    fireEvent.click(within(c).getByRole('button', { name: 'Disconnect' }))
    const reads = () => calls.filter((x) => x.method === 'GET' && (x.url === '/v1/onboarding' || x.url === '/v1/git-tokens')).length
    const before = reads()
    fireEvent.click(within(c).getByRole('button', { name: 'Disconnect' }))
    await waitFor(() => expect(deletes()).toHaveLength(1))
    expect(await within(c).findByText('GitHub was disconnected')).toBeTruthy()
    expect(within(c).getByText('revoked at GitHub')).toBeTruthy()
    // The card and the page's token records are read again.
    await waitFor(() => expect(reads()).toBeGreaterThanOrEqual(before + 2))
    await waitFor(() => expect(c.textContent).toContain('through the tenant token'))
  })
})

describe('at 390', () => {
  it("stacks the card's actions one per row", async () => {
    routes(() => APP)
    await mount()
    const c = await card()
    await waitFor(() => expect(c.querySelector('.ur-gh-acts')).not.toBeNull())
    const acts = c.querySelector('.ur-gh-acts')!
    const sheets = ALL.map(([, t]) => t).join('\n')
    expect(cascade(sheets, acts, 'flex-direction', { width: 390 }).winner?.value).toBe('column')
    expect(cascade(sheets, acts, 'flex-direction', { width: 1440 }).winner?.value).not.toBe('column')
  })
})
