// ADMIN › SETUP'S STEP PANELS (DAYONE-SETUP-UI-1; part of #2688; plan §5.1).
//
// WHAT EACH CASE HOLDS:
//   * the verify row (D26, the gap P1 found) counts from the engine's own
//     `verify` field: the check, done of total, elapsed, a countdown to the
//     expected end and to the deadline;
//   * "Send to your team" stays locked, with no plugin line, until verify
//     passes;
//   * a failed step turns red with its one-line cause, the fix and a Retry
//     that re-runs that step's check;
//   * the plugin line's only value is the deployment URL;
//   * the GitHub App callback posts `{code, state}` once and says the secrets
//     were stored without drawing any value.
//
// Fixtures only; no network. Fake values are built at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { StrictMode } from 'react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>

/** The instant every case reads as "now", in epoch seconds (the engine's clock). */
const T0 = Date.parse('2026-10-29T15:00:00Z') / 1000
const NOW_MS = (T0 + 240) * 1000
const HOST = 'swarm.example.com'

function state(over: Json = {}, engine: Json = {}): Json {
  return {
    env: 'prod',
    engine: {
      release: 'v1.0.0',
      hostname: HOST,
      stages: Array.from({ length: 16 }, (_, i) =>
        i <= 8 || i === 10 || i === 13 ? { state: 'done', at: '2026-10-29T14:40:00Z', by: 'ana@example.com' } : null,
      ),
      lease: { holder: 'laptop', heartbeat: new Date(NOW_MS - 10_000).toISOString(), expires: null },
      verify: null,
      hand_off_at: '2026-10-29T14:45:00Z',
      ready_at: null,
      ...engine,
    },
    cli_running: true,
    github_app: {
      started: true,
      id: 4242,
      slug: 'swarmcloud-example',
      client_id: 'Iv1.example',
      installed_on: 'example-org',
      secrets_written_at: '2026-10-29T14:50:00Z',
      self_check: { ok: true, reason: null, at: '2026-10-29T14:52:00Z' },
    },
    teams: [
      {
        id: 'engineering',
        group: 'engineering@example.com',
        workspace: 'engineering',
        first_admin: 'ana@example.com',
        invites: ['ravi@example.com', 'lea@example.com'],
        progress: { workspace_state: 'ready', created_at: '2026-10-29T14:53:00Z' },
      },
    ],
    accounts: {
      ravi: { owner: 'ravi@example.com', link_expires: '2026-10-30T14:54:00Z', signed_in_at: '2026-10-29T14:56:00Z' },
    },
    finished: { at: '2026-10-29T14:58:00Z', by: 'ana@example.com' },
    addons: [{ name: 'ci', enable: 'swarm addon enable ci', secret_link: false }],
    hostname: HOST,
    github_org: 'example-org',
    ...over,
  }
}

const VERIFYING = { check: 'digests', done: 2, total: 8, started: T0, expected_end: T0 + 600, deadline: T0 + 1800, result: null }

async function mountSteps(doc: Json) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const mod = await import('../AdminSetupSteps')
  const { normSetupState } = await import('../api')
  return render(<mod.AdminSetupSteps state={normSetupState(doc)} now={NOW_MS} />)
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

