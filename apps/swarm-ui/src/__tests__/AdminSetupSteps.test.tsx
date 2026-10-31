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

const rand = (n: number) => Array.from(crypto.getRandomValues(new Uint8Array(n)), (b) => (b % 36).toString(36)).join('')

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

/**
 * `window.open`, as a browser answers it inside a click: a blank tab the page
 * points at the members page later. `opens` holds the URL each call asked
 * for ('' for a blank tab) and `tabs` each tab's address now.
 */
function fakeTabs(): { opens: string[]; tabs: { location: { href: string }; closed: boolean }[]; restore: () => void } {
  const opens: string[] = []
  const tabs: { location: { href: string }; closed: boolean; opener: unknown; close: () => void }[] = []
  const spy = vi.spyOn(window, 'open').mockImplementation((url) => {
    opens.push(String(url ?? ''))
    const tab = { location: { href: String(url ?? '') }, closed: false, opener: window as unknown, close: () => (tab.closed = true) }
    tabs.push(tab)
    return tab as unknown as Window
  })
  return { opens, tabs, restore: () => spy.mockRestore() }
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

  it('a failed workspace is retried by its id, never by asking for the team again (audit 5b, M6)', async () => {
    const failed: Json = { teams: [{ ...((state().teams as Json[])[0]), workspace_id: 'w-0abc', progress: { workspace_state: 'failed', created_at: '2026-10-29T14:53:00Z' } }] }
    const calls = serve((m, url) =>
      m === 'POST' && url === '/v1/admin/workspaces/w-0abc/retry'
        ? { status: 200, body: { workspace_id: 'w-0abc', state: 'approved' } }
        : m === 'GET' && url === '/v1/admin/setup'
          ? { status: 200, body: state() }
          : null,
    )
    await mountSteps(state(failed))
    const row = document.querySelector('[data-step="team"]') as HTMLElement
    expect(row.className).toContain('is-failed')
    fireEvent.click(within(row).getByRole('button', { name: 'Retry' }))
    // MUTATION: re-post the team, which the route answers 409 WORKSPACE_FAILED.
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url === '/v1/admin/workspaces/w-0abc/retry')).toBe(true))
    expect(calls.some((c) => c.method === 'POST' && c.url === '/v1/admin/setup/teams')).toBe(false)
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
    expect(within(row).queryByRole('button', { name: /sign-in link|Sign in my Claude account/ })).toBeNull()
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

  it('nobody is asked to start the console checks: the hand-over starts them (audit 5b, B1)', async () => {
    await mountSteps(state({ finished: null, ...teamWith({ grant: GRANTED }) }))
    const row = document.querySelector('[data-step="verify"]') as HTMLElement
    // MUTATION: keep a "Run the console checks" button that nothing tells the person to press.
    expect(within(row).queryByRole('button', { name: /Run the console checks/ })).toBeNull()
    expect(visible(row)).toContain('start by themselves when you hand over')
    cleanup()
    await mountSteps(state(teamWith({ grant: GRANTED })))
    const after = document.querySelector('[data-step="verify"]') as HTMLElement
    // MUTATION: wait on a click once handed over; swarm-api starts them on its next read.
    expect(within(after).queryByRole('button', { name: /Run the console checks/ })).toBeNull()
    expect(visible(after)).toContain('swarm-api starts them on this page’s next read')
  })

  it('draws the runs the hand-over started, each with its state, linked', async () => {
    serve((m, url) => {
      if (m === 'GET' && url.startsWith('/v1/tasks/task_v1')) return { status: 200, body: { task: { id: 'task_v1', tenant_id: 'engineering', state: 'RUNNING' } } }
      if (m === 'GET' && url.startsWith('/v1/workflows/wf_v1')) return { status: 200, body: { workflow: { id: 'wf_v1', state: 'SUCCEEDED' }, tasks: [] } }
      return null
    })
    await mountSteps(state({ ...teamWith({ grant: GRANTED }), verify_runs: { verify_runs: { console_task: 'task_v1', console_workflow: 'wf_v1' }, repository: 'example-org/app' } }))
    const row = document.querySelector('[data-step="verify"]') as HTMLElement
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
    // A failed run starts again by itself (audit 5b, B1), with no button: see the case below.
    expect(within(row).queryByRole('button', { name: /Run the console checks/ })).toBeNull()
    expect(visible(row)).toContain('it starts again when you hand over: 2 of 2 fresh starts left')
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
    const tabs = fakeTabs()
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
    // MUTATION: open the tab after the fetch, outside the click, where popup blockers stop it (audit 5b, M5).
    expect(tabs.opens).toEqual([''])
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url === '/v1/admin/setup/teams/engineering/invites')).toBe(true))
    expect(calls.find((c) => c.url === '/v1/admin/setup/teams/engineering/invites')?.body).toEqual({ email: 'kim@example.com' })
    // MUTATION: leave the person to find the page.
    await waitFor(() => expect(tabs.tabs[0]?.location.href).toBe(MEMBERS_URL))
    const link = await within(row).findByRole('link', { name: /Google Admin/ })
    expect(link.getAttribute('href')).toBe(MEMBERS_URL)
    expect(visible(row)).toContain('waiting for kim@example.com to join')
    tabs.restore()
  })

  it('the team row keeps the members page for the invites made with the team (audit 5b, M5)', async () => {
    await mountSteps(state(teamWith({ add_members_url: MEMBERS_URL, members: [{ email: 'ravi@example.com', member: false }, { email: 'lea@example.com', member: true }] })))
    const row = document.querySelector('[data-step="team"]') as HTMLElement
    // MUTATION: build the link only from invites made in this session.
    const link = within(row).getByRole('link', { name: 'Open the group in Google Admin' })
    expect(link.getAttribute('href')).toBe(MEMBERS_URL)
    cleanup()
    await mountSteps(state(teamWith({ add_members_url: MEMBERS_URL, members: [{ email: 'lea@example.com', member: true }] })))
    // MUTATION: keep the link once nobody is waiting to join.
    expect(within(document.querySelector('[data-step="team"]') as HTMLElement).queryByRole('link', { name: 'Open the group in Google Admin' })).toBeNull()
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
    // The route's `personal` row, the installer's personal workspace, ready (DAYONE-SETUP-UI-5A).
    await mountSteps(onePerson({}, { accounts: {} }))
    const row = document.querySelector('[data-step="team"]') as HTMLElement
    // MUTATION: draw the team form for one person.
    expect(visible(row)).toContain('One-person install: your personal workspace is ready.')
    expect(row.className).toContain('is-done')
    expect(within(row).queryByRole('button', { name: 'Create the team' })).toBeNull()
    expect(within(document.body).queryByLabelText('Invite someone')).toBeNull()
    // MUTATION: keep the accounts gated on a team one person never makes.
    const accounts = document.querySelector('[data-step="accounts"]') as HTMLElement
    expect(visible(accounts)).not.toContain('after the first team')
    expect(within(accounts).getByRole('button', { name: 'Sign in my Claude account' })).toBeTruthy()
    expect(visible(screen.getByRole('region', { name: 'Capacity' }))).toContain('One person')
  })

  it('Send to your team names no command that does not exist', async () => {
    await mountSteps(state({}, { verify: { ...VERIFYING, done: 8, result: 'passed' } }))
    // MUTATION: keep "swarm doctor": the CLI has no such verb.
    expect(visible(screen.getByRole('region', { name: 'Send to your team' }))).not.toContain('swarm doctor')
  })
})

