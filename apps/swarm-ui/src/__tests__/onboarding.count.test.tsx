// THE SETUP COUNT AND THE PROBE'S AGE (2026-10-08).
//
// WHAT EACH CASE HOLDS:
//   * the count is REQUIRED steps only: a step the server serves with
//     `required: false` (the workspace steps while WORKSPACE_GATE is off) is
//     named in a separate optional note and never holds the count below its
//     total. Before this, a complete checklist read "8 of 9 done" forever;
//   * a step with `required` absent or null is required (the server's own
//     `s.get("required", True)`), so it still counts against the total;
//   * with no optional step, the count reads as it always did;
//   * a connection whose last probe did not complete shows the probe's error
//     with its age ("as of 22:55; re-checked hourly"), and a complete probe
//     shows no such line.
//
// Fixtures only; no network. Nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Step = { step: string; state: string; required?: boolean | null; evidence?: Record<string, unknown> }

function doc(steps: Step[]) {
  const full = steps.map((s) => ({ code: null, copy: null, checked_at: null, evidence: {}, issues: [], ...s }))
  return {
    tenant_id: 'eng',
    user: 'dev@swarm.example.com',
    user_hash: '89abcdef01234567',
    derived_at: new Date().toISOString(),
    next_step: full.find((s) => s.state !== 'done' && s.required !== false)?.step ?? null,
    complete: full.every((s) => s.state === 'done' || s.required === false),
    source: 'derived on this read',
    steps: full,
  }
}

const CONNECTED = { via: 'user', kind: 'app_user', forge_login: 'octo-dev', token_state: 'active', probe_complete: true, probe_error: null }

const REST: Step[] = [
  { step: 'github_connected', state: 'done', required: true, evidence: CONNECTED },
  { step: 'app_installed', state: 'done', evidence: { needed: true, read: true, installed: ['octo-dev'] } },
  { step: 'orgs_enabled', state: 'done', evidence: { owners: [{ owner: 'octo-dev', owner_type: 'User', reach: 'reachable' }] } },
  { step: 'repos_chosen', state: 'done', evidence: { repositories: [{ repository: 'octo-dev/example-api', mode: 'write' }] } },
  { step: 'access_verified', state: 'done', evidence: { repositories: [{ repository: 'octo-dev/example-api', result: 'passed' }] } },
  { step: 'ready', state: 'done', evidence: { first_repository: 'octo-dev/example-api' } },
]

/** Complete, with the workspace step optional and not done: the 2026-10-08 page. */
const OPTIONAL_UNDONE = doc([
  { step: 'signed_in', state: 'done' },
  { step: 'workspace', state: 'todo', required: false },
  { step: 'claude_account', state: 'done', required: null },
  ...REST,
])

/** The same steps, every one required and done. */
const ALL_DONE = doc([{ step: 'signed_in', state: 'done' }, ...REST])

/** A null `required` is required: not done, it holds the count. */
const NULL_REQUIRED_UNDONE = doc([
  { step: 'signed_in', state: 'done' },
  { step: 'workspace', state: 'todo', required: false },
  { step: 'claude_account', state: 'todo', required: null },
  ...REST,
])

const DENIED = "swarm-api may not read swarm-tenant-eng-git-u-0123456789abcdef: its conditional project grant on tenant eng's GitHub user slots is missing"

function probed(evidence: Record<string, unknown>) {
  return doc([{ step: 'signed_in', state: 'done' }, { step: 'github_connected', state: 'in_progress', required: true, evidence: { ...CONNECTED, ...evidence } }, ...REST.slice(1)])
}

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../Onboarding')
}

function answer(body: unknown) {
  return serve((m, url) => (m === 'GET' && url === '/v1/onboarding' ? { status: 200, body } : null))
}

const stepEl = (name: string) => document.querySelector(`li.ob-step[data-step="${name}"]`) as HTMLElement

afterEach(() => {
  vi.unstubAllEnvs()
  window.localStorage.clear()
})

describe('the setup count counts required steps only', () => {
  it('reads 8 of 8 required done, with the undone optional step named apart', async () => {
    answer(OPTIONAL_UNDONE)
    const { OnboardingScreen } = await load()
    render(<OnboardingScreen />)
    await screen.findByRole('list', { name: 'Setup steps' })
    expect(screen.getByText('Setup is complete')).toBeTruthy()
    expect(screen.getByText('8 of 8 required done · 1 optional (Request your workspace)')).toBeTruthy()
    expect(screen.queryByText(/8 of 9/)).toBeNull()
    expect(screen.getByRole('img', { name: '8 of 8 required setup steps done' })).toBeTruthy()
  })

  it('reads as it always did when every step is required and done', async () => {
    answer(ALL_DONE)
    const { OnboardingScreen } = await load()
    render(<OnboardingScreen />)
    await screen.findByRole('list', { name: 'Setup steps' })
    expect(screen.getByText('7 of 7 done')).toBeTruthy()
    expect(screen.queryByText(/optional/)).toBeNull()
    expect(screen.getByRole('img', { name: '7 of 7 setup steps done' })).toBeTruthy()
  })

  it('counts a step whose required is null as required', async () => {
    answer(NULL_REQUIRED_UNDONE)
    const { OnboardingScreen } = await load()
    render(<OnboardingScreen />)
    await screen.findByRole('list', { name: 'Setup steps' })
    expect(screen.getByText('7 of 8 required done · 1 optional (Request your workspace)')).toBeTruthy()
  })

  it('counts the same on the Overview card', async () => {
    answer(NULL_REQUIRED_UNDONE)
    const { SetupCard } = await load()
    render(<SetupCard />)
    expect(await screen.findByText('7 of 8 required done · 1 optional (Request your workspace)')).toBeTruthy()
  })
})

describe('an incomplete probe shows its error with its age', () => {
  it('names the error, the time it was seen, and that it is re-checked hourly', async () => {
    const at = new Date(2026, 9, 7, 22, 55).toISOString()
    answer(probed({ probe_complete: false, probe_error: DENIED, probe_attempted_at: at }))
    const { OnboardingScreen } = await load()
    render(<OnboardingScreen />)
    await screen.findByRole('list', { name: 'Setup steps' })
    const text = visible(stepEl('github_connected'))
    expect(text).toContain(DENIED)
    expect(text).toContain('as of 22:55; re-checked hourly')
  })

  it('shows no probe error once the probe completed', async () => {
    const at = new Date(2026, 9, 7, 23, 59).toISOString()
    answer(probed({ probe_complete: true, probe_error: null, probe_attempted_at: at }))
    const { OnboardingScreen } = await load()
    render(<OnboardingScreen />)
    await screen.findByRole('list', { name: 'Setup steps' })
    const text = visible(stepEl('github_connected'))
    expect(text).not.toContain('re-checked hourly')
    expect(text).not.toContain('may not read')
  })

  it('shows a leftover error on a complete probe nowhere', async () => {
    const at = new Date(2026, 9, 7, 23, 59).toISOString()
    answer(probed({ probe_complete: true, probe_error: DENIED, probe_attempted_at: at }))
    const { OnboardingScreen } = await load()
    render(<OnboardingScreen />)
    await screen.findByRole('list', { name: 'Setup steps' })
    expect(visible(stepEl('github_connected'))).not.toContain('re-checked hourly')
  })
})