describe('Admin › Setup step panels', () => {
  it('the verify row shows a countdown from expected and deadline', async () => {
    await mountSteps(state({}, { verify: VERIFYING }))
    const row = document.querySelector('[data-step="verify"]') as HTMLElement
    // MUTATION: drop the verify row from the checklist.
    expect(row).not.toBeNull()
    const text = visible(row)
    // MUTATION: read `done`/`total` from anywhere but the engine's verify field.
    expect(text).toContain('digests')
    expect(text).toContain('2 of 8')
    // MUTATION: count elapsed from `expected_end` instead of `started`.
    expect(text).toContain('4 min elapsed')
    // MUTATION: count the countdown to the deadline instead of the expected end.
    expect(text).toContain('about 6 min left')
    // MUTATION: drop the deadline from the row.
    expect(text).toContain('deadline in 26 min')
  })

  it('the verify row says when it is past its expected end, and still counts to the deadline', async () => {
    const late = { ...VERIFYING, expected_end: T0 + 120 }
    await mountSteps(state({}, { verify: late }))
    const text = visible(document.querySelector('[data-step="verify"]'))
    expect(text).toContain('2 min past the expected end')
    expect(text).toContain('deadline in 26 min')
  })

  it('Send to your team stays locked until verify passes', async () => {
    await mountSteps(state({}, { verify: VERIFYING }))
    const send = screen.getByRole('region', { name: 'Send to your team' })
    // MUTATION: unlock the card on `finished` rather than on verify's result.
    expect(visible(send)).toContain('unlocked when verify passes')
    expect(send.querySelector('figure.c-code')).toBeNull()
    expect(visible(send)).not.toContain('plugin install')
    cleanup()

    await mountSteps(state({}, { verify: { ...VERIFYING, done: 8, result: 'passed' } }))
    const open = screen.getByRole('region', { name: 'Send to your team' })
    expect(visible(open)).not.toContain('unlocked when verify passes')
    expect(open.querySelector('figure.c-code')).not.toBeNull()
  })

  it('a failed verify keeps Send to your team locked and shows the failed check', async () => {
    await mountSteps(state({}, { verify: { ...VERIFYING, check: 'end-to-end', done: 5, result: 'failed' } }))
    const row = document.querySelector('[data-step="verify"]') as HTMLElement
    expect(row.className).toContain('is-failed')
    expect(visible(row)).toContain('end-to-end')
    expect(visible(screen.getByRole('region', { name: 'Send to your team' }))).toContain('unlocked when verify passes')
  })

  it('a failed step shows its fix and a retry', async () => {
    const calls = serve((m, url) =>
      m === 'POST' && url === '/v1/admin/setup/github-app/self-check'
        ? { status: 200, body: { ok: true, reason: null, at: '2026-10-29T15:04:00Z' } }
        : m === 'GET' && url === '/v1/admin/setup'
          ? { status: 200, body: state() }
          : null,
    )
    const failed = state({
      github_app: { ...(state().github_app as Json), self_check: { ok: false, reason: 'the App key did not sign', at: '2026-10-29T14:52:00Z' } },
    })
    await mountSteps(failed)
    const row = document.querySelector('[data-step="github"]') as HTMLElement
    // MUTATION: read a failed self-check as in progress.
    expect(row.className).toContain('is-failed')
    // MUTATION: drop the one-line cause.
    expect(visible(row)).toContain('the App key did not sign')
    // MUTATION: drop the fix.
    expect(within(row).getByText(/^Fix:/)).toBeTruthy()
    // MUTATION: drop Retry, or point it at anything but the step's own check.
    fireEvent.click(within(row).getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url === '/v1/admin/setup/github-app/self-check')).toBe(true))
  })

  it('the workspace apply reads ready as done, and denied as failed with its fix and a retry', async () => {
    const team = (workspace_state: string): Json => ({ teams: [{ ...((state().teams as Json[])[0]), progress: { workspace_state, created_at: '2026-10-29T14:53:00Z' } }] })
    const nsFact = () => within(document.querySelector('[data-step="team"]') as HTMLElement).getByText('Namespace, quota, verify-keys and worker identity').closest('li') as HTMLElement
    // MUTATION: count the namespace done on any value but swarm-api's own `ready`.
    await mountSteps(state(team('ready')))
    expect(nsFact().className).toContain('is-done')
    expect((document.querySelector('[data-step="team"]') as HTMLElement).className).not.toContain('is-failed')
    cleanup()
    const calls = serve((m, url) => (m === 'POST' && url === '/v1/admin/setup/teams' ? { status: 200, body: {} } : m === 'GET' && url === '/v1/admin/setup' ? { status: 200, body: state() } : null))
    // MUTATION: drop `denied` from the states that fail the step.
    await mountSteps(state(team('denied')))
    const row = document.querySelector('[data-step="team"]') as HTMLElement
    expect(row.className).toContain('is-failed')
    expect(nsFact().className).toContain('is-failed')
    expect(within(row).getByText(/^Fix:/)).toBeTruthy()
    fireEvent.click(within(row).getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url === '/v1/admin/setup/teams')).toBe(true))
  })

  it('the plugin line carries only the deployment URL', async () => {
    await mountSteps(state({}, { verify: { ...VERIFYING, done: 8, result: 'passed' } }))
    const send = screen.getByRole('region', { name: 'Send to your team' })
    const code = visible(send.querySelector('figure.c-code pre'))
    expect(code).toContain(`deployment_url=https://${HOST}`)
    // The one value is the URL: no client id, no secret, no tenant, no other key=value.
    // MUTATION: add the OAuth client id (or any second setting) to the line.
    expect(code).not.toContain('Iv1.example')
    expect(code).not.toMatch(/oauth|secret|tenant/i)
    expect(code.match(/[a-z_]+=/g)).toEqual(['deployment_url='])
  })

  it('draws the steps in the confirmed order: GitHub App, first team, Claude accounts, verify, send', async () => {
    await mountSteps(state({ accounts: {}, finished: null }))
    const order = Array.from(document.querySelectorAll('[data-step]')).map((el) => el.getAttribute('data-step'))
    // MUTATION: put the accounts before the team, as the P1-B frames drew them.
    expect(order.filter((k) => k !== 'addons')).toEqual(['cloud', 'github', 'team', 'accounts', 'verify', 'send'])
  })

  it('says swarm install is not running when the lease is stale, and keeps the steps usable', async () => {
    await mountSteps(state({ cli_running: false, github_app: null, teams: [], accounts: {}, finished: null }))
    expect(visible(document.body)).toContain('swarm install is not running')
    expect(screen.getByRole('button', { name: 'Create the GitHub App' })).toBeTruthy()
  })

  it('refuses a pool-account link before the first team, and says why', async () => {
    await mountSteps(state({ teams: [], accounts: {}, finished: null }))
    const row = document.querySelector('[data-step="accounts"]') as HTMLElement
    expect(visible(row)).toContain('after the first team')
    expect(within(row).queryByRole('button', { name: 'Send a sign-in link' })).toBeNull()
  })
})