// DAYONE-SETUP-UI-5A (part of #2688; audit findings 12, 20, 27 and 28):
//   * a one-person install reaches the hand-over: the personal workspace's
//     row (the route's `personal`) takes the first team's place, its pool
//     accounts count as signed in, and the hand-over rests on its smoke;
//   * the smoke reads the route's own word, `succeeded`; a failed one has a
//     Retry that posts `POST /v1/admin/setup/smoke`; the hand-over waits for it;
//   * the team form links Google Admin's groups page and shows the route's
//     refusal when the group cannot be read or the installer is not in it;
//   * the App self-check waits for swarm install's infra pass 3, and
//     `app_id_not_configured` reads as that wait, never as a failure.

const ONE_PERSON_SIZING = { mode: 'individual', members: 1, pool_limits: { global: 4, default_tenant: 4 }, service_max_instances: {}, pool_accounts: 1, budget_usd: 150 }
const SUCCEEDED_SMOKE = { state: 'succeeded', task_id: 'task_smoke', repository: 'example-org/app' }

/** A one-person install as the route serves it: `mode`, `personal` and `steps`, and no team. */
function onePerson(personal: Json = {}, over: Json = {}): Json {
  return state({
    sizing: ONE_PERSON_SIZING,
    mode: 'individual',
    steps: {
      first_team: { needed: false, state: 'not_needed', reason: 'not needed for one person' },
      invites: { needed: false, state: 'not_needed', reason: 'not needed for one person' },
    },
    personal: {
      owner: 'ana@example.com',
      workspace: 'u-ana',
      progress: { created_at: '2026-10-29T14:53:00Z' },
      workspace_state: 'ready',
      grant: GRANTED,
      smoke: SUCCEEDED_SMOKE,
      pool_accounts: ['ana'],
      ...personal,
    },
    teams: [],
    accounts: { ana: { owner: 'ana@example.com', link_expires: '2026-10-30T14:54:00Z' } },
    finished: null,
    ...over,
  })
}

const handOver = () => screen.queryByRole('button', { name: 'Hand over to swarm install' })

describe('Admin › Setup finishes a one-person install (finding 12)', () => {
  it('carries mode, personal and steps through normSetupState', async () => {
    const { normSetupState } = await import('../api')
    const s = normSetupState(onePerson())
    // MUTATION: drop any of the three the route serves.
    expect(s.mode).toBe('individual')
    expect(s.personal?.workspace).toBe('u-ana')
    expect(s.personal?.pool_accounts).toEqual(['ana'])
    expect(s.personal?.smoke?.state).toBe('succeeded')
    expect(s.steps?.first_team?.needed).toBe(false)
    expect(s.steps?.invites?.reason).toBe('not needed for one person')
    // A route from before `mode` is read by the sizing.
    expect(normSetupState(state({ sizing: ONE_PERSON_SIZING })).mode).toBe('individual')
    expect(normSetupState(state()).mode).toBe('team')
  })

  it('draws the personal workspace in the first team’s place, with the first-team and invites steps not needed', async () => {
    await mountSteps(onePerson())
    const row = document.querySelector('[data-step="team"]') as HTMLElement
    const text = visible(row)
    // MUTATION: draw the team form, or nothing of the personal workspace.
    expect(text).toContain('u-ana')
    expect(text).toContain('not needed for one person')
    expect(within(row).queryByRole('button', { name: 'Create the team' })).toBeNull()
    expect(within(row).getByText('Repository grant').closest('li')?.className).toContain('is-done')
    expect(within(row).getByText('Smoke agent').closest('li')?.className).toContain('is-done')
  })

  it('counts an account in the personal workspace’s pool as signed in, and hands over on the personal smoke', async () => {
    await mountSteps(onePerson())
    const accounts = document.querySelector('[data-step="accounts"]') as HTMLElement
    // MUTATION: count signed in only from `signed_in_at`, which only the hand-over writes.
    expect(accounts.className).toContain('is-done')
    expect(visible(accounts)).toContain('your personal workspace')
    // MUTATION: keep the hand-over on `state.teams`, which one person never has.
    expect(handOver()).not.toBeNull()
  })

  it('says sign-in goes through the console’s own link, never sc account add', async () => {
    await mountSteps(onePerson({ pool_accounts: [] }, { accounts: { ana: { owner: 'ana@example.com', link_expires: '2026-10-30T14:54:00Z' } } }))
    // MUTATION: keep "or they run sc account add".
    expect(visible(document.body)).not.toContain('sc account add')
    cleanup()
    await mountSteps(onePerson({ pool_accounts: [] }, { accounts: {} }))
    expect(visible(document.body)).not.toContain('sc account add')
    expect(visible(document.querySelector('[data-step="accounts"]'))).toContain('one-time link')
  })

  it('waits while the personal workspace is not filed yet, with the route’s reason', async () => {
    await mountSteps(onePerson({ workspace: null, workspace_state: null, grant: null, smoke: null, pool_accounts: [], reason: 'no_installer' }))
    const row = document.querySelector('[data-step="team"]') as HTMLElement
    // MUTATION: call the personal workspace ready before it exists.
    expect(row.className).not.toContain('is-done')
    expect(visible(row)).toContain('no_installer')
    expect(handOver()).toBeNull()
  })
})

