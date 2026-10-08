// THE ONBOARDING CHECKLIST (#780, OB8; docs/onboarding.md §2.1-§2.3, Entry A).
//
// WHAT EACH CASE HOLDS:
//   * the seven steps render in the server's order, each with the state the
//     server derived, and the next step is the one `next_step` names;
//   * Install the App (#780, 2026-10-08) is its own step between Connect
//     GitHub and the orgs: installed nowhere, it says so and links the App's
//     install page; installed, it names where; a token connection needs none;
//   * a failed step prints the §2.3 copy the server sent, WORD FOR WORD, with
//     the GitHub page it names; a step waiting on another says which;
//   * each not-done step's action goes where the work is: Connect GitHub
//     starts the App's authorisation, the rest open Work › Access;
//   * on Overview the card shows while setup is incomplete, draws nothing when
//     it is complete or its read failed, and Hide keeps it hidden on remount;
//   * a complete checklist offers the first task.
//
// Fixtures only; no network. Nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Step = { step: string; state: string; code?: string | null; copy?: string | null; evidence?: Record<string, unknown>; issues?: unknown[] }

const SSO_COPY =
  "example-org uses SAML single sign-on and GitHub has not linked your session to it yet. Open https://github.com/orgs/example-org/sso, sign in with example-org's identity provider, then press Re-check. Nothing in SwarmCloud has to change."

function doc(steps: Step[], over: Record<string, unknown> = {}) {
  const full = steps.map((s) => ({ code: null, copy: null, checked_at: null, evidence: {}, issues: [], ...s }))
  return {
    tenant_id: 'eng',
    user: 'dev@swarm.example.com',
    user_hash: '89abcdef01234567',
    derived_at: new Date().toISOString(),
    next_step: full.find((s) => s.state !== 'done')?.step ?? null,
    complete: full.every((s) => s.state === 'done'),
    source: 'derived on this read',
    steps: full,
    ...over,
  }
}

const FRESH = doc([
  { step: 'signed_in', state: 'done', evidence: { email: 'dev@swarm.example.com', tenant_id: 'eng' } },
  { step: 'github_connected', state: 'todo', evidence: { via: null } },
  { step: 'app_installed', state: 'todo', evidence: { waiting_for: 'github_connected' } },
  { step: 'orgs_enabled', state: 'todo', evidence: { waiting_for: 'github_connected' } },
  { step: 'repos_chosen', state: 'todo', evidence: { waiting_for: 'github_connected' } },
  { step: 'access_verified', state: 'todo', evidence: { waiting_for: 'github_connected' } },
  { step: 'ready', state: 'todo', evidence: { waiting_for: 'github_connected' } },
])

const CONNECTED = { via: 'user', kind: 'app_user', forge_login: 'octo-dev', token_state: 'active' }

const SSO_FAILED = doc([
  { step: 'signed_in', state: 'done', evidence: { email: 'dev@swarm.example.com', tenant_id: 'eng' } },
  { step: 'github_connected', state: 'done', evidence: CONNECTED },
  { step: 'app_installed', state: 'done', evidence: { needed: true, read: true, installed: ['octo-dev', 'example-org'] } },
  {
    step: 'orgs_enabled',
    state: 'failed',
    evidence: { owners: [{ owner: 'octo-dev', owner_type: 'User', reach: 'reachable' }, { owner: 'example-org', owner_type: 'Organization', reach: 'sso_required' }] },
    issues: [{ code: 'SSO_NOT_AUTHORISED', owner: 'example-org', url: 'https://github.com/orgs/example-org/sso', copy: SSO_COPY }],
  },
  { step: 'repos_chosen', state: 'done', evidence: { repositories: [{ repository: 'octo-dev/example-api', mode: 'write' }] } },
  { step: 'access_verified', state: 'in_progress', evidence: { repositories: [{ repository: 'octo-dev/example-api', result: 'pending' }] } },
  { step: 'ready', state: 'todo', evidence: { waiting_for: 'orgs_enabled' } },
])

