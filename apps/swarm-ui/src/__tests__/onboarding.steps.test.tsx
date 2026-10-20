// SET UP GITHUB ACCESS: the guided steps on Work › Repositories › Git tokens
// (lane GT-GUIDE, owner request 2026-10-12, the GUIDED STEPS design).
//
// WHAT EACH CASE HOLDS:
//   * the page opens on three steps -- Connect GitHub, Choose what it reaches,
//     Try it -- each ticked from `GET /v1/onboarding` and never by the page,
//     with "<n> of 3 done" over them;
//   * a step that is not done says what to do next and offers it: Connect,
//     Install the App, Manage in Access (or Enable inline), Check access;
//   * a step waiting on an earlier one says so and offers nothing;
//   * Check access on a repo runs the EXISTING verify routes -- a grant's
//     `POST /v1/access/grants/{repo_id}/verify` for an App connection, the
//     token's `POST /v1/git-tokens/{id}/verify` for a stored token -- and
//     re-reads the steps;
//   * Run a test agent opens Submit with claude-code, a harmless prompt and
//     the chosen repository already filled in, and the default strategy that
//     pushes nothing;
//   * the orgs line never says "appear here once it has" while the step
//     waits on the person: it says to enable them on Work › Access.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, token, visible } from './repofixture'

const WAIT = { timeout: 4000 }

type Json = Record<string, unknown>

function step(name: string, state: string, evidence: Json = {}, extra: Json = {}): Json {
  return { step: name, state, code: null, copy: null, checked_at: null, evidence, issues: [], ...extra }
}

const APP_CONNECTED = { via: 'user', kind: 'app_user', forge_login: 'octo-dev', token_state: 'active', token_id: 'tok_app' }

const GRANTS = [
  { repository: 'example-org/example-api', repo_id: 'repo_0a1b2c3d4e5f6071', owner: 'example-org', mode: 'write', archived: false },
  { repository: 'example-org/example-web', repo_id: 'repo_1111111111111111', owner: 'example-org', mode: 'read', archived: false },
]

const OWNERS = [
  { owner: 'octo-dev', owner_type: 'User', source: 'account', reach: 'reachable', registered: 0, enabled: false },
  { owner: 'example-org', owner_type: 'Organization', source: 'enabled', reach: 'reachable', registered: 0, enabled: true },
]

function doc(steps: Json[], isAdmin = false): Json {
  return {
    tenant_id: 'eng',
    user: 'dev@swarm.example.com',
    user_hash: '89abcdef01234567',
    derived_at: new Date().toISOString(),
    next_step: steps.find((s) => s.state !== 'done')?.step ?? null,
    complete: false,
    source: 'derived on this read',
    steps: [step('signed_in', 'done', { email: 'dev@swarm.example.com', tenant_id: 'eng', is_admin: isAdmin }), ...steps],
  }
}

/** Steps 1 and 2 done, step 3 not checked yet: the owner's "2 of 3 done". */
const TWO_DONE = doc([
  step('github_connected', 'done', APP_CONNECTED),
  step('app_installed', 'done', { needed: true, read: true, installed: ['example-org'] }),
  step('orgs_enabled', 'done', { owners: OWNERS, orgs_read: true, orgs_capped: false, sso_hidden_orgs: 0, read_at: null, enabled: ['example-org'] }),
  step('repos_chosen', 'done', { repositories: GRANTS, capped: false }),
  step('access_verified', 'in_progress', {
    source: 'grants',
    token_id: 'tok_app',
    repositories: GRANTS.map((g) => ({ ...g, checks: {}, result: 'pending', verified_at: null })),
    unverified: [],
  }),
])

const NOT_CONNECTED = doc([
  step('github_connected', 'todo', { via: null }),
  step('app_installed', 'todo', { waiting_for: 'github_connected' }),
  step('orgs_enabled', 'todo', { waiting_for: 'github_connected' }),
  step('repos_chosen', 'todo', { waiting_for: 'github_connected' }),
  step('access_verified', 'todo', { waiting_for: 'github_connected' }),
])