describe('the first workspace’s smoke (finding 20)', () => {
  it('reads the route’s `succeeded` as done', async () => {
    await mountSteps(state({ finished: null, ...teamWith({ grant: GRANTED, smoke: SUCCEEDED_SMOKE }) }))
    const fact = within(document.querySelector('[data-step="team"]') as HTMLElement).getByText('Smoke agent').closest('li') as HTMLElement
    // MUTATION: compare against `passed`, a word the route never writes.
    expect(fact.className).toContain('is-done')
    expect(handOver()).not.toBeNull()
  })

  it('a failed smoke shows its cause and a Retry that posts the smoke route', async () => {
    const calls = serve((m, url) =>
      m === 'POST' && url === '/v1/admin/setup/smoke'
        ? { status: 200, body: { team: 'engineering', workspace: 'engineering', smoke: { state: 'running', task_id: 'task_again' } } }
        : m === 'GET' && url === '/v1/admin/setup'
          ? { status: 200, body: state() }
          : null,
    )
    await mountSteps(state({ finished: null, ...teamWith({ grant: GRANTED, smoke: { state: 'failed', reason: 'quota_exceeded' } }) }))
    const row = document.querySelector('[data-step="team"]') as HTMLElement
    expect(within(row).getByText('Smoke agent').closest('li')?.className).toContain('is-failed')
    expect(visible(row)).toContain('quota_exceeded')
    // MUTATION: leave a failed smoke with no way to run it again.
    fireEvent.click(within(row).getByRole('button', { name: 'Retry the smoke' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url === '/v1/admin/setup/smoke')).toBe(true))
    // MUTATION: offer the hand-over while the smoke has not passed (the route answers 409).
    expect(handOver()).toBeNull()
  })

  it('a one-person failed smoke retries through the same route', async () => {
    const calls = serve((m, url) => (m === 'POST' && url === '/v1/admin/setup/smoke' ? { status: 200, body: { team: null, workspace: 'u-ana', smoke: { state: 'running' } } } : null))
    await mountSteps(onePerson({ smoke: { state: 'failed', reason: 'no_capacity' } }))
    fireEvent.click(within(document.querySelector('[data-step="team"]') as HTMLElement).getByRole('button', { name: 'Retry the smoke' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url === '/v1/admin/setup/smoke')).toBe(true))
    expect(handOver()).toBeNull()
  })

  it('no hand-over while the smoke is running', async () => {
    await mountSteps(state({ finished: null, ...teamWith({ grant: GRANTED, smoke: { state: 'running', task_id: 'task_smoke' } }) }))
    expect(handOver()).toBeNull()
  })
})

describe('the team form and the group (finding 27)', () => {
  it('links Google Admin’s groups page beside the group field', async () => {
    await mountSteps(state({ teams: [], accounts: {}, finished: null }))
    const row = document.querySelector('[data-step="team"]') as HTMLElement
    // MUTATION: drop the link, or point it anywhere else.
    const link = within(row).getByRole('link', { name: /Create group/ })
    expect(link.getAttribute('href')).toBe('https://admin.google.com/ac/groups')
    expect(link.getAttribute('target')).toBe('_blank')
  })

  it('shows the route’s refusal and the page that fixes it when the installer is not in the group', async () => {
    const message = 'ana@example.com is not a member of eng@example.com: add them to it in Google Admin (https://admin.google.com/ac/groups), then try again'
    serve((m, url) =>
      m === 'POST' && url === '/v1/admin/setup/teams'
        ? {
            status: 409,
            body: { code: 'conflict', message, detail: { reason: 'installer_not_member', group: 'eng@example.com', admin_groups_url: 'https://admin.google.com/ac/groups', add_members_url: MEMBERS_URL } },
          }
        : null,
    )
    await mountSteps(state({ teams: [], accounts: {}, finished: null }))
    const row = document.querySelector('[data-step="team"]') as HTMLElement
    fireEvent.change(within(row).getByLabelText('Workspace group'), { target: { value: 'eng@example.com' } })
    fireEvent.click(within(row).getByRole('button', { name: 'Create the team' }))
    // MUTATION: swallow the route's reason.
    await waitFor(() => expect(visible(row)).toContain('is not a member of eng@example.com'))
    // MUTATION: leave the person to find the members page.
    const add = within(row).getByRole('link', { name: /Add yourself to the group/ })
    expect(add.getAttribute('href')).toBe(MEMBERS_URL)
  })
})

