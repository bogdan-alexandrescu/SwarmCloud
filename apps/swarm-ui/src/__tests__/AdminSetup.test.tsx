// ADMIN › SETUP, THE PAGE (DAYONE-SETUP-UI-2; part of #2688; plan §5.1,
// frames B05, B06, B09a, B15).
//
// WHAT EACH CASE HOLDS:
//   * a first run, with swarm install's cloud stages done and nothing done
//     on this page, draws the strip with the CLI's step done and every
//     console step pending, in BGD's order (GitHub App, first team, Claude
//     accounts, verify, Send to your team), and the lead names the next one;
//   * when no CLI holds the install lease (`cli_running` false, R8) the page
//     says to run swarm install in a terminal to continue, and the console
//     steps stay usable;
//   * once the first team exists, Add a team is the same team panel as its
//     own flow, with the `swarm team add` line beside it, following the form,
//     and its submit is the setup API's team route (B15).
//
// The page reads `GET /v1/admin/setup` through the real API client
// (`loadSetup`), against a stubbed fetch: no network, no credentials.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>

const T0 = Date.parse('2026-10-29T15:00:00Z')
const HOST = 'swarm.example.com'
const CLI_STAGES = [0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 13]

/** swarm install has handed over: its cloud stages are done and nothing on this page is. */
function firstRun(over: Json = {}): Json {
  return {
    env: 'prod',
    engine: {
      release: 'v1.0.0',
      hostname: HOST,
      stages: Array.from({ length: 16 }, (_, i) => (CLI_STAGES.includes(i) ? { state: 'done', at: '2026-10-29T14:40:00Z', by: 'ana@example.com' } : null)),
      lease: { holder: 'laptop', heartbeat: new Date(T0 - 10_000).toISOString(), expires: null },
      verify: null,
      hand_off_at: '2026-10-29T14:45:00Z',
      ready_at: null,
    },
    cli_running: true,
    github_app: null,
    teams: [],
    accounts: {},
    finished: null,
    addons: [],
    hostname: HOST,
    github_org: 'example-org',
    ...over,
  }
}

const FIRST_TEAM = {
  id: 'engineering',
  group: 'engineering@example.com',
  workspace: 'engineering',
  first_admin: 'ana@example.com',
  invites: ['ravi@example.com'],
  progress: { workspace_state: 'ready' },
  smoke: { state: 'passed', detail: 'green in 2 min 48 s', url: null },
}

async function mountPage(doc: Json) {
  const calls = serve((m, url) => {
    if (m === 'GET' && url === '/v1/admin/setup') return { status: 200, body: doc }
    if (m === 'POST' && url === '/v1/admin/setup/teams') return { status: 200, body: { team: 'research' } }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const mod = await import('../AdminSetup')
  render(<mod.AdminSetupScreen now={T0} />)
  await waitFor(() => expect(document.querySelector('[aria-label="Install setup"]')).not.toBeNull())
  return calls
}

const strip = () => document.querySelector('[aria-label="Install setup"]') as HTMLElement
const stripStep = (key: string) => strip().querySelector(`[data-step="${key}"]`) as HTMLElement
const row = (key: string) => document.querySelector(`ol[aria-label="Setup steps"] > [data-step="${key}"]`) as HTMLElement

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
})

describe('Admin › Setup, the page', () => {
  it("first run shows the CLI's stages done and the console's steps pending", async () => {
    await mountPage(firstRun())
    // The strip, in BGD's order: the CLI's part, then the console's.
    // MUTATION: draw the accounts before the first team (the P1-B frames' order) -> fails.
    expect([...strip().querySelectorAll('[data-step]')].map((s) => s.getAttribute('data-step'))).toEqual(['cloud', 'github', 'team', 'accounts', 'verify', 'send'])
    // MUTATION: read the cloud step off anything but the engine's stages -> fails.
    expect(stripStep('cloud').className).toContain('is-done')
    expect(visible(stripStep('cloud'))).toMatch(/swarm install.*11 of 11 stages/)
    for (const k of ['github', 'team', 'accounts', 'verify', 'send']) expect(stripStep(k).className).not.toContain('is-done')
    // The next step is the GitHub App's, and the strip and the lead say so.
    expect(stripStep('github').className).toContain('is-next')
    expect(visible(strip())).toMatch(/1 of 6 done/)
    const lead = document.querySelector('.ws-lead') as HTMLElement
    expect(visible(lead)).toMatch(/Step 2 of 6/)
    expect(visible(lead)).toMatch(/GitHub App/)
    expect(visible(lead)).toMatch(/swarm install did the cloud part/)
    // The checklist SETUP-UI-1 built, composed here: the cloud row done, the console's to do.
    expect(row('cloud').className).toContain('is-done')
    for (const k of ['github', 'team', 'accounts']) expect(row(k).className).toContain('is-todo')
    expect(row('send').className).toContain('is-todo')
    // And the installer card beside it.
    expect(screen.getByText('The installer')).toBeTruthy()
  })

  it('when no CLI holds the lease the page says run swarm install to continue', async () => {
    await mountPage(firstRun({ cli_running: false }))
    // MUTATION: drop the R8 banner, or read `cli_running` as true -> fails.
    expect(screen.getByText('swarm install is not running')).toBeTruthy()
    expect(visible(document.body)).toMatch(/Run swarm install in a terminal to continue/)
    // The strip's count says what it is waiting on, rather than "waiting on this page".
    expect(visible(strip())).toMatch(/swarm install is not running/)
    expect(visible(strip())).not.toMatch(/the CLI is waiting on this page/)
    // The console steps stay usable: the GitHub App's button is live.
    const create = within(row('github')).getByRole('button', { name: /Create the GitHub App/ }) as HTMLButtonElement
    expect(create.disabled).toBe(false)
  })

  it('the second team flow prints the swarm team add line beside it', async () => {
    const calls = await mountPage(firstRun({ teams: [FIRST_TEAM] }))
    const flow = screen.getByRole('region', { name: 'Add a team' })
    const line = () => visible(within(flow).getByLabelText('From a terminal'))
    // Before anything is typed, the line names its one required value.
    expect(line()).toBe('swarm team add <group>')
    fireEvent.change(within(flow).getByLabelText(/Workspace group/), { target: { value: 'research@example.com' } })
    fireEvent.change(within(flow).getByLabelText(/First workspace admin/), { target: { value: 'lea@example.com' } })
    fireEvent.change(within(flow).getByLabelText(/Invite/), { target: { value: 'ravi@example.com, omar@example.com' } })
    // MUTATION: print a fixed line, or one that drops a field the form holds -> fails.
    expect(line()).toBe('swarm team add research@example.com --admin lea@example.com --invite ravi@example.com --invite omar@example.com')
    // The same call the CLI makes: the setup API's team route.
    fireEvent.click(within(flow).getByRole('button', { name: /Add the team/ }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url === '/v1/admin/setup/teams')).toBe(true))
    const post = calls.find((c) => c.url === '/v1/admin/setup/teams')!
    expect(post.body).toEqual({ group: 'research@example.com', first_admin: 'lea@example.com', invites: ['ravi@example.com', 'omar@example.com'] })
    // The teams the deployment has, beside it.
    expect(visible(screen.getByRole('region', { name: 'Teams' }))).toMatch(/engineering/)
  })

  it('a first run draws no second team flow: the first team is the checklist\'s', async () => {
    await mountPage(firstRun())
    // MUTATION: draw Add a team before a first team exists -> two team forms, and this fails.
    expect(screen.queryByRole('region', { name: 'Add a team' })).toBeNull()
  })
})