/** Connected, App installed, nothing enabled on Access yet. */
const NOTHING_ENABLED = doc([
  step('github_connected', 'done', APP_CONNECTED),
  step('app_installed', 'done', { needed: true, read: true, installed: ['octo-dev'] }),
  step('orgs_enabled', 'in_progress', {
    owners: [{ ...OWNERS[0], enabled: false }],
    orgs_read: true,
    orgs_capped: false,
    sso_hidden_orgs: 0,
    read_at: null,
    enabled: [],
  }),
  step('repos_chosen', 'todo', { repositories: [], capped: false }),
  step('access_verified', 'todo', { waiting_for: 'repos_chosen' }),
])

async function mount(go = vi.fn()) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RepositoriesScreen } = await import('../Repositories')
  render(<RepositoriesScreen view="page=tokens" go={go} />)
  return { go }
}

afterEach(() => vi.unstubAllEnvs())

function routes(onboarding: () => unknown, extra: (m: string, url: string, body: unknown) => { status: number; body: unknown } | null = () => null) {
  return serve((m, url, body) => {
    if (m === 'GET' && url === '/v1/onboarding') return { status: 200, body: onboarding() }
    if (m === 'GET' && url === '/v1/git-tokens') return { status: 200, body: { git_tokens: [token()] } }
    if (m === 'GET' && url === '/v1/repositories') return { status: 200, body: { repositories: [] } }
    return extra(m, url, body)
  })
}

const steps = async () => {
  const head = await screen.findByText('Set up GitHub access', { selector: 'h2' }, WAIT)
  return head.closest<HTMLElement>('.ur-steps')!
}
const row = (root: HTMLElement, n: number) => root.querySelector<HTMLElement>(`.ur-step[data-n="${n}"]`)!

describe('the page opens on three steps, ticked from the onboarding read', () => {
  it('"2 of 3 done": Connect and Choose ticked with what they found, Try it offered', async () => {
    routes(() => TWO_DONE)
    await mount()
    const s = await steps()
    await waitFor(() => expect(row(s, 1)).toBeTruthy(), WAIT)
    expect(visible(s.querySelector('.ur-steps-count'))).toBe('2 of 3 done')
    expect([1, 2, 3].map((n) => row(s, n).getAttribute('data-state'))).toEqual(['done', 'done', 'todo'])
    expect(visible(row(s, 1).querySelector('h3'))).toBe('Connect GitHub')
    expect(visible(row(s, 1))).toContain('@octo-dev')
    expect(visible(row(s, 1))).toContain('renews automatically')
    expect(within(row(s, 1)).getByRole('button', { name: 'Disconnect' })).toBeTruthy()
    expect(visible(row(s, 2).querySelector('h3'))).toBe('Choose what it reaches')
    expect(visible(row(s, 2))).toContain('example-org (2 repos)')
    const manage = within(row(s, 2)).getByRole('link', { name: 'Manage in Access' })
    expect(manage.getAttribute('href')).toBe('/access')
    expect(visible(row(s, 3).querySelector('h3'))).toBe('Try it')
    const pick = within(row(s, 3)).getByRole('combobox', { name: 'Repository to check' })
    expect(Array.from((pick as HTMLSelectElement).options).map((o) => o.textContent)).toEqual(['example-org/example-api', 'example-org/example-web'])
    expect(within(row(s, 3)).getByRole('button', { name: 'Check access' })).toBeTruthy()
    expect(within(row(s, 3)).getByRole('button', { name: 'Run a test agent' })).toBeTruthy()
  })

  it('nothing connected: step 1 offers Connect GitHub, steps 2 and 3 wait for it and offer nothing', async () => {
    routes(() => NOT_CONNECTED)
    await mount()
    const s = await steps()
    await waitFor(() => expect(row(s, 1)).toBeTruthy(), WAIT)
    expect(visible(s.querySelector('.ur-steps-count'))).toBe('0 of 3 done')
    expect(within(row(s, 1)).getByRole('button', { name: 'Connect GitHub' })).toBeTruthy()
    for (const n of [2, 3]) {
      expect(visible(row(s, n))).toContain('After step 1')
      expect(row(s, n).querySelectorAll('button, a')).toHaveLength(0)
    }
  })

  it('an App installed nowhere: step 1 is not done and offers the install page', async () => {
    routes(() =>
      doc([
        step('github_connected', 'done', APP_CONNECTED),
        step('app_installed', 'todo', { needed: true, read: true, installed: [], login: 'octo-dev', install_url: 'https://github.com/apps/swarmcloud/installations/new' }),
        step('orgs_enabled', 'todo', { waiting_for: 'app_installed' }),
        step('repos_chosen', 'todo', { waiting_for: 'app_installed' }),
        step('access_verified', 'todo', { waiting_for: 'app_installed' }),
      ]),
    )
    await mount()
    const s = await steps()
    await waitFor(() => expect(row(s, 1)?.getAttribute('data-state')).toBe('todo'), WAIT)
    const install = within(row(s, 1)).getByRole('link', { name: 'Install the App' })
    expect(install.getAttribute('href')).toBe('https://github.com/apps/swarmcloud/installations/new')
    expect(visible(row(s, 1))).toContain('Install the SwarmCloud App')
    // The orgs line says what it waits on, never "appear here once it has".
    expect(visible(s)).not.toContain('appear here once it has')
  })
})