describe('the App self-check waits for infra pass 3 (finding 28)', () => {
  const installed = (self_check: Json | null): Json => ({ github_app: { ...(state().github_app as Json), self_check } })

  it('is disabled, with the reason, until stage 9 reports pass 3', async () => {
    await mountSteps(state(installed(null)))
    const row = document.querySelector('[data-step="github"]') as HTMLElement
    const button = within(row).getByRole('button', { name: 'Run the self-check' }) as HTMLButtonElement
    // MUTATION: offer the check before pass 3 gives swarm-api the App's id.
    expect(button.disabled).toBe(true)
    expect(visible(row)).toContain('infra pass 3')
    cleanup()
    const calls = serve((m, url) => (m === 'POST' && url === '/v1/admin/setup/github-app/self-check' ? { status: 200, body: { ok: false, reason: 'app_id_not_configured', at: null, state: 'waiting_for_infra_pass_3' } } : null))
    await mountSteps(state(installed(null), { stages: (state().engine as Json & { stages: unknown[] }).stages.map((s, i) => (i === 9 ? { state: 'done', at: '2026-10-29T14:55:00Z', by: 'ana@example.com' } : s)) }))
    // Once pass 3 has run the page runs the check by itself (audit 4d, F7), and the button stays as the way to run it again.
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url === '/v1/admin/setup/github-app/self-check')).toBe(true))
    const after = within(document.querySelector('[data-step="github"]') as HTMLElement).getByRole('button', { name: 'Run the self-check' }) as HTMLButtonElement
    // MUTATION: keep it disabled once pass 3 has run.
    await waitFor(() => expect(after.disabled).toBe(false))
  })

  it('says why it waits in a few words beside the button, leaving the sentence to the row', async () => {
    await mountSteps(state(installed(null)))
    const why = (document.querySelector('[data-step="github"]') as HTMLElement).querySelector('.c-why') as HTMLElement
    // MUTATION: put the full sentence beside the button: the action column widens and squeezes the row's body.
    expect(visible(why).length).toBeLessThanOrEqual(24)
    expect(visible(why)).toContain('infra pass 3')
  })

  it('reads app_id_not_configured as waiting for swarm install’s infra pass 3, not as a failure', async () => {
    await mountSteps(state(installed({ ok: false, reason: 'app_id_not_configured', at: '2026-10-29T14:52:00Z', state: 'waiting_for_infra_pass_3' })))
    const row = document.querySelector('[data-step="github"]') as HTMLElement
    // MUTATION: draw the wait as a failed step.
    expect(row.className).not.toContain('is-failed')
    expect(visible(row)).toContain('waiting for swarm install’s infra pass 3')
    // MUTATION: send the person to create the App again.
    expect(visible(row)).not.toContain('create the App again')
    cleanup()
    // A check recorded before the route served `state` is read by its reason.
    await mountSteps(state(installed({ ok: false, reason: 'app_id_not_configured', at: '2026-10-29T14:52:00Z' })))
    expect(document.querySelector('[data-step="github"]')?.className).not.toContain('is-failed')
  })
})

describe('the first team needs no typing the install already knows (#26, N20; DAYONE-SETUP-UI-6)', () => {
  const noTeam = (engine: Json = {}): Json => state({ teams: [], accounts: {}, finished: null }, engine)

  it('createSetupTeam keeps the invites and their Google Admin members page the team route returns', async () => {
    serve((m, url) =>
      m === 'POST' && url === '/v1/admin/setup/teams'
        ? {
            status: 201,
            body: {
              team: 'engineering',
              invites: [
                { email: 'ravi@example.com', add_members_url: MEMBERS_URL, member: false },
                { email: 'lea@example.com', add_members_url: 'https://evil.example.com/members', member: true },
              ],
            },
          }
        : null,
    )
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { createSetupTeam } = await import('../api')
    const r = await createSetupTeam({ group: 'engineering@example.com', invites: ['ravi@example.com', 'lea@example.com'] })
    expect(r.status).toBe('ok')
    const data = (r as { data: unknown }).data
    // MUTATION: keep only `team` and drop the members page the route answered.
    expect(data).toEqual({
      team: 'engineering',
      invites: [
        { email: 'ravi@example.com', add_members_url: MEMBERS_URL, member: false },
        // MUTATION: pass through a page that is not Google Admin's.
        { email: 'lea@example.com', add_members_url: null, member: true },
      ],
    })
  })

  it('creating the team with invites opens the group’s members page, as the form’s hint says', async () => {
    const tabs = fakeTabs()
    serve((m, url) =>
      m === 'POST' && url === '/v1/admin/setup/teams'
        ? { status: 201, body: { team: 'eng', invites: [{ email: 'kim@example.com', add_members_url: MEMBERS_URL, member: false }] } }
        : m === 'GET' && url === '/v1/admin/setup'
          ? { status: 200, body: noTeam() }
          : null,
    )
    await mountSteps(noTeam())
    const row = document.querySelector('[data-step="team"]') as HTMLElement
    // MUTATION: promise the page "opens from the team's row", where no button appears before an invite there.
    expect(visible(row)).not.toContain('opens from the team’s row')
    expect(visible(row)).toContain('opens when you create the team')
    fireEvent.change(within(row).getByLabelText('Workspace group'), { target: { value: 'eng@example.com' } })
    fireEvent.change(within(row).getByLabelText('Invite'), { target: { value: 'kim@example.com' } })
    fireEvent.click(within(row).getByRole('button', { name: 'Create the team' }))
    // MUTATION: open the tab after the fetch resolves, outside the click (audit 5b, M5).
    expect(tabs.opens).toEqual([''])
    // MUTATION: drop the members page the route answered.
    await waitFor(() => expect(tabs.tabs[0]?.location.href).toBe(MEMBERS_URL))
    expect(tabs.opens).toEqual([''])
    tabs.restore()
  })

  it('pre-fills the group field with the team group swarm install created', async () => {
    const calls = serve((m, url) =>
      m === 'POST' && url === '/v1/admin/setup/teams' ? { status: 201, body: { team: 'swarm-team', invites: [] } } : m === 'GET' && url === '/v1/admin/setup' ? { status: 200, body: noTeam() } : null,
    )
    await mountSteps(noTeam({ workspace_groups: { team: 'swarm-team@example.com' } }))
    const row = document.querySelector('[data-step="team"]') as HTMLElement
    const input = within(row).getByLabelText('Workspace group') as HTMLInputElement
    // MUTATION: start the field empty, asking for what the install already made.
    expect(input.value).toBe('swarm-team@example.com')
    expect(visible(row)).toContain('swarm install created')
    fireEvent.click(within(row).getByRole('button', { name: 'Create the team' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url === '/v1/admin/setup/teams')).toBe(true))
    expect((calls.find((c) => c.url === '/v1/admin/setup/teams')?.body as Json).group).toBe('swarm-team@example.com')
    // The person can still change it.
    cleanup()
    await mountSteps(noTeam({ workspace_groups: { team: 'swarm-team@example.com' } }))
    const again = within(document.querySelector('[data-step="team"]') as HTMLElement).getByLabelText('Workspace group') as HTMLInputElement
    fireEvent.change(again, { target: { value: 'eng@example.com' } })
    expect(again.value).toBe('eng@example.com')
    cleanup()
    // Before stage 8 records a group, the field starts empty.
    await mountSteps(noTeam())
    expect((within(document.querySelector('[data-step="team"]') as HTMLElement).getByLabelText('Workspace group') as HTMLInputElement).value).toBe('')
  })
})

