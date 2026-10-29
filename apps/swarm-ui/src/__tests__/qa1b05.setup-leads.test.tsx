// CONSOLE VISUAL QA PASS 1B, LANE QA1B-05 (part of #2596): the pass-1
// findings QA1-FIX-10 and QA1-FIX-18 wrote as patches for Access.tsx and
// WorkspaceSettings.tsx, applied.
//
//   * config-08 (GitHub access): with the strip's step 1 held at "Enable orgs
//     failed" the lead said "GitHub access is set up". It names the held step
//     and sends the reader to Workspace setup, whose checklist says what to do.
//   * config-08 (Workspace setup): the lead said "The strip above summarises
//     this checklist." over a strip counting 1 of 4 and a checklist counting
//     3 of 4. It says both counts, each named.
//   * config-12: the GitHub permissions section sat inside the checklist's
//     two-thirds column, leaving the right-hand column empty for its whole
//     height. It is under the grid, across both columns.
//
// Fixtures only; no network. Nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>

const step = (name: string, state: string, evidence: Json = {}): Json => ({ step: name, state, code: null, copy: null, checked_at: null, evidence, issues: [] })

/** GitHub connected and repositories chosen, but Enable orgs failed: the strip's step 1 is held at "Enable orgs failed". */
const HELD: Json = {
  tenant_id: 'eng',
  user: 'ana.b@example.com',
  user_hash: 'h',
  derived_at: new Date().toISOString(),
  steps: [
    step('signed_in', 'done'),
    step('github_connected', 'done', { login: 'octo-dev' }),
    step('repos_chosen', 'done', { repositories: ['a', 'b'] }),
    step('orgs_enabled', 'failed'),
  ],
  next_step: 'orgs_enabled',
  complete: false,
  dismissed: false,
  source: 'derived',
}

const CONNECTION: Json = {
  connection_id: 'github:eng:abc',
  forge: 'github',
  method: 'app_user',
  forge_login: 'octo-dev',
  forge_user_id: 42,
  token_id: 'tok_u',
  state: 'active',
  failure: null,
  access_expires_at: new Date(Date.now() + 7 * 3600_000).toISOString(),
  refresh_expires_at: null,
  refreshed_at: new Date(Date.now() - 600_000).toISOString(),
  refreshing: false,
  created_at: new Date().toISOString(),
  connected_at: new Date().toISOString(),
  revoked_at: null,
}

const ORG: Json = {
  owner: 'example-org',
  owner_type: 'Organization',
  installation_id: 7,
  repository_selection: 'selected',
  install_state: 'installed',
  sso: 'ok',
  enabled: true,
  enabled_at: null,
  checked_at: null,
}

const GRANT = (n: number): Json => ({
  repo_id: `repo_${String(n).padStart(16, '0')}`,
  repository: `example-org/example-${n}`,
  owner: 'example-org',
  mode: 'read',
  can_push: true,
  archived: false,
  granted_at: null,
  granted_by: 'dev@example.com',
  checks: { clone: { state: 'ok', code: null, checked_at: null } },
  verified_at: null,
})

function api() {
  serve((m, url) => {
    if (m !== 'GET') return null
    if (url === '/v1/access') return { status: 200, body: { tenant_id: 'eng', connection: CONNECTION, orgs: [ORG], grants: [GRANT(1), GRANT(2)], github_quota: [] } }
    if (url === '/v1/access/orgs') return { status: 200, body: { tenant_id: 'eng', orgs_listed: true, install_url: null, owners: [{ ...ORG, install_url: null }] } }
    if (url.startsWith('/v1/access/orgs/example-org/repositories')) {
      return { status: 200, body: { tenant_id: 'eng', owner: 'example-org', repositories: [], page: 1, per_page: 100, max_pages: 10, next_page: null, capped: false, q: null, total_count: 0 } }
    }
    if (url === '/v1/onboarding') return { status: 200, body: HELD }
    if (url === '/v1/profile') return { status: 404, body: { detail: 'Not Found' } }
    if (url === '/v1/workspace/roles') return { status: 404, body: { detail: 'Not Found' } }
    if (url === '/v1/judgment') return { status: 404, body: { detail: 'Not Found' } }
    if (url.startsWith('/v1/forge/permissions')) return { status: 404, body: { detail: 'Not Found' } }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

describe('config-08: GitHub access’s lead names the step the strip holds, never "set up"', () => {
  it('with the strip’s step 1 held at Enable orgs failed, the lead says so and sends the reader to Workspace setup', async () => {
    api()
    window.history.replaceState(null, '', '/config/github')
    const [{ AccessScreen }, { SetupContext }] = await Promise.all([import('../Access'), import('../ConfigSetup')])
    render(
      <SetupContext.Provider value={{ doc: { status: 'ok', fetchedAt: Date.now(), data: HELD as never }, personal: true }}>
        <AccessScreen />
      </SetupContext.Provider>,
    )
    const lead = (await screen.findByRole('region', { name: 'Next step' })) as HTMLElement
    const words = visible(lead)
    // MUTATION: drop the held-step branch -> "GitHub access is set up" over a failed step, fails.
    expect(words).not.toContain('GitHub access is set up')
    expect(words).toContain('Step 1 of 3 · needs you.')
    expect(within(lead).getByRole('heading', { level: 2 }).textContent).toBe('Enable orgs failed')
    expect(words).toContain("GitHub is connected as @octo-dev, but Workspace setup's checklist says Enable orgs failed")
    const buttons = [...lead.querySelectorAll<HTMLElement>('.c-btn')]
    expect(buttons.map((b) => b.textContent?.trim())).toEqual(['Open Workspace setup'])
    expect(buttons[0]!.getAttribute('href')).toBe('/config/workspace/setup')
  })
})

async function setupPart() {
  api()
  const [{ WorkspaceSettingsScreen }, { SetupContext }] = await Promise.all([import('../WorkspaceSettings'), import('../ConfigSetup')])
  render(
    <SetupContext.Provider value={{ doc: { status: 'ok', fetchedAt: Date.now(), data: HELD as never }, personal: false }}>
      <WorkspaceSettingsScreen part="setup" />
    </SetupContext.Provider>,
  )
}

describe('config-08: Workspace setup’s lead says both counts, each named', () => {
  it('names the strip’s count and the checklist’s, not "summarises"', async () => {
    await setupPart()
    const lead = (await screen.findByRole('region', { name: 'Step 4 of 4 · next' })) as HTMLElement
    // The strip: GitHub held (failed), Repositories done, Secrets and People unread -> 1 of 4.
    // The checklist: signed in, GitHub connected and repositories chosen done -> 3 of 4.
    // MUTATION: put "The strip above summarises this checklist." back -> fails.
    expect(visible(lead)).not.toContain('summarises')
    expect(visible(lead)).toContain('The strip above counts its own 4 steps, 1 of 4 done; this checklist counts 3 of 4 done.')
  })
})

describe('config-12: the GitHub permissions section spans the setup page, under the grid', () => {
  it('draws the section after the grid, not inside the checklist’s column', async () => {
    await setupPart()
    await screen.findByRole('list', { name: 'Setup steps' })
    const section = document.querySelector<HTMLElement>('.fp-section')
    expect(section).not.toBeNull()
    // MUTATION: mount GitHubPermissions inside OnboardingScreen again -> it sits in .ob-page and .ws-grid, fails.
    expect(section!.closest('.ob-page')).toBeNull()
    expect(section!.closest('.ws-grid')).toBeNull()
    const grid = document.querySelector('.ws-grid.is-setup')!
    expect(grid.compareDocumentPosition(section!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})
