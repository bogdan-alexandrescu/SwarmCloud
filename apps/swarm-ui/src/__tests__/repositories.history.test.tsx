// WORK › REPOSITORIES › ONE REPOSITORY: HOW MUCH HISTORY THE INDEX READ (QA G4-06).
//
// A one-commit-deep clone has no history inside the 90-day window: before
// #786 the extractor read it as "every file changed once", and since then it
// says co-change is not known. The API judges the index's
// `extractor.history` and serves it as `index.history`
// (`swarm_api/repoindex.py` `history_depth`). The Hot-spots and Test map
// tabs put a dash where co-change evidence was impossible, with the API's
// reason behind it -- never "The index lists no hot-spots", which reads as a
// repository nobody changes -- and say "a lower bound" when the history
// stops inside the window.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { coverageOf, liveIndex, repo, serve, sha, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const ID = 'repo_4444444444444444'

const SHALLOW_WHY = 'the checkout is shallow and holds no commit inside the 90-day window'
const IMPOSSIBLE = { recorded: true, available: false, shallow: true, window_days: 90, window_covered: false, commits: 0, co_change: 'impossible', reason: SHALLOW_WHY }
const PARTIAL = {
  ...IMPOSSIBLE,
  available: true,
  commits: 12,
  co_change: 'partial',
  reason: "the indexer's checkout is shallow: its history stops inside the 90-day window after 12 commits, so change counts and co-change are a lower bound",
}
const KNOWN = { ...IMPOSSIBLE, available: true, shallow: false, window_covered: true, commits: 480, co_change: 'known', reason: null }

function routes(history: Record<string, unknown> | null, doc: Record<string, unknown> = {}) {
  const detail = {
    repository: repo({ repo_id: ID, repo: 'swarmcloud' }, { current_sha: sha('9f8e7d6'), head_sha: sha('9f8e7d6'), coverage: coverageOf(40, 31, 1339, 7) }),
    index_runs: [],
    used_by: [],
  }
  const index = liveIndex(ID, doc, history === null ? {} : { history })
  return serve((m, url) => {
    if (m !== 'GET') return null
    const u = new URL(url, 'http://x')
    if (u.pathname === `/v1/repositories/${ID}`) return { status: 200, body: detail }
    if (u.pathname === `/v1/repositories/${ID}/index` && u.searchParams.get('format') === 'json') return { status: 200, body: index }
    return null
  })
}

async function mount(tab: string) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RepositoriesScreen } = await import('../Repositories')
  render(<RepositoriesScreen view={`repo=${ID}&tab=${tab}`} go={vi.fn()} />)
  await waitFor(() => expect(document.querySelector('h1')?.textContent).toBe('example-org/swarmcloud'), WAIT)
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.restoreAllMocks()
})

describe('G4-06: a shallow history is a dash with a reason, not an empty list', () => {
  it('Hot-spots: no commit in the window is a dash carrying the API\'s reason', async () => {
    routes(IMPOSSIBLE, { hot_spots: [] })
    await mount('hot-spots')
    await waitFor(() => expect(document.querySelector('.ur-history')).not.toBeNull(), WAIT)
    const dash = document.querySelector('.ur-history .c-dash')
    expect(dash?.getAttribute('title')).toBe(SHALLOW_WHY)
    expect(visible(document.querySelector('.ur-history'))).toContain('Hot-spots and co-change')
    expect(document.body.textContent).not.toContain('The index lists no hot-spots.')
  })

  it('Test map: co-change evidence is a dash with the same reason', async () => {
    routes(IMPOSSIBLE)
    await mount('test-map')
    await waitFor(() => expect(document.querySelector('.ur-history')).not.toBeNull(), WAIT)
    expect(document.querySelector('.ur-history .c-dash')?.getAttribute('title')).toBe(SHALLOW_WHY)
    expect(visible(document.querySelector('.ur-history'))).toContain('Co-change evidence')
    // The edges are still drawn: the dash is about co-change only.
    expect(document.querySelectorAll('.ur-tmap-row').length).toBeGreaterThan(0)
  })

  it('a history that stops inside the window says the counts are a lower bound', async () => {
    routes(PARTIAL)
    await mount('hot-spots')
    await waitFor(() => expect(document.querySelectorAll('.ur-hs')).toHaveLength(2), WAIT)
    expect(visible(document.querySelector('.ur-history'))).toContain('lower bound')
    expect(document.querySelector('.ur-history .c-dash')).toBeNull()
  })

  it('a whole window, or an API that serves no judgement, draws no history line', async () => {
    for (const history of [KNOWN, null]) {
      document.body.innerHTML = ''
      routes(history)
      await mount('hot-spots')
      await waitFor(() => expect(document.querySelectorAll('.ur-hs')).toHaveLength(2), WAIT)
      expect(document.querySelector('.ur-history')).toBeNull()
    }
  })
})