// ADMIN › SETUP FOLLOWS THE ENGINE (DAYONE-SETUP-UI-7; part of #2688; audit 3).
//
// WHAT EACH CASE HOLDS:
//   * N10: the plugin line installs from the marketplace of the deployment's
//     own release channel (an rc release's `channels/rc.marketplace.json`);
//   * A7: the engine's epoch-seconds times read as dates, ISO text still
//     reads, and no field renders Invalid Date;
//   * A8: a failed or cancelled console run offers "Run the console checks
//     again" with the fresh starts the route has left, and none past them.

const FAILED_RUNS = (retries: Json = {}): Json => ({
  finished: null,
  ...teamWith({ grant: GRANTED }),
  verify_runs: { verify_runs: { console_task: 'task_f1', console_workflow: 'wf_f1' }, retries, repository: 'example-org/app' },
})

describe('Admin › Setup follows the engine', () => {
  it('the plugin line installs from the marketplace of the deployment’s channel', async () => {
    const passed = { verify: { ...VERIFYING, done: 8, result: 'passed' } }
    await mountSteps(state({}, { ...passed, release: '2026.10.3-rc.2' }))
    const rc = visible(screen.getByRole('region', { name: 'Send to your team' }).querySelector('figure.c-code pre'))
    // MUTATION: install every deployment from the stable marketplace.
    expect(rc).toContain('claude plugin marketplace add https://storage.googleapis.com/swarmcloud/channels/rc.marketplace.json ')
    cleanup()
    await mountSteps(state({}, { ...passed, release: '2026.10.3' }))
    const stable = visible(screen.getByRole('region', { name: 'Send to your team' }).querySelector('figure.c-code pre'))
    // MUTATION: send a stable deployment to a channel file.
    expect(stable).toContain('claude plugin marketplace add https://storage.googleapis.com/swarmcloud/marketplace.json ')
    const mod = await import('../AdminSetupSteps')
    expect(mod.releaseChannel(null)).toBe('stable')
    expect(mod.pluginMarketplace('beta')).toBe('https://storage.googleapis.com/swarmcloud/channels/beta.marketplace.json')
  })

  it('reads the engine’s epoch-seconds times as dates, and ISO text as before', async () => {
    const { normSetupState } = await import('../api')
    const at = T0 - 1200
    const doc = state({}, {
      stages: Array.from({ length: 16 }, (_, i) => (i <= 8 || i === 10 || i === 13 ? { state: 'done', at, by: 'ana@example.com' } : null)),
      lease: { holder: 'laptop', heartbeat: T0 + 230 },
      hand_off_at: T0 - 900,
      ready_at: T0 + 60,
    })
    const e = normSetupState(doc).engine
    // MUTATION: keep epoch seconds as text (String(n)), which Date reads as Invalid Date.
    expect(e?.stages[0]?.at).toBe(new Date(at * 1000).toISOString())
    expect(e?.lease?.heartbeat).toBe(new Date((T0 + 230) * 1000).toISOString())
    expect(e?.hand_off_at).toBe(new Date((T0 - 900) * 1000).toISOString())
    expect(e?.ready_at).toBe(new Date((T0 + 60) * 1000).toISOString())
    // ISO text reads unchanged; text that is no time is null, never Invalid Date.
    expect(normSetupState(state()).engine?.hand_off_at).toBe('2026-10-29T14:45:00Z')
    expect(normSetupState(state({}, { ready_at: 'soon' })).engine?.ready_at).toBeNull()
    await mountSteps(doc)
    const text = visible(document.body)
    expect(text).not.toContain('Invalid Date')
    expect(visible(document.querySelector('[data-step="cloud"]'))).toContain(`finished ${new Date(at * 1000).toLocaleString()}`)
  })

  it('a failed console run is started again by swarm-api, with the fresh starts left', async () => {
    const calls = serve((m, url) => {
      if (m === 'GET' && url.startsWith('/v1/tasks/task_f1')) return { status: 200, body: { task: { id: 'task_f1', tenant_id: 'engineering', state: 'CANCELLED' } } }
      if (m === 'GET' && url.startsWith('/v1/workflows/wf_f1')) return { status: 200, body: { workflow: { id: 'wf_f1', state: 'SUCCEEDED' }, tasks: [] } }
      return null
    })
    await mountSteps(state({ ...FAILED_RUNS(), finished: { at: '2026-10-29T14:58:00Z', by: 'ana@example.com' } }))
    const row = document.querySelector('[data-step="verify"]') as HTMLElement
    // MUTATION: drop the count, or count from anything but the route's `retries`.
    await waitFor(() => expect(visible(row)).toContain('swarm-api starts it again on this page’s next read: 2 of 2 fresh starts left'))
    // MUTATION: ask the person to press a button the status route's read makes needless.
    expect(within(row).queryByRole('button', { name: /Run the console checks/ })).toBeNull()
    expect(calls.some((c) => c.method === 'POST' && c.url === '/v1/admin/setup/verify-runs')).toBe(false)
  })

  it('a console run that used its fresh starts is not offered again', async () => {
    serve((m, url) =>
      m === 'GET' && url.startsWith('/v1/tasks/task_f1')
        ? { status: 200, body: { task: { id: 'task_f1', tenant_id: 'engineering', state: 'RUNNING' } } }
        : m === 'GET' && url.startsWith('/v1/workflows/wf_f1')
          ? { status: 200, body: { workflow: { id: 'wf_f1', state: 'FAILED' }, tasks: [] } }
          : null,
    )
    await mountSteps(state(FAILED_RUNS({ console_task: 0, console_workflow: 2 })))
    const row = document.querySelector('[data-step="verify"]') as HTMLElement
    const again = await within(row).findByRole('button', { name: 'Run the console checks again' })
    // MUTATION: start a run the route answers 409 for.
    expect((again as HTMLButtonElement).disabled).toBe(true)
    expect(visible(row)).toContain('0 of 2 fresh starts left')
    expect(visible(row)).toContain('failed after 2 fresh starts')
  })

  it('a run still going or succeeded is not offered again', async () => {
    serve((m, url) =>
      m === 'GET' && url.startsWith('/v1/tasks/task_f1')
        ? { status: 200, body: { task: { id: 'task_f1', tenant_id: 'engineering', state: 'RUNNING' } } }
        : m === 'GET' && url.startsWith('/v1/workflows/wf_f1')
          ? { status: 200, body: { workflow: { id: 'wf_f1', state: 'SUCCEEDED' }, tasks: [] } }
          : null,
    )
    await mountSteps(state(FAILED_RUNS()))
    const row = document.querySelector('[data-step="verify"]') as HTMLElement
    await waitFor(() => expect(visible(row)).toContain('succeeded'))
    await waitFor(() => expect(visible(row)).toContain('running'))
    // MUTATION: offer the button on any recorded run.
    expect(within(row).queryByRole('button', { name: /Run the console checks/ })).toBeNull()
  })
})

