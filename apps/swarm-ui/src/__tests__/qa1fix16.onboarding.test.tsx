// VISUAL QA PASS 1, LANE QA1-FIX-16 (part of #2596): the Workspace setup
// checklist's Verify access and Ready rows, on the fixture console's own
// onboarding document (api.ts `fixtureOnboarding`: orgs_enabled failed on
// SSO, access_verified in progress with example-web not checked yet, Ready
// todo with `waiting_for: 'orgs_enabled'`).
//
//   config-10: the Verify access row said "press Verify on GitHub access" and
//     linked Config › GitHub access, which has no Verify; Verify is on
//     Config › Repositories ("Repositories SwarmCloud may reach as you").
//   config-11: Ready said "waits for Enable orgs" alone while Verify access
//     was unfinished too.
//
// MUTATIONS: link the sentence or the Verify access button to ACCESS again
// -> the href cases fail; return the server's one `waiting_for` name for Ready
// -> "waits for Enable orgs and Verify access" fails; list a step that itself
// only waits on an earlier one -> the FRESH case fails.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
})

async function mountFixtures() {
  vi.resetModules()
  const { OnboardingScreen } = await import('../Onboarding')
  render(<OnboardingScreen />)
  await screen.findByRole('list', { name: 'Setup steps' }, { timeout: 5000 })
}

const stepEl = (name: string) => document.querySelector(`li.ob-step[data-step="${name}"]`) as HTMLElement

describe('config-10: Verify access names and links the page that holds Verify', () => {
  it('says "press Verify on Repositories" and links Config › Repositories', async () => {
    await mountFixtures()
    const row = stepEl('access_verified')
    expect(visible(row)).toContain('example-org/example-web: press Verify on Repositories')
    expect(visible(row)).not.toContain('GitHub access')
    expect(within(row).getByRole('link', { name: 'Repositories' }).getAttribute('href')).toBe('/config/repositories')
  })

  it("sends the step's Verify access button to Repositories too", async () => {
    await mountFixtures()
    expect(within(stepEl('access_verified')).getByRole('link', { name: 'Verify access' }).getAttribute('href')).toBe('/config/repositories')
    // Enable orgs stays on GitHub access, where orgs are enabled.
    expect(within(stepEl('orgs_enabled')).getByRole('link', { name: 'Enable orgs' }).getAttribute('href')).toBe('/config/github')
  })
})

describe('config-11: Ready names every unfinished required step before it', () => {
  it('reads "waits for Enable orgs and Verify access" on the fixture document', async () => {
    await mountFixtures()
    expect(visible(stepEl('ready'))).toContain('waits for Enable orgs and Verify access')
  })
  it('names only the step a person can act on when the later ones wait on it', async () => {
    const step = (name: string, state: string, evidence: Record<string, unknown> = {}) => ({ step: name, state, code: null, copy: null, checked_at: null, evidence, issues: [] })
    const waits = { waiting_for: 'github_connected' }
    serve((m, url) =>
      m === 'GET' && url === '/v1/onboarding'
        ? {
            status: 200,
            body: {
              tenant_id: 'eng', user: 'dev@swarm.example.com', user_hash: '89abcdef01234567', derived_at: new Date().toISOString(),
              next_step: 'github_connected', complete: false, source: 'derived on this read',
              steps: [
                step('signed_in', 'done'),
                step('github_connected', 'todo', { via: null }),
                step('app_installed', 'todo', waits),
                step('orgs_enabled', 'todo', waits),
                step('repos_chosen', 'todo', waits),
                step('access_verified', 'todo', waits),
                step('ready', 'todo', waits),
              ],
            },
          }
        : null,
    )
    vi.stubEnv('VITE_LIVE', '1')
    await mountFixtures()
    expect(visible(stepEl('ready'))).toContain('waits for Connect GitHub')
    expect(visible(stepEl('ready'))).not.toContain(' and ')
  })
})