const COMPLETE = doc([
  { step: 'signed_in', state: 'done' },
  { step: 'github_connected', state: 'done', evidence: CONNECTED },
  { step: 'app_installed', state: 'done', evidence: { needed: true, read: true, installed: ['octo-dev'] } },
  { step: 'orgs_enabled', state: 'done', evidence: { owners: [{ owner: 'octo-dev', owner_type: 'User', reach: 'reachable' }] } },
  { step: 'repos_chosen', state: 'done', evidence: { repositories: [{ repository: 'octo-dev/example-api', mode: 'write' }] } },
  { step: 'access_verified', state: 'done', evidence: { repositories: [{ repository: 'octo-dev/example-api', result: 'passed' }] } },
  { step: 'ready', state: 'done', evidence: { first_repository: 'octo-dev/example-api' } },
])

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../Onboarding')
}

function answer(body: unknown, status = 200) {
  return serve((m, url) => (m === 'GET' && url === '/v1/onboarding' ? { status, body } : null))
}

const steps = () => [...document.querySelectorAll('li.ob-step')] as HTMLElement[]
const stepEl = (name: string) => document.querySelector(`li.ob-step[data-step="${name}"]`) as HTMLElement

afterEach(() => {
  vi.unstubAllEnvs()
  window.localStorage.clear()
})

describe('the Setup page draws the server-derived checklist', () => {
  it('renders the seven steps in order with their states, and marks the next one', async () => {
    answer(FRESH)
    const { OnboardingScreen } = await load()
    render(<OnboardingScreen />)
    await screen.findByRole('list', { name: 'Setup steps' })
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Setup')
    expect(steps().map((li) => li.dataset.step)).toEqual(['signed_in', 'github_connected', 'app_installed', 'orgs_enabled', 'repos_chosen', 'access_verified', 'ready'])
    expect(steps().map((li) => li.dataset.state)).toEqual(['done', 'todo', 'todo', 'todo', 'todo', 'todo', 'todo'])
    expect(stepEl('github_connected').getAttribute('aria-current')).toBe('step')
    expect(steps().filter((li) => li.getAttribute('aria-current') === 'step')).toHaveLength(1)
    expect(visible(stepEl('signed_in'))).toContain('dev@swarm.example.com · tenant eng')
    expect(screen.getByText('1 of 7 done')).toBeTruthy()
    expect(screen.getByRole('img', { name: '1 of 7 setup steps done' })).toBeTruthy()
  })

  it('says what a waiting step waits for, and offers it no action', async () => {
    answer(FRESH)
    const { OnboardingScreen } = await load()
    render(<OnboardingScreen />)
    await screen.findByRole('list', { name: 'Setup steps' })
    expect(visible(stepEl('orgs_enabled'))).toContain('waits for Connect GitHub')
    expect(within(stepEl('orgs_enabled')).queryByRole('link')).toBeNull()
    expect(within(stepEl('ready')).queryByRole('link')).toBeNull()
  })

  it('starts the App authorisation from the Connect GitHub step', async () => {
    const calls = serve((m, url) => {
      if (m === 'GET' && url === '/v1/onboarding') return { status: 200, body: FRESH }
      if (m === 'POST' && url === '/v1/onboarding/github/authorize') {
        return { status: 200, body: { authorize_url: 'https://github.com/login/oauth/authorize?client_id=Iv1.example', expires_in_seconds: 600 } }
      }
      return null
    })
    const mod = await load()
    const gh = await import('../GitHubConnect')
    const assign = vi.spyOn(gh.browser, 'assign').mockImplementation(() => {})
    render(<mod.OnboardingScreen />)
    await screen.findByRole('list', { name: 'Setup steps' })
    fireEvent.click(within(stepEl('github_connected')).getByRole('button', { name: 'Connect GitHub' }))
    await waitFor(() => expect(assign).toHaveBeenCalledWith('https://github.com/login/oauth/authorize?client_id=Iv1.example'))
    expect(calls.filter((c) => c.url === '/v1/onboarding/github/authorize')).toEqual([expect.objectContaining({ method: 'POST', body: { surface: 'console' } })])
  })

  it("prints a failed step's §2.3 copy word for word, with the GitHub page it names", async () => {
    answer(SSO_FAILED)
    const { OnboardingScreen } = await load()
    render(<OnboardingScreen />)
    await screen.findByRole('list', { name: 'Setup steps' })
    const orgs = stepEl('orgs_enabled')
    expect(orgs.dataset.state).toBe('failed')
    expect(orgs.getAttribute('aria-current')).toBe('step')
    expect(within(orgs).getByText('SSO_NOT_AUTHORISED')).toBeTruthy()
    expect(visible(orgs.querySelector('.ob-issue p'))).toBe(`SSO_NOT_AUTHORISED ${SSO_COPY}`)
    expect(within(orgs).getByRole('link', { name: 'Open on GitHub' }).getAttribute('href')).toBe('https://github.com/orgs/example-org/sso')
    expect(visible(orgs)).toContain('example-org (sso required)')
    // The fix is on Access, and Re-check is a fresh read.
    expect(within(orgs).getByRole('link', { name: 'Enable orgs' }).getAttribute('href')).toBe('/access')
    expect(within(orgs).getByRole('button', { name: 'Re-check' })).toBeTruthy()
    // An in-progress step later on also points at Access.
    expect(within(stepEl('access_verified')).getByRole('link', { name: 'Verify access' }).getAttribute('href')).toBe('/access')
    expect(visible(stepEl('repos_chosen'))).toContain('1 granted: octo-dev/example-api (write)')
    expect(visible(stepEl('access_verified'))).toContain('0 passed · 1 not checked yet')
  })

  it('re-reads the checklist on Re-check', async () => {
    const calls = answer(SSO_FAILED)
    const { OnboardingScreen } = await load()
    render(<OnboardingScreen />)
    await screen.findByRole('list', { name: 'Setup steps' })
    const before = calls.filter((c) => c.url === '/v1/onboarding').length
    fireEvent.click(within(stepEl('orgs_enabled')).getByRole('button', { name: 'Re-check' }))
    await waitFor(() => expect(calls.filter((c) => c.url === '/v1/onboarding').length).toBe(before + 1))
  })

  it('offers the first task once every step is done', async () => {
    answer(COMPLETE)
    const { OnboardingScreen } = await load()
    render(<OnboardingScreen />)
    await screen.findByRole('list', { name: 'Setup steps' })
    expect(screen.getByText('Setup is complete')).toBeTruthy()
    expect(within(stepEl('ready')).getByRole('link', { name: 'Submit a task' }).getAttribute('href')).toBe('/submit/task')
    expect(visible(stepEl('ready'))).toContain('submit a first task in octo-dev/example-api')
    expect(document.querySelector('[aria-current="step"]')).toBeNull()
  })
})