// DAYONE-SETUP-UI-8 (part of #2688; audit 4d, F5, F6 and F7):
//   * F7: once infra pass 3 has run, the page runs the App's self-check by
//     itself (pass 4 and the hand-off wait on it); before pass 3, or once a
//     check has passed or failed, it runs none;
//   * F5: the pool account's owner is filled in with the installer; the
//     installer's own sign-in opens in a tab of its own, never as a link to
//     copy, and a link for someone else is "made", not "sent";
//   * F6: no link is made before the workspace it signs into is `ready`.

describe('the App self-check runs by itself once infra pass 3 has run (audit 4d, F7)', () => {
  const PASS_3 = (state().engine as Json & { stages: unknown[] }).stages.map((s, i) => (i === 9 ? { state: 'switches_on', at: '2026-10-29T14:55:00Z', by: 'ana@example.com' } : s))
  const installed = (self_check: Json | null): Json => ({ github_app: { ...(state().github_app as Json), self_check } })
  const selfChecks = (calls: { method: string; url: string }[]) => calls.filter((c) => c.method === 'POST' && c.url === '/v1/admin/setup/github-app/self-check')

  it('posts the self-check with no click when pass 3 has run and no check is recorded', async () => {
    const calls = serve((m, url) =>
      m === 'POST' && url === '/v1/admin/setup/github-app/self-check' ? { status: 200, body: { ok: true, reason: null, at: '2026-10-29T15:04:00Z', state: 'passed' } } : null,
    )
    await mountSteps(state(installed(null), { stages: PASS_3 }))
    // MUTATION: leave the check to the "Run the self-check" button -> the hand-off waits on a click.
    await waitFor(() => expect(selfChecks(calls)).toHaveLength(1))
    expect(visible(document.querySelector('[data-step="github"]'))).not.toContain('the self-check is next')
  })

  it('also runs a check still recorded as waiting for pass 3, once', async () => {
    const calls = serve((m, url) =>
      m === 'POST' && url === '/v1/admin/setup/github-app/self-check' ? { status: 200, body: { ok: false, reason: 'app_id_not_configured', at: null, state: 'waiting_for_infra_pass_3' } } : null,
    )
    const { unmount } = await mountSteps(state(installed({ ok: false, reason: 'app_id_not_configured', at: '2026-10-29T14:52:00Z', state: 'waiting_for_infra_pass_3' }), { stages: PASS_3 }))
    await waitFor(() => expect(selfChecks(calls)).toHaveLength(1))
    // MUTATION: re-run on every render -> the check is posted in a loop.
    await new Promise((r) => setTimeout(r, 50))
    expect(selfChecks(calls)).toHaveLength(1)
    unmount()
  })

  it('runs none before pass 3, after a pass, or after a failure', async () => {
    const calls = serve(() => null)
    await mountSteps(state(installed(null)))
    cleanup()
    await mountSteps(state(installed({ ok: true, reason: null, at: '2026-10-29T14:52:00Z' }), { stages: PASS_3 }))
    cleanup()
    await mountSteps(state(installed({ ok: false, reason: 'the App key did not sign', at: '2026-10-29T14:52:00Z' }), { stages: PASS_3 }))
    await new Promise((r) => setTimeout(r, 50))
    // MUTATION: run it before pass 3 gives swarm-api the App's id, or again over a recorded result.
    expect(selfChecks(calls)).toHaveLength(0)
  })

  it('selfCheckDue reads the same four facts', async () => {
    const { selfCheckDue } = await import('../AdminSetupSteps')
    const { normSetupState } = await import('../api')
    expect(selfCheckDue(normSetupState(state(installed(null), { stages: PASS_3 })))).toBe(true)
    expect(selfCheckDue(normSetupState(state(installed(null))))).toBe(false)
    expect(selfCheckDue(normSetupState(state({ github_app: { ...(state().github_app as Json), installed_on: null, self_check: null } }, { stages: PASS_3 })))).toBe(false)
  })
})