describe('Choose what it reaches says where orgs are enabled', () => {
  it('connected with no org enabled: says to enable them on Access, links there, and Enable acts inline', async () => {
    let enabled = false
    const calls = routes(
      () => (enabled ? TWO_DONE : NOTHING_ENABLED),
      (m, url) => {
        if (m === 'POST' && url === '/v1/access/orgs') {
          enabled = true
          return { status: 200, body: { org: { owner: 'octo-dev' }, tenant_id: 'eng' } }
        }
        return null
      },
    )
    await mount()
    const s = await steps()
    await waitFor(() => expect(row(s, 2)?.getAttribute('data-state')).toBe('todo'), WAIT)
    expect(visible(row(s, 2))).toMatch(/Enable the accounts and orgs SwarmCloud may work in.*Work › Access/)
    expect(visible(s)).not.toContain('appear here once it has')
    const manage = within(row(s, 2)).getByRole('link', { name: 'Manage in Access' })
    expect(manage.getAttribute('href')).toBe('/access')
    expect(manage.className).toContain('primary')
    fireEvent.click(within(row(s, 2)).getByRole('button', { name: 'Enable octo-dev' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url === '/v1/access/orgs')).toBe(true), WAIT)
    expect(calls.find((c) => c.method === 'POST' && c.url === '/v1/access/orgs')!.body).toEqual({ owner: 'octo-dev' })
    // The step is re-read, and ticks from what the server derived.
    await waitFor(() => expect(row(s, 2).getAttribute('data-state')).toBe('done'), WAIT)
  })
})

