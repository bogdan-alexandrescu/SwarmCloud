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

// ADMIN › SETUP SAYS ONLY WHAT EXISTS (DAYONE-SETUP-UI-4; part of #2688).
//
// WHAT EACH CASE HOLDS:
//   * the verify row lists the checks stage 14 runs (the end-to-end task and
//     workflow, GKE, live browser, code intelligence), and nothing it does not
//     (no smoke, no race, no "GKE proof");
//   * "Run the console checks" posts SETUP-API-2's route and shows both runs'
//     states, each linked to its page;
//   * the team row shows its repository grant over the App installation's
//     repositories;
//   * the invite control opens the Google Admin members page and says
//     "waiting for <email> to join" until the Groups API sees them;
//   * a read-only Capacity card shows the sizing;
//   * a one-person install replaces the first-team and invites steps with one line.

const GRANTED = { state: 'granted', mode: 'write', account: 'example-org', selection: 'selected', repositories: ['example-org/app', 'example-org/lib'] }
const MEMBERS_URL = 'https://admin.google.com/ac/groups/g0123/members'

function teamWith(extra: Json): Json {
  return { teams: [{ ...((state().teams as Json[])[0]), ...extra }] }
}

describe('Admin › Setup says only what exists', () => {
  it('the verify row lists the checks verify runs, and none it does not', async () => {
    await mountSteps(state({ finished: null }))
    const text = visible(document.querySelector('[data-step="verify"]'))
    // MUTATION: keep the old list (smoke, GKE proof, race).
    expect(text).not.toMatch(/\brace\b/)
    expect(text).not.toContain('GKE proof')
    expect(text).not.toMatch(/\bsmoke\b/)
    // MUTATION: drop any of the checks VERIFY-ENGINE runs.
    expect(text).toContain('a task and a workflow')
    expect(text).toContain('a task on GKE')
    expect(text).toContain('a live-browser task')
    expect(text).toContain('a code-intelligence read')
  })

  it('Run the console checks starts both runs and shows each one’s state, linked', async () => {
    const calls = serve((m, url) => {
      if (m === 'POST' && url === '/v1/admin/setup/verify-runs') {
        return { status: 200, body: { verify_runs: { console_task: 'task_v1', console_workflow: 'wf_v1' }, repository: 'example-org/app', started: true } }
      }
      if (m === 'GET' && url.startsWith('/v1/tasks/task_v1')) return { status: 200, body: { task: { id: 'task_v1', tenant_id: 'engineering', state: 'RUNNING' } } }
      if (m === 'GET' && url.startsWith('/v1/workflows/wf_v1')) return { status: 200, body: { workflow: { id: 'wf_v1', state: 'SUCCEEDED' }, tasks: [] } }
      return null
    })
    await mountSteps(state({ finished: null, ...teamWith({ grant: GRANTED }) }))
    const row = document.querySelector('[data-step="verify"]') as HTMLElement
    // MUTATION: drop the button.
    fireEvent.click(within(row).getByRole('button', { name: 'Run the console checks' }))
    // MUTATION: post anything but SETUP-API-2's route.
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url === '/v1/admin/setup/verify-runs')).toBe(true))
    const task = await within(row).findByRole('link', { name: /task_v1/ })
    const wf = await within(row).findByRole('link', { name: /wf_v1/ })
    // MUTATION: link the runs anywhere but their own pages.
    expect(task.getAttribute('href')).toContain('task_v1')
    expect(wf.getAttribute('href')).toBe('/workflows/wf_v1')
    // MUTATION: show the ids without the runs' states.
    await waitFor(() => expect(visible(row)).toContain('running'))
    await waitFor(() => expect(visible(row)).toContain('succeeded'))
    expect(visible(row)).toContain('example-org/app')
  })

  it('reads runs already started from the status route, without a click', async () => {
    serve((m, url) =>
      m === 'GET' && url.startsWith('/v1/tasks/task_v2')
        ? { status: 200, body: { task: { id: 'task_v2', tenant_id: 'engineering', state: 'FAILED' } } }
        : m === 'GET' && url.startsWith('/v1/workflows/wf_v2')
          ? { status: 200, body: { workflow: { id: 'wf_v2', state: 'RUNNING' }, tasks: [] } }
          : null,
    )
    await mountSteps(state({ finished: null, verify_runs: { verify_runs: { console_task: 'task_v2', console_workflow: 'wf_v2' }, repository: 'example-org/app' } }))
    const row = document.querySelector('[data-step="verify"]') as HTMLElement
    // MUTATION: ignore the status route's `verify_runs`.
    expect(await within(row).findByRole('link', { name: /task_v2/ })).toBeTruthy()
    await waitFor(() => expect(visible(row)).toContain('failed'))
    expect(within(row).queryByRole('button', { name: 'Run the console checks' })).toBeNull()
  })

  it('the team row shows its repository grant', async () => {
    await mountSteps(state(teamWith({ grant: GRANTED })))
    const fact = within(document.querySelector('[data-step="team"]') as HTMLElement).getByText('Repository grant').closest('li') as HTMLElement
    // MUTATION: drop the grant, or show it as anything but done when granted.
    expect(fact.className).toContain('is-done')
    expect(visible(fact)).toContain('example-org/app')
    expect(visible(fact)).toContain('example-org/lib')
    cleanup()
    await mountSteps(state(teamWith({ grant: { state: 'waiting_for_repositories', account: 'example-org' } })))
    const waiting = within(document.querySelector('[data-step="team"]') as HTMLElement).getByText('Repository grant').closest('li') as HTMLElement
    // MUTATION: draw a grant that is still waiting as done.
    expect(waiting.className).toContain('is-in_progress')
    expect(visible(waiting)).toContain('no repository')
  })

  it('the invite control opens the Google Admin members page and waits for them to join', async () => {
    const opened: string[] = []
    const open = vi.spyOn(window, 'open').mockImplementation((url) => {
      opened.push(String(url))
      return null
    })
    const calls = serve((m, url, body) =>
      m === 'POST' && url === '/v1/admin/setup/teams/engineering/invites'
        ? { status: 200, body: { team: 'engineering', email: (body as Json).email, group: 'engineering@example.com', add_members_url: MEMBERS_URL, member: false } }
        : m === 'GET' && url === '/v1/admin/setup'
          ? { status: 200, body: state() }
          : null,
    )
    await mountSteps(state(teamWith({ members: [{ email: 'ravi@example.com', member: false }, { email: 'lea@example.com', member: true }] })))
    const row = document.querySelector('[data-step="team"]') as HTMLElement
    // MUTATION: read an invite as joined before the Groups API says so.
    expect(visible(row)).toContain('waiting for ravi@example.com to join')
    expect(visible(row)).not.toContain('waiting for lea@example.com to join')
    // MUTATION: promise a sign-in link nobody sends.
    expect(visible(document.body)).not.toContain('sign-in link; nobody')
    fireEvent.change(within(row).getByLabelText('Invite someone'), { target: { value: 'kim@example.com' } })
    fireEvent.click(within(row).getByRole('button', { name: 'Invite' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url === '/v1/admin/setup/teams/engineering/invites')).toBe(true))
    expect(calls.find((c) => c.url === '/v1/admin/setup/teams/engineering/invites')?.body).toEqual({ email: 'kim@example.com' })
    // MUTATION: leave the person to find the page.
    await waitFor(() => expect(opened).toEqual([MEMBERS_URL]))
    const link = await within(row).findByRole('link', { name: /Google Admin/ })
    expect(link.getAttribute('href')).toBe(MEMBERS_URL)
    expect(visible(row)).toContain('waiting for kim@example.com to join')
    open.mockRestore()
  })

  it('the team form says what an invite does', async () => {
    await mountSteps(state({ teams: [], accounts: {}, finished: null }))
    const row = document.querySelector('[data-step="team"]') as HTMLElement
    // MUTATION: keep "each gets a sign-in link".
    expect(visible(row)).not.toContain('sign-in link')
    expect(visible(row)).toContain('Google Admin')
  })

  it('a read-only Capacity card shows the sizing', async () => {
    const sizing = {
      mode: 'team',
      members: 12,
      pool_limits: { global: 40, default_tenant: 15, provider_tenant: 10, resource_classes: {}, runner_profiles: {}, backends: {} },
      service_max_instances: { 'swarm-api': 4, 'swarm-scheduler': 2, 'swarm-quota-broker': 1, 'swarm-reconciler': 1 },
      pool_accounts: 3,
      budget_usd: 900,
    }
    await mountSteps(state({ sizing }))
    const card = screen.getByRole('region', { name: 'Capacity' })
    const text = visible(card)
    // MUTATION: drop any of the sizing's figures.
    expect(text).toContain('A team of 12 people')
    expect(text).toContain('40 agents at once')
    expect(text).toContain('15 agents at once per workspace')
    expect(text).toContain('3 Claude subscription accounts')
    expect(text).toContain('$900 a month')
    // MUTATION: make the card editable.
    expect(card.querySelector('input, button, select, textarea')).toBeNull()
    cleanup()
    await mountSteps(state())
    expect(visible(screen.getByRole('region', { name: 'Capacity' }))).toContain('not recorded')
  })

  it('a one-person install replaces the first-team and invites steps with one line', async () => {
    const sizing = { mode: 'individual', members: 1, pool_limits: { global: 4, default_tenant: 4 }, service_max_instances: {}, pool_accounts: 1, budget_usd: 150 }
    await mountSteps(state({ sizing, teams: [], accounts: {}, finished: null }))
    const row = document.querySelector('[data-step="team"]') as HTMLElement
    // MUTATION: draw the team form for one person.
    expect(visible(row)).toContain('One-person install: your personal workspace is ready.')
    expect(row.className).toContain('is-done')
    expect(within(row).queryByRole('button', { name: 'Create the team' })).toBeNull()
    expect(within(document.body).queryByLabelText('Invite someone')).toBeNull()
    // MUTATION: keep the accounts gated on a team one person never makes.
    const accounts = document.querySelector('[data-step="accounts"]') as HTMLElement
    expect(visible(accounts)).not.toContain('after the first team')
    expect(within(accounts).getByRole('button', { name: 'Send a sign-in link' })).toBeTruthy()
    expect(visible(screen.getByRole('region', { name: 'Capacity' }))).toContain('One person')
  })

  it('Send to your team names no command that does not exist', async () => {
    await mountSteps(state({}, { verify: { ...VERIFYING, done: 8, result: 'passed' } }))
    // MUTATION: keep "swarm doctor": the CLI has no such verb.
    expect(visible(screen.getByRole('region', { name: 'Send to your team' }))).not.toContain('swarm doctor')
  })
})