describe('the installer adds their own Claude account without copying a link (audit 4d, F5)', () => {
  const linkFor = (owner: string, label: string): Json => {
    const token = rand(43)
    return { kind: 'pool-account', label, owner, url: `https://${HOST}/admin/setup/link/${token}`, path: `/admin/setup/link/${token}`, expires_at: '2026-10-30T15:00:00Z' }
  }
  const fakeTab = () => ({ location: { href: '' }, close: vi.fn() })

  it('one person: the owner is the installer, fixed, and the sign-in opens in its own tab', async () => {
    const { OWN_SIGN_IN_WINDOW } = await import('../SetupLink')
    const link = linkFor('ana@example.com', 'ana')
    const tab = fakeTab()
    const open = vi.spyOn(window, 'open').mockImplementation(() => tab as unknown as Window)
    const calls = serve((m, url) => (m === 'POST' && url === '/v1/admin/setup/links' ? { status: 200, body: link } : m === 'GET' && url === '/v1/admin/setup' ? { status: 200, body: onePerson({}, { accounts: {} }) } : null))
    await mountSteps(onePerson({}, { accounts: {} }))
    const row = document.querySelector('[data-step="accounts"]') as HTMLElement
    const owner = within(row).getByLabelText(/^Its owner/) as HTMLInputElement
    // MUTATION: leave the owner empty -> the installer types their own address.
    expect(owner.value).toBe('ana@example.com')
    // MUTATION: let one person name another owner the route refuses.
    expect(owner.readOnly).toBe(true)
    fireEvent.change(within(row).getByLabelText('Account label'), { target: { value: 'ana' } })
    // MUTATION: keep "Send a sign-in link" -> a send that sends nothing.
    expect(within(row).queryByRole('button', { name: /Send/ })).toBeNull()
    fireEvent.click(within(row).getByRole('button', { name: 'Sign in my Claude account' }))
    await waitFor(() => expect(tab.location.href).toBe(link.url))
    // The tab is opened inside the click (no popup blocker) and named so the link page opens the link at once.
    expect(open).toHaveBeenCalledWith('', OWN_SIGN_IN_WINDOW)
    expect(calls.find((c) => c.url === '/v1/admin/setup/links')?.body).toEqual({ kind: 'pool-account', label: 'ana', owner: 'ana@example.com' })
    // MUTATION: show the installer their own link to copy.
    expect(row.querySelector('figure.c-code')).toBeNull()
    expect(visible(row)).not.toContain(String(link.path))
    expect(visible(row)).toContain('opened in a new tab')
    open.mockRestore()
  })

  it('a team: the owner is the signed-in admin; another owner gets a link made to send them', async () => {
    const tab = fakeTab()
    const open = vi.spyOn(window, 'open').mockImplementation(() => tab as unknown as Window)
    const ravi = linkFor('ravi@example.com', 'ravi')
    const calls = serve((m, url) =>
      m === 'GET' && url === '/v1/tenants/me'
        ? { status: 200, body: { tenant: { tenant_id: 'u-ana', kind: 'user', display_name: 'ana@example.com' }, principal: { email: 'ana@example.com', domain: 'example.com', groups: [], is_admin: true }, environment: 'prod', environment_declared: true } }
        : m === 'POST' && url === '/v1/admin/setup/links'
          ? { status: 200, body: ravi }
          : m === 'GET' && url === '/v1/admin/setup'
            ? { status: 200, body: state() }
            : null,
    )
    await mountSteps(state({ accounts: {}, finished: null }))
    const row = document.querySelector('[data-step="accounts"]') as HTMLElement
    const owner = within(row).getByLabelText(/^Its owner/) as HTMLInputElement
    // MUTATION: leave the owner empty in team mode -> the installer types their own address.
    await waitFor(() => expect(owner.value).toBe('ana@example.com'))
    expect(within(row).getByRole('button', { name: 'Sign in my Claude account' })).toBeTruthy()
    fireEvent.change(owner, { target: { value: 'ravi@example.com' } })
    fireEvent.change(within(row).getByLabelText('Account label'), { target: { value: 'ravi' } })
    // MUTATION: keep "Send" for a link this page only makes.
    fireEvent.click(within(row).getByRole('button', { name: 'Make a sign-in link' }))
    await waitFor(() => expect(row.querySelector('figure.c-code')).not.toBeNull())
    expect(visible(row)).toContain('One-time link for ravi@example.com, shown once: send it to them')
    // Someone else's link is never opened in this browser.
    expect(open).not.toHaveBeenCalled()
    expect(calls.find((c) => c.url === '/v1/admin/setup/links')?.body).toEqual({ kind: 'pool-account', label: 'ravi', owner: 'ravi@example.com' })
    open.mockRestore()
  })
})