describe('GitHubAppCallback', () => {
  it('the callback posts code and state once and shows secrets stored without a value', async () => {
    const code = `c${Math.random().toString(36).slice(2, 14)}`
    const st = `s${Math.random().toString(36).slice(2, 14)}`
    const secret = ['-----BEGIN', 'RSA PRIVATE KEY-----'].join(' ')
    const calls = serve((m, url) =>
      m === 'POST' && url === '/v1/admin/setup/github-app/conversion'
        ? // A server that answered more than `{id, slug}` must still draw none of it.
          { status: 200, body: { id: 4242, slug: 'swarmcloud-example', pem: secret, webhook_secret: st } }
        : null,
    )
    window.history.replaceState(null, '', `/admin/setup/github-app/callback?code=${code}&state=${st}`)
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { GitHubAppCallback } = await import('../AdminSetupSteps')
    // StrictMode runs the effect twice: the exchange must still be posted once.
    render(
      <StrictMode>
        <GitHubAppCallback />
      </StrictMode>,
    )
    await screen.findByText(/Secrets stored/)
    const posts = calls.filter((c) => c.url === '/v1/admin/setup/github-app/conversion')
    // MUTATION: post on every render (or once per effect run).
    expect(posts).toHaveLength(1)
    // MUTATION: post anything but `{code, state}`.
    expect(posts[0]?.body).toEqual({ code, state: st })
    const html = document.body.innerHTML
    // MUTATION: draw the answer's extra fields, or echo the code.
    expect(html).not.toContain('PRIVATE KEY')
    expect(html).not.toContain(code)
    expect(html).not.toContain(st)
    expect(visible(document.body)).toContain('swarmcloud-example')
    // The code is single-use: it leaves the address bar once posted.
    expect(window.location.search).toBe('')
  })

  it('a callback without a code or state posts nothing and says how to start again', async () => {
    const calls = serve(() => null)
    window.history.replaceState(null, '', '/admin/setup/github-app/callback')
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { GitHubAppCallback } = await import('../AdminSetupSteps')
    render(<GitHubAppCallback />)
    expect(visible(document.body)).toContain('Create the GitHub App again')
    expect(calls).toHaveLength(0)
  })
})
