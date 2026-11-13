// ADMIN › SETUP, THE PAGE (DAYONE-SETUP-UI-2; part of #2688; plan §5.1,
// frames B05, B06, B09a, B15).
//
// WHAT EACH CASE HOLDS (the page is the console finish the owner picked on
// 2026-11-02, a checklist beside a working panel; its own cases are in
// consoleFinish.test.tsx):
//   * a first run, with swarm install's cloud stages done and nothing done
//     on this page, draws the checklist's eight steps in the owner's order (the
//     sign-in client, GitHub App, first team, Claude accounts, the smoke
//     agent, verify, Send to your team) after the CLI's cloud step, and the
//     panel is on the next step the person can act on;
//   * the sign-in client step (issue #2964, the owner's pick of 2026-11-01)
//     sits after the CLI's cloud part and before the GitHub App, its state
//     read only from the route's `sign_in_client`: waiting is the person's
//     step and the panel opens on it, saved is in progress with swarm
//     install acting, applied is done, and a route that serves none is not
//     read yet, never done;
//   * when no CLI holds the install lease (`cli_running` false, R8) the
//     status line says swarm install is not running and to run it again, and
//     the console steps stay usable;
//   * once the first team exists, Add a team is the same team panel as its
//     own flow, with the `swarm team add` line beside it, following the form,
//     and its submit is the setup API's team route (B15);
//   * NO EXPLANATORY PROSE ON THE SURFACE (the owner's standing rule, 2026-11-05):
//     the panel's kicker is "Step N of 8" with what it follows as its
//     tooltip, each team field's hint is its tooltip and its input's
//     description, and what Add a team and From a terminal do sits behind an
//     (i) beside the heading.
//
// The page reads `GET /v1/admin/setup` through the real API client
// (`loadSetup`), against a stubbed fetch: no network, no credentials.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

/** The text a reader sees: tooltips and hidden descriptions left out. */
function shown(el: Element | null): string {
  if (el === null) return ''
  const copy = el.cloneNode(true) as Element
  for (const off of Array.from(copy.querySelectorAll('[role="tooltip"], [hidden]'))) off.remove()
  return visible(copy)
}

/** The tooltip of the host around `el`. */
const tipOf = (el: Element | null) => visible(el?.closest('.c-tip-host')?.querySelector('[role="tooltip"]') ?? null)

type Json = Record<string, unknown>

const T0 = Date.parse('2026-10-29T15:00:00Z')
const HOST = 'swarm.example.com'
const CLI_STAGES = [0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 13]

/** The route's `sign_in_client` in a given state, with the deployment's three console pages. */
function signInClient(state: 'waiting' | 'saved' | 'applied'): Json {
  const page = (path: string) => `https://console.cloud.google.com/auth/${path}?project=example`
  const saved = state !== 'waiting'
  return {
    state,
    client_id: saved ? 'desktop-example.apps.googleusercontent.com' : null,
    saved_at: saved ? '2026-10-29T14:47:00Z' : null,
    saved_by: saved ? 'ana@example.com' : null,
    links: { branding: page('branding'), audience: page('audience'), create_client: page('clients/create') },
  }
}

/** swarm install has handed over: its cloud stages are done, the sign-in client is applied, and nothing else on this page is. */
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
    sign_in_client: signInClient('applied'),
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

const step = (key: string) => document.querySelector(`nav[aria-label="Setup steps"] [data-step="${key}"]`) as HTMLElement
const keys = () => [...document.querySelectorAll('nav[aria-label="Setup steps"] [data-step]')].map((li) => li.getAttribute('data-step'))
const status = () => visible(screen.getByRole('status', { name: 'What swarm install is doing' }))
const panel = () => document.querySelector('.cfc-panel') as HTMLElement
const pick = (key: string) => fireEvent.click(within(step(key)).getByRole('button'))

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
})

