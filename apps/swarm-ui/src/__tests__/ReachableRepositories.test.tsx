// "REPOSITORIES SWARMCLOUD MAY REACH AS YOU", OUT OF ACCESS.TSX
// (GITHUB-ACCESS-PAGE; part of #2157). BGD put the list on Config ›
// Repositories, so it is its own exported component, unchanged in behaviour:
// the same card, the same rows in the same order (failures first), the same
// cells and the same actions it drew on Work › Access. access.page.test.tsx,
// access.push-test.test.tsx and access.ask-first.test.tsx hold what each
// action does; this file holds that the extraction drew the same thing.
//
// Fixtures only; no network.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

const ID = (n: number) => `repo_${String(n).padStart(16, '0')}`

const GRANT = (n: number, repository: string, mode: 'read' | 'write', checks: Record<string, string> = {}) => ({
  repo_id: ID(n),
  repository,
  owner: repository.split('/')[0],
  mode,
  can_push: true,
  archived: false,
  granted_at: null,
  granted_by: 'dev@swarm.example.com',
  checks: Object.fromEntries(Object.entries(checks).map(([k, state]) => [k, { state, code: state === 'missing' ? 'PERMISSION_MISSING' : null, checked_at: null }])),
  verified_at: null,
})

const GRANTS = [
  GRANT(1, 'example-org/example-docs', 'read', { clone: 'ok', push: 'not_required', pull_request: 'not_required' }),
  GRANT(2, 'example-org/example-service', 'write', { clone: 'ok', push: 'missing', pull_request: 'unknown' }),
  GRANT(3, 'octo-dev/example-api', 'write', { clone: 'ok', push: 'ok', pull_request: 'ok' }),
]

const rowOf = (repository: string) => document.querySelector(`li.ac-grant[data-repo="${repository}"]`) as HTMLElement

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('ReachableRepositories', () => {
  it('draws the card and the rows Access drew: failures first, each check, the actions', async () => {
    vi.resetModules()
    const { ReachableRepositories } = await import('../ReachableRepositories')
    render(<ReachableRepositories grants={GRANTS as never} onChange={vi.fn()} />)
    // MUTATION: retitle the card (it is the name BGD gave the Repositories section).
    expect(screen.getByRole('heading', { level: 2, name: 'Repositories SwarmCloud may reach as you' })).toBeTruthy()
    const grid = screen.getByRole('list', { name: 'Your granted repositories, failures first' })
    // MUTATION: sort by name only (drop grantRank from the sort).
    expect([...grid.querySelectorAll('li.ac-grant')].map((li) => [(li as HTMLElement).dataset.repo, (li as HTMLElement).dataset.rank])).toEqual([
      ['example-org/example-service', '0'],
      ['example-org/example-docs', '2'],
      ['octo-dev/example-api', '2'],
    ])
    const svc = rowOf('example-org/example-service')
    // MUTATION: drop the CHECKS cells from a row.
    expect(svc.querySelector('[data-check="push"] [data-cap]')?.getAttribute('data-cap')).toBe('missing')
    expect(svc.querySelector('[data-check="pull_request"] [data-cap]')?.getAttribute('data-cap')).toBe('unknown')
    const docs = rowOf('example-org/example-docs')
    expect(docs.querySelector('[data-check="push"] .c-dash')?.getAttribute('aria-label')).toBe('Push is not needed for a read grant')
    // MUTATION: offer Push test on a read grant.
    expect(within(svc).getAllByRole('button').map((b) => b.textContent)).toEqual(['Verify', 'Push test', 'Remove'])
    expect(within(docs).getAllByRole('button').map((b) => b.textContent)).toEqual(['Verify', 'Remove'])
    expect(within(svc).getAllByRole('radio').map((b) => [b.textContent, b.getAttribute('aria-checked')])).toEqual([['Read', 'false'], ['Write', 'true']])
    expect(screen.getByRole('button', { name: 'Re-check all' })).toBeTruthy()
    // MUTATION: drop the card's foot.
    expect(visible(document.querySelector('.ac-grants'))).toContain('An agent for a repository not on this list is refused at submission.')
  })

  it('says no repository is chosen yet when there are no grants, and Re-check all is off', async () => {
    vi.resetModules()
    const { ReachableRepositories } = await import('../ReachableRepositories')
    render(<ReachableRepositories grants={[]} onChange={vi.fn()} />)
    // MUTATION: drop the empty state.
    expect(screen.getByText('No repository chosen yet')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Re-check all' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('reads GET /v1/access itself when mounted as ReachableRepositoriesRead, for Config › Repositories', async () => {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const calls = serve((m, url) => (m === 'GET' && url === '/v1/access' ? { status: 200, body: { tenant_id: 'eng', connection: null, orgs: [], grants: GRANTS } } : null))
    const { ReachableRepositoriesRead } = await import('../ReachableRepositories')
    render(<ReachableRepositoriesRead />)
    // MUTATION: render the card from an empty list instead of the read's grants.
    const grid = await screen.findByRole('list', { name: 'Your granted repositories, failures first' })
    expect(grid.querySelectorAll('li.ac-grant')).toHaveLength(3)
    expect(calls.map((c) => c.url)).toEqual(['/v1/access'])
  })
})