const INSTALL_URL = 'https://github.com/apps/swarmcloud-saga/installations/new'

const NOT_INSTALLED = doc([
  { step: 'signed_in', state: 'done', evidence: { email: 'dev@swarm.example.com', tenant_id: 'eng' } },
  { step: 'github_connected', state: 'done', evidence: CONNECTED },
  {
    step: 'app_installed',
    state: 'todo',
    evidence: { needed: true, read: true, source: 'github', login: 'octo-dev', installed: [], not_installed: ['octo-dev'], install_url: INSTALL_URL },
  },
  { step: 'orgs_enabled', state: 'todo', evidence: { waiting_for: 'app_installed' } },
  { step: 'repos_chosen', state: 'todo', evidence: { waiting_for: 'app_installed' } },
  { step: 'access_verified', state: 'todo', evidence: { waiting_for: 'app_installed' } },
  { step: 'ready', state: 'todo', evidence: { waiting_for: 'app_installed' } },
])

describe('the Install the App step (#780, 2026-10-08)', () => {
  it('sits between Connect GitHub and the orgs, says the App is installed nowhere, and links its install page', async () => {
    const calls = answer(NOT_INSTALLED)
    const { OnboardingScreen } = await load()
    render(<OnboardingScreen />)
    await screen.findByRole('list', { name: 'Setup steps' })
    expect(steps().map((li) => li.dataset.step).slice(1, 4)).toEqual(['github_connected', 'app_installed', 'orgs_enabled'])
    const inst = stepEl('app_installed')
    expect(inst.getAttribute('aria-current')).toBe('step')
    expect(visible(inst)).toContain('Install the App')
    expect(visible(inst)).toContain("connected as @octo-dev, but SwarmCloud Saga isn't installed anywhere yet")
    expect(visible(inst)).toContain('Connecting authorised the App to act as you')
    const link = within(inst).getByRole('link', { name: 'Install the App' })
    expect(link.getAttribute('href')).toBe(INSTALL_URL)
    expect(link.getAttribute('target')).toBe('_blank')
    expect(visible(stepEl('orgs_enabled'))).toContain('waits for Install the App')
    const before = calls.filter((c) => c.url === '/v1/onboarding').length
    fireEvent.click(within(inst).getByRole('button', { name: 'Re-check' }))
    await waitFor(() => expect(calls.filter((c) => c.url === '/v1/onboarding').length).toBe(before + 1))
  })

  it('names where the App is installed once it is, and offers no install', async () => {
    answer(COMPLETE)
    const { OnboardingScreen } = await load()
    render(<OnboardingScreen />)
    await screen.findByRole('list', { name: 'Setup steps' })
    const inst = stepEl('app_installed')
    expect(inst.dataset.state).toBe('done')
    expect(visible(inst)).toContain('installed on octo-dev')
    expect(within(inst).queryByRole('link')).toBeNull()
  })

  it('says a token connection needs no installation', async () => {
    const changed = NOT_INSTALLED.steps.map((s) =>
      s.step === 'app_installed' ? { ...s, state: 'done', evidence: { needed: false, via: 'tenant', kind: 'classic_pat' } } : s,
    )
    answer({ ...NOT_INSTALLED, steps: changed, next_step: 'orgs_enabled' })
    const { OnboardingScreen } = await load()
    render(<OnboardingScreen />)
    await screen.findByRole('list', { name: 'Setup steps' })
    expect(visible(stepEl('app_installed'))).toContain('not needed: this connection is a token, not the App')
  })
})

