// WORK › REPOSITORIES › ONE REPOSITORY › SETTINGS: the hard stops card
// (docs/schedules.md §4.4, lane S11).
//
// WHAT EACH CASE HOLDS:
//   * the card shows whether the repository is the platform's own and its
//     protected paths, in the served order, the four defaults marked as the
//     floor a member cannot remove and an added path not marked;
//   * the floor is the API's (`hard_stop_paths_floor`), never restated here;
//   * a registration that does not serve the fields (an API older than S11)
//     draws no card, rather than a confident "no" and an empty list;
//   * the card changes nothing: no write is sent from the Settings tab.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { normRepoHardStops } from '../api'
import { repo, serve, sha, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const ID = 'repo_2222222222222222'
const FLOOR = ['.github/workflows/**', 'terraform/bootstrap/**', '**/iam*.tf', 'CODEOWNERS']

function detail(fields: Record<string, unknown>) {
  return {
    repository: {
      ...repo({ repo_id: ID, repo: 'example-web' }, { current_sha: sha('9f8e7d6'), head_sha: sha('9f8e7d6') }),
      graph: { depth: 3, min_confidence: 0.2 },
      selection_policy: { policy: null, mode: null, inherited_from_tenant: true },
      ...fields,
    },
    index_runs: [],
    used_by: [],
  }
}

function routes(body: unknown) {
  return serve((m, url) => {
    if (m !== 'GET') return null
    if (url === `/v1/repositories/${ID}`) return { status: 200, body }
    return null
  })
}

async function mount() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RepositoriesScreen } = await import('../Repositories')
  render(<RepositoriesScreen view={`repo=${ID}&tab=settings`} go={vi.fn()} />)
  await waitFor(() => expect(document.querySelector('h1')?.textContent).toBe('example-org/example-web'), WAIT)
}

afterEach(() => vi.unstubAllEnvs())

describe('Settings: the hard stops card', () => {
  it('a platform repository: says so, lists the paths, and marks the four defaults as the floor', async () => {
    const calls = routes(detail({ platform: true, hard_stop_paths: [...FLOOR, 'deploy/**'], hard_stop_paths_floor: FLOOR }))
    await mount()
    await waitFor(() => expect(document.querySelector('.ur-hardstops')).not.toBeNull(), WAIT)
    const card = document.querySelector('.ur-hardstops')!
    expect(visible(card.querySelector('h2'))).toBe('Hard stops')
    expect(card.querySelector('[data-platform]')?.getAttribute('data-platform')).toBe('true')
    expect(visible(card)).toContain('Platform repository yes')
    expect(visible(card)).toContain('set and cleared by a platform admin only')
    expect(visible(card)).toContain("held for the platform owner's approval")
    const rows = Array.from(card.querySelectorAll<HTMLElement>('.ur-hardstop'))
    expect(rows.map((r) => visible(r.querySelector('code')))).toEqual([...FLOOR, 'deploy/**'])
    expect(rows.map((r) => r.getAttribute('data-floor'))).toEqual(['true', 'true', 'true', 'true', 'false'])
    expect(visible(rows[0]!)).toContain('default, cannot be removed')
    expect(visible(rows[4]!)).not.toContain('cannot be removed')
    // Shown, not changed: the Settings tab sends no write.
    expect(calls.filter((c) => c.method !== 'GET')).toEqual([])
  })

  it('any other repository: not platform, held for a second member', async () => {
    routes(detail({ platform: false, hard_stop_paths: FLOOR, hard_stop_paths_floor: FLOOR }))
    await mount()
    await waitFor(() => expect(document.querySelector('.ur-hardstops')).not.toBeNull(), WAIT)
    const card = document.querySelector('.ur-hardstops')!
    expect(card.querySelector('[data-platform]')?.getAttribute('data-platform')).toBe('false')
    expect(visible(card)).toContain('Platform repository no')
    expect(visible(card)).toContain('held for a second member of the tenant')
    expect(card.querySelectorAll('.ur-hardstop')).toHaveLength(4)
  })

  it('the floor is what the API serves: a path the API does not call a default is not marked', async () => {
    routes(detail({ platform: false, hard_stop_paths: [...FLOOR, 'x/**'], hard_stop_paths_floor: FLOOR.slice(0, 2) }))
    await mount()
    await waitFor(() => expect(document.querySelector('.ur-hardstops')).not.toBeNull(), WAIT)
    const marks = Array.from(document.querySelectorAll('.ur-hardstop')).map((r) => r.getAttribute('data-floor'))
    expect(marks).toEqual(['true', 'true', 'false', 'false', 'false'])
  })

  it('an API that does not serve the fields draws no card', async () => {
    routes(detail({}))
    await mount()
    await waitFor(() => expect(document.querySelectorAll('.ur-detail .c-card').length).toBeGreaterThan(0), WAIT)
    expect(document.querySelector('.ur-hardstops')).toBeNull()
  })
})

describe('normRepoHardStops', () => {
  it('reads the record under `repository` or bare, and refuses a partial answer', () => {
    const fields = { platform: true, hard_stop_paths: ['a', 7, 'b'], hard_stop_paths_floor: ['a'] }
    expect(normRepoHardStops({ repository: fields })).toEqual({ platform: true, hard_stop_paths: ['a', 'b'], floor: ['a'] })
    expect(normRepoHardStops(fields)).toEqual({ platform: true, hard_stop_paths: ['a', 'b'], floor: ['a'] })
    expect(normRepoHardStops({ repository: { hard_stop_paths: [] } })).toBeNull()
    expect(normRepoHardStops({ repository: { platform: 'true', hard_stop_paths: [] } })).toBeNull()
    expect(normRepoHardStops({ repository: { platform: false } })).toBeNull()
    expect(normRepoHardStops(null)).toBeNull()
  })
})