describe('no sign-in link is made before its workspace is ready (audit 4d, F6)', () => {
  it('one person: waits for the personal workspace, then offers the sign-in', async () => {
    const calls = serve(() => null)
    await mountSteps(onePerson({ workspace_state: 'applying' }, { accounts: {} }))
    const row = document.querySelector('[data-step="accounts"]') as HTMLElement
    fireEvent.change(within(row).getByLabelText('Account label'), { target: { value: 'ana' } })
    const button = within(row).getByRole('button', { name: 'Sign in my Claude account' }) as HTMLButtonElement
    // MUTATION: offer the link while swarm-workspace-apply runs -> the link is spent on a 403.
    expect(button.disabled).toBe(true)
    expect(visible(row)).toContain('Waiting for the workspace u-ana (swarm-workspace-apply: applying)')
    fireEvent.click(button)
    await new Promise((r) => setTimeout(r, 20))
    expect(calls.some((c) => c.url === '/v1/admin/setup/links')).toBe(false)
    cleanup()
    await mountSteps(onePerson({}, { accounts: {} }))
    const ready = document.querySelector('[data-step="accounts"]') as HTMLElement
    fireEvent.change(within(ready).getByLabelText('Account label'), { target: { value: 'ana' } })
    expect((within(ready).getByRole('button', { name: 'Sign in my Claude account' }) as HTMLButtonElement).disabled).toBe(false)
    expect(visible(ready)).not.toContain('Waiting for the workspace')
  })

  it('a team: waits for the first team’s workspace', async () => {
    await mountSteps(state({ teams: [{ ...(state().teams as Json[])[0], progress: { workspace_state: 'pending' } }], accounts: {}, finished: null }))
    const row = document.querySelector('[data-step="accounts"]') as HTMLElement
    fireEvent.change(within(row).getByLabelText('Account label'), { target: { value: 'ravi' } })
    fireEvent.change(within(row).getByLabelText(/^Its owner/), { target: { value: 'ravi@example.com' } })
    expect((within(row).getByRole('button', { name: 'Make a sign-in link' }) as HTMLButtonElement).disabled).toBe(true)
    expect(visible(row)).toContain('Waiting for the workspace engineering (swarm-workspace-apply: pending)')
  })
})

// NO WORKSPACE BEFORE PUBLISHING (DAYONE-MANIFEST-STEPS-2; part of #2688).
//
// WHAT EACH CASE HOLDS: until swarm install's infra pass 3 turns workspace
// publishing on, an approved workspace would only be logged workspace_stuck
// (reason publishing_off), so the route says the first team waits and the
// form offers no "Create the team"; for one person the route files no
// personal workspace and the row says what it waits for.

describe('the first workspace waits for workspace publishing', () => {
  const WAITS = 'waiting for swarm install’s infra pass 3, which turns on workspace publishing'
  const waiting = (): Json =>
    state({ teams: [], accounts: {}, finished: null, steps: { first_team: { needed: true, state: 'waiting', reason: WAITS }, invites: { needed: true, state: 'to_do' } } })

  it('offers no Create the team until the route says publishing is on', async () => {
    const calls = serve(() => null)
    await mountSteps(waiting())
    const row = document.querySelector('[data-step="team"]') as HTMLElement
    fireEvent.change(within(row).getByLabelText('Workspace group'), { target: { value: 'eng@example.com' } })
    const button = within(row).getByRole('button', { name: 'Create the team' }) as HTMLButtonElement
    // MUTATION: approve a workspace infra pass 3 has not turned publishing on for.
    expect(button.disabled).toBe(true)
    fireEvent.click(button)
    expect(calls.some((c) => c.method === 'POST' && c.url === '/v1/admin/setup/teams')).toBe(false)
    expect(visible(row)).toContain('infra pass 3')
    expect(row.className).not.toContain('is-done')
    cleanup()
    await mountSteps(state({ teams: [], accounts: {}, finished: null, steps: { first_team: { needed: true, state: 'to_do' }, invites: { needed: true, state: 'to_do' } } }))
    const open = within(document.querySelector('[data-step="team"]') as HTMLElement)
    fireEvent.change(open.getByLabelText('Workspace group'), { target: { value: 'eng@example.com' } })
    // MUTATION: keep the form shut once publishing is on.
    expect((open.getByRole('button', { name: 'Create the team' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('says a one-person install’s personal workspace waits for infra pass 3', async () => {
    await mountSteps(onePerson({ workspace: null, workspace_state: null, grant: null, smoke: null, pool_accounts: [], reason: 'publishing_off' }))
    const row = document.querySelector('[data-step="team"]') as HTMLElement
    expect(row.className).not.toContain('is-done')
    // MUTATION: show the bare code, leaving the person to guess what it waits for.
    expect(visible(row)).toContain('infra pass 3')
    expect(visible(row)).not.toContain('publishing_off')
  })
})