describe('Admin › Setup, the page', () => {
  it("first run shows the CLI's stages done and the console's steps pending", async () => {
    await mountPage(firstRun())
    // The checklist, in the owner's order: the CLI's part, then the console's.
    // MUTATION: draw the accounts before the first team (the P1-B frames' order) -> fails.
    // MUTATION: leave the sign-in client out, or draw it after the GitHub App -> fails.
    expect(keys()).toEqual(['cloud', 'sign-in-client', 'github', 'team', 'accounts', 'smoke', 'verify', 'send'])
    // MUTATION: read the cloud step off anything but the engine's stages -> fails.
    expect(step('cloud').className).toContain('is-done')
    expect(visible(step('cloud'))).toMatch(/11 of 11 stages, by swarm install/)
    expect(step('sign-in-client').className).toContain('is-done')
    for (const k of ['github', 'team', 'accounts', 'smoke', 'verify', 'send']) expect(step(k).className).not.toContain('is-done')
    // The next step is the GitHub App's: the person acts on it, and the panel follows it.
    expect(visible(step('github'))).toContain('You')
    expect(step('github').className).toContain('is-selected')
    expect(status()).toMatch(/2 of 8 done/)
    expect(panel().getAttribute('aria-label')).toBe('Step 3: GitHub App')
    // MUTATION: print what the panel follows beside the step again.
    const kicker = panel().querySelector('.cfc-kicker')
    expect(shown(kicker)).toBe('Step 3 of 8')
    expect(tipOf(kicker?.querySelector('.asm-explained') ?? null)).toBe('The panel follows the next step you can act on.')
    // The panel holds today's GitHub step.
    expect(within(panel()).getByRole('button', { name: /Create the GitHub App/ })).toBeTruthy()
  })

  it('when no CLI holds the lease the page says run swarm install to continue', async () => {
    await mountPage(firstRun({ cli_running: false }))
    // MUTATION: drop the R8 line, or read `cli_running` as true -> fails.
    expect(status()).toMatch(/swarm install is not running · last seen 10 s ago/)
    expect(status()).toMatch(/run swarm install again in a terminal/)
    expect(status()).not.toMatch(/waiting on this page/)
    // The console steps stay usable: the GitHub App's button is live.
    const create = within(panel()).getByRole('button', { name: /Create the GitHub App/ }) as HTMLButtonElement
    expect(create.disabled).toBe(false)
  })

  it('the second team flow prints the swarm team add line beside it', async () => {
    const calls = await mountPage(firstRun({ teams: [FIRST_TEAM] }))
    const flow = screen.getByRole('region', { name: 'Add a team' })
    // The code block's one line, without its line number.
    const line = () => {
      const dl = within(flow).getByRole('group', { name: 'From a terminal' }).querySelector('.c-dl')!
      return [...dl.childNodes].filter((n) => !(n instanceof Element && n.classList.contains('c-ln'))).map((n) => n.textContent).join('')
    }
    // Before anything is typed, the line names its two required values.
    expect(line()).toBe('swarm team add <group> --project=<project-id>')
    fireEvent.change(within(flow).getByLabelText(/Workspace group/), { target: { value: 'research@example.com' } })
    fireEvent.change(within(flow).getByLabelText(/First workspace admin/), { target: { value: 'lea@example.com' } })
    fireEvent.change(within(flow).getByLabelText(/Invite/), { target: { value: 'ravi@example.com, omar@example.com' } })
    // MUTATION: print a fixed line, or one that drops a field the form holds -> fails.
    // Invites are no flag of swarm team add: the line leaves them to the console.
    expect(line()).toBe('swarm team add research@example.com --project=<project-id> --first-admin lea@example.com')
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

  it('the Add a team hint says what an invite does, not a sign-in link nobody sends', async () => {
    await mountPage(firstRun({ teams: [FIRST_TEAM] }))
    const flow = screen.getByRole('region', { name: 'Add a team' })
    // MUTATION: keep "Each gets a sign-in link".
    expect(visible(flow)).not.toContain('sign-in link')
    expect(visible(flow)).toContain('Google Admin')
  })

  it('Add a team keeps its hints and what it does behind tooltips, each still in the page', async () => {
    await mountPage(firstRun({ teams: [FIRST_TEAM] }))
    const flow = screen.getByRole('region', { name: 'Add a team' })
    const hints: [RegExp, string][] = [
      [/Workspace group/, 'A Google Workspace group; its members are the team'],
      [/Workspace name/, 'Shown in the console; the group’s name when left empty'],
      [/First workspace admin/, 'Administers the team’s workspace'],
      [/Invite/, 'Each is added to the group on its members page in Google Admin (swarm-api adds no one); they count as joined once the Groups API sees them'],
    ]
    for (const [label, hint] of hints) {
      // MUTATION: print a field's hint under it again.
      expect(shown(flow)).not.toContain(hint)
      const input = within(flow).getByLabelText(label)
      // The hint is the input's description, and the tooltip over its label and input.
      expect(document.getElementById(input.getAttribute('aria-describedby') ?? '')?.textContent).toBe(hint)
      expect(tipOf(input)).toBe(hint)
    }
    // MUTATION: keep the card's foot, or the terminal card's lead line.
    expect(shown(flow)).not.toContain('The same call swarm team add makes')
    expect(shown(flow)).not.toContain('It follows the form as you type')
    expect(tipOf(within(flow).getByRole('button', { name: 'About Add a team' }))).toBe(
      'The same call swarm team add makes: the team’s record, then swarm-workspace-apply makes its namespace, quota, verify-keys and worker identity.',
    )
    expect(tipOf(within(flow).getByRole('button', { name: 'About From a terminal' }))).toBe('The same steps, from the machine swarm install runs on. It follows the form as you type.')
  })
})

// DAYONE-SETUP-UI-5A (part of #2688; audit finding 12): the strip and the
// lead rest on the route's `mode` and `personal` for one person, and say
// `swarm doctor` only for what that verb does.
describe('Admin › Setup, one person and the doctor', () => {
  const ONE_PERSON = {
    sizing: { mode: 'individual', members: 1, pool_limits: { global: 4 }, service_max_instances: {}, pool_accounts: 1, budget_usd: 150 },
    mode: 'individual',
    steps: { first_team: { needed: false, state: 'not_needed', reason: 'not needed for one person' }, invites: { needed: false, state: 'not_needed', reason: 'not needed for one person' } },
    personal: { owner: 'ana@example.com', workspace: 'u-ana', workspace_state: 'ready', progress: {}, pool_accounts: [] },
    github_app: { started: true, id: 4242, slug: 'swarmcloud-example', installed_on: 'example-org', secrets_written_at: '2026-10-29T14:50:00Z', self_check: { ok: true, reason: null, at: '2026-10-29T14:52:00Z' } },
  }

  it('the checklist counts the personal workspace as the first workspace, and the accounts are next', async () => {
    await mountPage(firstRun(ONE_PERSON))
    // MUTATION: keep the team step on `state.teams`, which one person never has.
    expect(step('team').className).toContain('is-done')
    expect(visible(step('team'))).toContain('Your workspace')
    expect(visible(step('team'))).toContain('u-ana')
    expect(visible(step('accounts'))).not.toContain('after the first team')
    expect(panel().getAttribute('aria-label')).toBe('Step 5: Claude accounts')
    // MUTATION: keep "or sc account add" in the step.
    expect(visible(panel())).not.toContain('sc account add')
  })

  // DAYONE-SETUP-WORDS (part of #2688): the accounts step says the page makes the link; nothing is sent.
  it('the accounts step says the page makes the one-time link', async () => {
    await mountPage(firstRun(ONE_PERSON))
    // MUTATION: say the page sends the link.
    expect(visible(panel())).not.toContain('this page sends')
    expect(visible(panel())).toContain('a one-time link this page makes')
  })

  // The smoke agent is its own step (the owner's pick of 2026-11-02), and the hand-over waits on it.
  const SIGNED_IN = { ana: { owner: 'ana@example.com', link_expires: '2026-10-30T14:54:00Z' } }
  const withSmoke = (smoke: Json) => ({ ...ONE_PERSON, personal: { ...ONE_PERSON.personal, pool_accounts: ['ana'], smoke }, accounts: SIGNED_IN })

  it('a failed smoke is the smoke step failed with its Retry, and no hand over is offered', async () => {
    await mountPage(firstRun(withSmoke({ state: 'failed', reason: 'no_capacity' })))
    // MUTATION: tick the smoke step with the smoke failed.
    expect(step('smoke').className).toContain('is-failed')
    expect(panel().getAttribute('aria-label')).toBe('Step 6: Smoke agent')
    expect(visible(panel())).toContain('no_capacity')
    expect(within(panel()).getByRole('button', { name: 'Retry the smoke agent' })).toBeTruthy()
    pick('verify')
    // MUTATION: offer the hand-over before the smoke succeeded.
    expect(within(panel()).queryByRole('button', { name: /Hand over to swarm install/ })).toBeNull()
  })

  it('a running smoke keeps its step in progress; a succeeded one offers the hand-over', async () => {
    await mountPage(firstRun(withSmoke({ state: 'running', task_id: 'task_smoke' })))
    expect(step('smoke').className).toContain('is-in_progress')
    pick('verify')
    expect(within(panel()).queryByRole('button', { name: /Hand over to swarm install/ })).toBeNull()
    cleanup()
    await mountPage(firstRun(withSmoke({ state: 'succeeded', task_id: 'task_smoke' })))
    expect(step('smoke').className).toContain('is-done')
    expect(panel().getAttribute('aria-label')).toBe('Step 7: Verify')
    expect(within(panel()).getByRole('button', { name: /Hand over to swarm install/ })).toBeTruthy()
  })

  it('a team’s failed smoke keeps the smoke step failed', async () => {
    await mountPage(firstRun({ github_app: ONE_PERSON.github_app, teams: [{ ...FIRST_TEAM, smoke: { state: 'failed', detail: 'quota_exceeded', url: null } }] }))
    // MUTATION: count the smoke done once the team exists, whatever its state.
    expect(step('smoke').className).toContain('is-failed')
    expect(step('verify').className).not.toContain('is-done')
  })

  it('names swarm doctor only for what it does, and the hourly doctor’s findings on Health', async () => {
    await mountPage(firstRun())
    const about = visible(screen.getByRole('region', { name: 'This install' }))
    // MUTATION: say the page shows what swarm doctor runs (Health shows the hourly doctor's run).
    expect(about).not.toContain('The checks swarm doctor runs')
    expect(about).toContain('Admin › Setup › Health')
  })
})

// SETUP-STRIP-SIGNIN (part of #2688; issue #2964, the owner's pick of
// 2026-11-01): the checklist opens with the Sign-in client row, so the strip
// and the lead carry the step too, after the CLI's cloud part and before the
// GitHub App, from the same `sign_in_client` field.
describe('Admin › Setup, the sign-in client step', () => {
  it('while the client waits, the sign-in client is the person’s step and the panel opens on it, not on the GitHub App', async () => {
    await mountPage(firstRun({ sign_in_client: signInClient('waiting') }))
    // MUTATION: leave the step out -> the GitHub App is next and this fails.
    expect(step('sign-in-client').className).not.toContain('is-done')
    expect(step('sign-in-client').className).toContain('is-selected')
    expect(step('github').className).not.toContain('is-selected')
    expect(status()).toMatch(/1 of 8 done/)
    expect(panel().getAttribute('aria-label')).toBe('Step 2: Sign-in client')
    expect(within(panel()).getByRole('list', { name: 'Google pages' })).toBeTruthy()
  })

  it('a saved client is in progress until the infra pass applies it, then done, and the GitHub App is next', async () => {
    await mountPage(firstRun({ sign_in_client: signInClient('saved') }))
    // MUTATION: count a saved client done before IAP holds it -> fails.
    expect(step('sign-in-client').className).toContain('is-in_progress')
    expect(visible(step('sign-in-client'))).toContain('swarm install')
    expect(status()).toMatch(/infra pass/)
    pick('sign-in-client')
    expect(visible(panel())).toContain('desktop-example.apps.googleusercontent.com')
    cleanup()
    await mountPage(firstRun({ sign_in_client: signInClient('applied') }))
    expect(step('sign-in-client').className).toContain('is-done')
    expect(panel().getAttribute('aria-label')).toBe('Step 3: GitHub App')
    pick('sign-in-client')
    expect(visible(panel())).toContain('desktop-example.apps.googleusercontent.com')
  })

  it('a route that serves no sign_in_client is not read yet, never done', async () => {
    await mountPage(firstRun({ sign_in_client: undefined }))
    // MUTATION: read an absent field as done -> fails.
    expect(step('sign-in-client').className).not.toContain('is-done')
    expect(visible(step('sign-in-client'))).toMatch(/not read yet/i)
    cleanup()
    // A state the route never writes is the same.
    await mountPage(firstRun({ sign_in_client: { ...signInClient('applied'), state: 'done' } }))
    expect(step('sign-in-client').className).not.toContain('is-done')
    expect(visible(step('sign-in-client'))).toMatch(/not read yet/i)
  })
})
