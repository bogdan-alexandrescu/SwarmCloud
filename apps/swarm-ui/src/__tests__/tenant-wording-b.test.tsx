// TENANT-WORDING-B (VQ-056, part of #2391): the console says "workspace"
// wherever a reader sees the word, on the Ledger, Config › GitHub access and
// Workspace setup. BGD's decision of 2026-10-26; Admin › Tenants keeps its
// technical name, and the code's identifiers, API fields and routes do not
// change (the tenant group is still `tenant_id`, the read still `tenant_id`).
//
// The lines already asserted elsewhere moved with their tests:
// timeline.causes (park reasons), activity.timeline (not filtered by),
// ledger.modelgroups (Group by), vqa2.r5.access (the lede) and
// onboarding.checklist (Signed in). This file holds the rest.
//
// BREAK IT: put "tenant" back in any one string below -- its case fails.
//
// Fixtures only; no network. Nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import { ledgerFixture } from '../outcomes.fixture'
import { serve, visible } from './repofixture'

afterEach(() => {
  vi.unstubAllEnvs()
  window.localStorage.clear()
})

describe('Reliability grouped by workspace', () => {
  it('titles, heads and counts the rows as workspaces', async () => {
    const { ReliabilityCard } = await import('../Ledger')
    const d = ledgerFixture()
    const row = d.groups.rows[0]!
    d.groups = { ...d.groups, by: 'tenant_id', rows: [{ ...row, key: 'eng' }, { ...row, key: 'research' }], rows_total: 2 }
    const { container } = render(<ReliabilityCard data={d} group="tenant_id" platform onGroup={() => {}} />)
    expect(container.textContent).toContain('Reliability by workspace')
    expect(container.querySelector('thead th')?.textContent).toBe('Workspace')
    expect(container.textContent).toContain('2 workspaces')
    expect(container.textContent).not.toMatch(/tenant/i)
  })
})

describe('Workspace setup', () => {
  const STEPS = (via: Record<string, unknown>) => [
    { step: 'signed_in', state: 'done', evidence: { tenant_id: 'eng' } },
    { step: 'github_connected', state: 'done', evidence: via },
    { step: 'app_installed', state: 'done', evidence: { needed: false, via: 'tenant', kind: 'classic_pat' } },
    { step: 'orgs_enabled', state: 'todo', evidence: { waiting_for: 'repos_chosen' } },
    { step: 'repos_chosen', state: 'todo', evidence: {} },
    { step: 'access_verified', state: 'todo', evidence: { waiting_for: 'repos_chosen' } },
    { step: 'ready', state: 'todo', evidence: { waiting_for: 'repos_chosen' } },
  ]

  function answer(via: Record<string, unknown>) {
    const steps = STEPS(via).map((s) => ({ code: null, copy: null, checked_at: null, issues: [], ...s }))
    const body = {
      tenant_id: 'eng',
      user: 'dev@swarm.example.com',
      user_hash: '89abcdef01234567',
      derived_at: new Date().toISOString(),
      next_step: 'orgs_enabled',
      complete: false,
      source: 'derived on this read',
      steps,
    }
    serve((m, url) => (m === 'GET' && url === '/v1/onboarding' ? { status: 200, body } : null))
  }

  it('names the workspace, never the tenant, in the steps and the foot', async () => {
    answer({ via: 'tenant', kind: 'classic_pat', forge_login: 'eng-bot' })
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { OnboardingScreen } = await import('../Onboarding')
    const { container } = render(<OnboardingScreen />)
    await screen.findByRole('list', { name: 'Setup steps' })
    const step = (name: string) => visible(container.querySelector(`li.ob-step[data-step="${name}"]`))
    expect(step('signed_in')).toContain('workspace eng')
    expect(step('github_connected')).toContain('agents act through the workspace token (@eng-bot), not as you')
    await waitFor(() => expect(visible(container.querySelector('.ob-card .c-card-foot'))).toContain('dev@swarm.example.com in workspace eng.'))
    expect(container.textContent).not.toMatch(/tenant/i)
  })
})

describe('Config › GitHub access, a typed repository', () => {
  it('says a grant that registered the repository registered it for the workspace', async () => {
    serve((m, url, body) => {
      if (m === 'PUT' && url.startsWith('/v1/access/grants/')) {
        const b = body as { repository: string; mode: 'read' | 'write' }
        const grant = {
          repo_id: url.split('/').pop(), repository: b.repository, owner: b.repository.split('/')[0], mode: b.mode,
          can_push: true, archived: false, granted_at: null, granted_by: 'dev@swarm.example.com',
          checks: { clone: { state: 'ok', code: null, checked_at: null } }, verified_at: null,
        }
        return { status: 200, body: { tenant_id: 'eng', grant, registered: true, registration_repo_id: grant.repo_id } }
      }
      return null
    })
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { TypedRepository } = await import('../Access')
    const { container } = render(<TypedRepository tenantId="eng" onChange={() => {}} />)
    fireEvent.change(screen.getByPlaceholderText('owner/repo'), { target: { value: 'example-org/example-api' } })
    fireEvent.click(screen.getByRole('button', { name: 'Grant' }))
    await waitFor(() => expect(container.textContent).toContain('example-org/example-api granted read, and registered for the workspace.'))
    expect(container.textContent).not.toMatch(/tenant/i)
  })
})