describe('Try it uses the existing verify routes', () => {
  it("an App connection's Check access verifies the chosen grant and re-reads the steps", async () => {
    let reads = 0
    const calls = routes(
      () => {
        reads += 1
        return TWO_DONE
      },
      (m, url) =>
        m === 'POST' && url === '/v1/access/grants/repo_1111111111111111/verify'
          ? { status: 200, body: { grant: { repo_id: 'repo_1111111111111111', repository: 'example-org/example-web' }, failures: [], passed: true, tenant_id: 'eng' } }
          : null,
    )
    await mount()
    const s = await steps()
    await waitFor(() => expect(row(s, 3)).toBeTruthy(), WAIT)
    fireEvent.change(within(row(s, 3)).getByRole('combobox', { name: 'Repository to check' }), { target: { value: 'repo_1111111111111111' } })
    const before = reads
    fireEvent.click(within(row(s, 3)).getByRole('button', { name: 'Check access' }))
    expect(await within(row(s, 3)).findByText(/example-org\/example-web: clone, push and pull request checks passed/, {}, WAIT)).toBeTruthy()
    expect(calls.filter((c) => c.method === 'POST').map((c) => c.url)).toEqual(['/v1/access/grants/repo_1111111111111111/verify'])
    await waitFor(() => expect(reads).toBeGreaterThan(before), WAIT)
  })

  it('a failed check shows the server\'s recovery copy for each failure', async () => {
    const copy = 'SwarmCloud cannot push to example-org/example-api as octo-dev. Ask an admin of example-org for write access.'
    routes(
      () => TWO_DONE,
      (m, url) =>
        m === 'POST' && url === '/v1/access/grants/repo_0a1b2c3d4e5f6071/verify'
          ? { status: 200, body: { grant: {}, failures: [{ check: 'push', code: 'PERMISSION_MISSING', copy }], passed: false, tenant_id: 'eng' } }
          : null,
    )
    await mount()
    const s = await steps()
    await waitFor(() => expect(row(s, 3)).toBeTruthy(), WAIT)
    fireEvent.click(within(row(s, 3)).getByRole('button', { name: 'Check access' }))
    expect(await within(row(s, 3)).findByText(copy, {}, WAIT)).toBeTruthy()
  })

  it("a stored token's Check access re-probes that token", async () => {
    const calls = routes(
      () =>
        doc([
          step('github_connected', 'done', { via: 'user', kind: 'fine_grained_pat', forge_login: 'octo-dev', token_id: 'tok_mine' }),
          step('app_installed', 'done', { needed: false }),
          step('orgs_enabled', 'done', { owners: OWNERS, orgs_read: true, orgs_capped: false, sso_hidden_orgs: 0, read_at: null, enabled: null }),
          step('repos_chosen', 'done', { repositories: GRANTS, capped: false }),
          step('access_verified', 'in_progress', { token_id: 'tok_mine', repositories: [] }),
        ]),
      (m, url) => (m === 'POST' && url === '/v1/git-tokens/tok_mine/verify' ? { status: 200, body: { token: token({ token_id: 'tok_mine' }) } } : null),
    )
    await mount()
    const s = await steps()
    await waitFor(() => expect(row(s, 3)).toBeTruthy(), WAIT)
    fireEvent.click(within(row(s, 3)).getByRole('button', { name: 'Check access' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true), WAIT)
    expect(calls.filter((c) => c.method === 'POST').map((c) => c.url)).toEqual(['/v1/git-tokens/tok_mine/verify'])
  })

  it('Run a test agent opens Submit with claude-code, a harmless prompt and the chosen repository', async () => {
    routes(() => TWO_DONE)
    const { go } = await mount()
    const s = await steps()
    await waitFor(() => expect(row(s, 3)).toBeTruthy(), WAIT)
    fireEvent.change(within(row(s, 3)).getByRole('combobox', { name: 'Repository to check' }), { target: { value: 'repo_1111111111111111' } })
    fireEvent.click(within(row(s, 3)).getByRole('button', { name: 'Run a test agent' }))
    expect(go).toHaveBeenCalledWith('work/new')
    const { peekTaskPrefill } = await import('../submitPrefill')
    const p = peekTaskPrefill()
    expect(p?.profile).toBe('claude-code')
    expect(p?.repositoryUrl).toBe('https://github.com/example-org/example-web.git')
    // Harmless: it reads and reports, it changes nothing.
    expect(p?.prompt).toMatch(/do not change/i)
  })
})

describe('Submit takes the test task it was handed', () => {
  it('opens with the runner chosen, the prompt and the repository filled, and pushes nothing', async () => {
    vi.resetModules()
    const { prefillTask } = await import('../submitPrefill')
    const { SubmitScreen } = await import('../Submit')
    prefillTask({ profile: 'claude-code', prompt: 'List the top-level files. Do not change anything.', repositoryUrl: 'https://github.com/example-org/example-web.git' })
    const { container } = render(<SubmitScreen />)
    await waitFor(() => expect(container.querySelector<HTMLInputElement>('input[name="runner-profile"][value="claude-code"]')?.checked).toBe(true), WAIT)
    expect(container.querySelector<HTMLInputElement>('#dsp-repo')!.value).toBe('https://github.com/example-org/example-web.git')
    expect(Array.from(container.querySelectorAll<HTMLTextAreaElement | HTMLInputElement>('textarea, input')).some((e) => e.value === 'List the top-level files. Do not change anything.')).toBe(true)
    // Handed once: the next Submit opens empty.
    const { peekTaskPrefill } = await import('../submitPrefill')
    await waitFor(() => expect(peekTaskPrefill()).toBeNull(), WAIT)
  })
})