describe('the Setup card on Overview (Entry A)', () => {
  it('shows while setup is incomplete, with Open setup', async () => {
    answer(SSO_FAILED)
    const { SetupCard } = await load()
    render(<SetupCard />)
    expect(await screen.findByText('Set up SwarmCloud')).toBeTruthy()
    expect(screen.getByText('4 of 7 done')).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Open setup' }).getAttribute('href')).toBe('/setup')
  })

  it('draws nothing once setup is complete', async () => {
    const calls = answer(COMPLETE)
    const { SetupCard } = await load()
    const { container } = render(<SetupCard />)
    await waitFor(() => expect(calls.length).toBeGreaterThan(0))
    await new Promise((r) => setTimeout(r, 20))
    expect(container.innerHTML).toBe('')
  })

  it('draws nothing when its read failed', async () => {
    const calls = answer({ code: 'internal', message: 'boom' }, 500)
    const { SetupCard } = await load()
    const { container } = render(<SetupCard />)
    await waitFor(() => expect(calls.length).toBeGreaterThan(0))
    await new Promise((r) => setTimeout(r, 20))
    expect(container.innerHTML).toBe('')
  })

  it('Hide keeps the card hidden in this browser, and the steps keep their state', async () => {
    answer(FRESH)
    const { SetupCard, OnboardingScreen } = await load()
    const first = render(<SetupCard />)
    fireEvent.click(await screen.findByRole('button', { name: 'Hide' }))
    expect(screen.queryByText('Set up SwarmCloud')).toBeNull()
    expect(window.localStorage.getItem('swarm.setup.hidden:eng:89abcdef01234567')).toBe('1')
    first.unmount()

    const again = render(<SetupCard />)
    await new Promise((r) => setTimeout(r, 20))
    expect(again.container.innerHTML).toBe('')
    again.unmount()

    // Work › Setup still shows the same steps.
    render(<OnboardingScreen />)
    await screen.findByRole('list', { name: 'Setup steps' })
    expect(steps().map((li) => li.dataset.state)).toEqual(['done', 'todo', 'todo', 'todo', 'todo', 'todo', 'todo'])
  })
})
