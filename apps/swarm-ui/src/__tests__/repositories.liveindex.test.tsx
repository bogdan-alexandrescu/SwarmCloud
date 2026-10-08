// WORK › REPOSITORIES › ONE REPOSITORY, READ AGAINST THE LIVE API'S SHAPES
// (QA pass G4, 2026-10-07, on swarm.saga.xyz).
//
// Every response here is shaped as the deployed API answers it, not as the
// design first guessed it:
//
//   * G4-01 `GET …/index?format=json` answers `{index: <metadata>, document:
//     <the index>, …}`; the page read the metadata as the document and drew
//     "Modules 0", "The index lists no modules", "The index maps no tests";
//   * G4-02 a test-map edge is `{source, test, evidence, command}`, one test
//     per edge, and many edges share one source: rows group by source, with
//     every test, its evidence and its command, and no React key collision;
//   * G4-03 `index.coverage` on a registration is counts (`modules`,
//     `modules_with_tests`, `test_map_edges`, `always_tests`), not a 0-1
//     ratio: "20 of 83 modules", never a dash that says it was not reported,
//     and never a percentage labelled as source files;
//   * G4-12 the graph draws fewer modules than the index lists, and says why;
//   * G4-13 the languages come from the index document when the languages
//     route is not served;
//   * G4-15 an inherited selection policy never sits beside "The policy was
//     not served."

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { coverageOf, liveIndex, repo, serve, sha, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const ID = 'repo_3333333333333333'

function detail(pol: Record<string, unknown> = { policy: 'P3', mode: 'X2', inherited_from_tenant: false }) {
  return {
    repository: {
      ...repo({ repo_id: ID, repo: 'swarmcloud' }, { current_sha: sha('9f8e7d6'), head_sha: sha('9f8e7d6'), coverage: coverageOf(83, 20, 1339, 7) }),
      graph: { depth: 3, min_confidence: 0.2 },
      selection_policy: pol,
    },
    index_runs: [],
    used_by: [],
  }
}

const GRAPH = {
  index_sha: sha('9f8e7d6'),
  head_sha: sha('9f8e7d6'),
  behind_by: 0,
  stale: false,
  cluster: 'module',
  modules: [
    { id: 'apps/common/swarm_common', modules: 1, symbols: 300, tests: 0, hot_spot_changes: 4, test_reach: 0.7, languages: ['python'] },
    { id: 'apps/swarm-api/swarm_api', modules: 1, symbols: 900, tests: 0, hot_spot_changes: 30, test_reach: 0.5, languages: ['python'] },
  ],
  edges: [{ from: 'apps/swarm-api/swarm_api', to: 'apps/common/swarm_common', weight: 40, kinds: { call: 40 }, max_confidence: 0.95 }],
  counts: { files: 470, symbols: 9000, edges: 41000 },
  truncated: [],
}

type R = { status: number; body: unknown }

function routes(over: { detail?: R; index?: R; languages?: R } = {}) {
  return serve((m, url) => {
    if (m !== 'GET') return null
    const u = new URL(url, 'http://x')
    if (u.pathname === `/v1/repositories/${ID}`) return over.detail ?? { status: 200, body: detail() }
    if (u.pathname === `/v1/repositories/${ID}/index` && u.searchParams.get('format') === 'json') return over.index ?? { status: 200, body: liveIndex(ID) }
    if (u.pathname === `/v1/repositories/${ID}/languages`) return over.languages ?? null
    if (u.pathname === `/v1/repositories/${ID}/graph`) return { status: 200, body: GRAPH }
    return null
  })
}

async function mount(tab: string | null = null) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RepositoriesScreen } = await import('../Repositories')
  const view = tab === null ? `repo=${ID}` : `repo=${ID}&tab=${tab}`
  render(<RepositoriesScreen view={view} go={vi.fn()} />)
  await waitFor(() => expect(document.querySelector('h1')?.textContent).toBe('example-org/swarmcloud'), WAIT)
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.restoreAllMocks()
})

describe('G4-01: the page reads the index from `document`, its size and cut from `index`', () => {
  it('Overview counts the modules and entry points the document lists, never 0', async () => {
    routes()
    await mount()
    await waitFor(() => expect(document.querySelector('.ur-kv')).not.toBeNull(), WAIT)
    const kv = visible(document.querySelector('.ur-kv'))
    expect(kv).toContain('Modules 3')
    expect(kv).toContain('Entry points 2')
    expect(kv).toContain('apps/swarm-api/swarm_api/main.py')
    expect(kv).not.toContain('none found')
    // The metadata's figures still come from the metadata.
    expect(kv).toContain('Index 393 KiB')
    expect(kv).toContain('truncated: test_map')
    const body = visible(document.querySelector('.ur-detail'))
    expect(body).not.toContain('The index lists no modules.')
    expect(body).not.toContain('The index lists no hot-spots.')
    expect(body).toContain('3 in the index')
    expect(document.querySelectorAll('.ur-mrow')).toHaveLength(3)
    expect(visible(document.querySelector('.ur-mrow'))).toContain('apps/common/swarm_common')
  })

  it('Hot-spots lists the document\'s hot-spots, and what changed with the hottest', async () => {
    routes()
    await mount('hot-spots')
    await waitFor(() => expect(document.querySelectorAll('.ur-hs')).toHaveLength(2), WAIT)
    expect(visible(document.querySelectorAll('.ur-hs')[0]!)).toContain('apps/swarm-ui/src/App.tsx')
    expect(visible(document.querySelectorAll('.ur-hs')[0]!)).toContain('61')
    expect(visible(document.querySelector('.ur-cochange'))).toContain('apps/swarm-ui/src/App.tsx ↔ apps/swarm-ui/src/styles/app.css')
  })

  it('an index envelope with no document (nothing promoted yet) is "No index yet", not an empty index', async () => {
    const empty = { ...liveIndex(ID), index: null, document: null, produced_by: null }
    routes({ index: { status: 200, body: empty } })
    await mount()
    await waitFor(() => expect(document.body.textContent).toContain('No index yet'), WAIT)
    expect(document.body.textContent).not.toContain('Modules 0')
    expect(document.body.textContent).not.toContain('The index lists no modules.')
  })
})

describe('G4-02: test-map edges are one test each, grouped by their source', () => {
  it('draws one row per source with every test, its evidence and its command', async () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
    routes()
    await mount('test-map')
    await waitFor(() => expect(document.querySelectorAll('.ur-tmap-row')).toHaveLength(2), WAIT)
    const [common, api] = Array.from(document.querySelectorAll<HTMLElement>('.ur-tmap-row'))
    expect(visible(common!.querySelector('code'))).toBe('apps/common/swarm_common/**')
    const tests = Array.from(common!.querySelectorAll<HTMLElement>('.ur-test'))
    expect(tests.map((t) => visible(t.querySelector('code')))).toEqual([
      'tests/unit/common/test_models.py',
      'tests/unit/common/test_state.py',
      'tests/unit/scheduler/test_admission.py',
    ])
    expect(tests.map((t) => visible(t.querySelector('.c-chip')))).toEqual(['import', 'import', 'co-change'])
    expect(tests[0]!.getAttribute('title')).toBe('Run: uv run pytest tests/unit/common/test_models.py')
    expect(tests[2]!.getAttribute('title')).toBe('No command recorded for this test')
    expect(visible(api!)).toContain('tests/unit/control_plane/test_repo_index.py')
    expect(visible(api!)).toContain('naming')
    expect(common!.querySelector('.c-dash')).toBeNull()
    expect(visible(document.querySelector('.ur-tmap'))).not.toContain('The index maps no tests.')
    // The document says its test map was cut at the size ceiling: the tab says so.
    expect(visible(document.querySelector('.ur-tmap'))).toContain('cut at its size ceiling')
    const keyWarnings = errors.mock.calls.filter((c) => String(c[0]).includes('same key'))
    expect(keyWarnings).toEqual([])
  })
})

describe('G4-03: tests mapped is counts of modules, as the API serves it', () => {
  it('the Overview tile and the Test map tab say "20 of 83 modules" with the edges, never a dash', async () => {
    routes()
    await mount()
    await waitFor(() => expect(document.querySelector('.ur-kv')).not.toBeNull(), WAIT)
    const kv = visible(document.querySelector('.ur-kv'))
    expect(kv).toContain('Tests mapped 20 of 83 modules')
    expect(kv).toContain('1,339 edges · 7 always-run')
    expect(kv).not.toContain('%')
    const tile = Array.from(document.querySelectorAll('.ur-tile')).find((t) => visible(t).startsWith('Tests mapped'))!
    expect(tile.querySelector('.c-dash')).toBeNull()
    const tab = Array.from(document.querySelectorAll<HTMLElement>('.ur-tabs a')).find((a) => visible(a.querySelector('.c-tab-label')) === 'Test map')!
    expect(visible(tab.querySelector('em'))).toBe('1,339')
  })

  it('the Test map tab\'s bar is filled to 20/83 and named for modules, not source files', async () => {
    routes()
    await mount('test-map')
    await waitFor(() => expect(document.querySelector('.ur-tmap .ur-tm')).not.toBeNull(), WAIT)
    const tm = document.querySelector<HTMLElement>('.ur-tmap .ur-tm')!
    expect(visible(tm)).toContain('20 of 83 modules')
    expect(visible(tm)).toContain('1,339 edges · 7 always-run')
    const bar = tm.querySelector<HTMLElement>('.ur-bar')!
    expect(bar.classList.contains('is-unmeasured')).toBe(false)
    expect(bar.querySelector('i')!.style.width).toBe('24%')
    expect(bar.getAttribute('aria-label')).toBe('Tests mapped: 20 of 83 modules have a test edge')
    expect(tm.textContent).not.toContain('source files')
  })
})

describe('G4-12: the graph says why it draws fewer modules than the index lists', () => {
  it('names both counts and what a graph node is', async () => {
    routes()
    await mount('graph')
    await waitFor(() => expect(document.querySelector('.rg-foot')).not.toBeNull(), WAIT)
    expect(visible(document.querySelector('.rg-foot'))).toContain(
      'the index lists 83 modules; the graph draws the 2 directories that hold parsed code symbols',
    )
  })
})

describe('G4-13: languages fall back to the index document', () => {
  it('reads the languages route, and draws its rows when it answers', async () => {
    const calls = routes({
      languages: { status: 200, body: { repo_id: ID, languages: [{ language: 'go', files: 12, grammar: 'tree-sitter-go', server: 'gopls', status: 'ok' }], source: 'graph' } },
    })
    await mount('settings')
    await waitFor(() => expect(document.querySelectorAll('.ur-lang')).toHaveLength(1), WAIT)
    expect(visible(document.querySelector('.ur-lang'))).toContain('go')
    expect(calls.some((c) => c.url === `/v1/repositories/${ID}/languages`)).toBe(true)
  })

  it('when the route is not served, draws the document\'s languages and says where they came from', async () => {
    routes()
    await mount('settings')
    await waitFor(() => expect(document.querySelectorAll('.ur-lang')).toHaveLength(2), WAIT)
    const rows = Array.from(document.querySelectorAll('.ur-lang')).map((l) => visible(l))
    expect(rows[0]).toContain('python')
    expect(rows[0]).toContain('410 files')
    expect(rows[1]).toContain('hcl')
    expect(rows[1]).toContain('ast and import edges only')
    expect(document.querySelector('[data-notserved="GET /v1/repositories/{repo_id}/languages"]')).toBeNull()
    expect(visible(document.querySelector('.ur-lang-src'))).toBe('Read from the index document: the languages route is not served by this API.')
  })

  it('when neither serves languages, the region still says "not served yet"', async () => {
    routes({ index: { status: 404, body: { detail: 'Not Found' } } })
    await mount('settings')
    await waitFor(() => expect(document.querySelector('[data-notserved="GET /v1/repositories/{repo_id}/languages"]')).not.toBeNull(), WAIT)
    expect(document.querySelectorAll('.ur-lang')).toHaveLength(0)
  })
})

describe('G4-15: the selection policy never says "inherited" and "not served" together', () => {
  it('an inherited policy the tenant default was not served for says that, once', async () => {
    routes({ detail: { status: 200, body: detail({ policy: null, mode: null, inherited_from_tenant: true }) } })
    await mount('settings')
    const card = document.querySelector<HTMLElement>('.ur-policy')!
    expect(visible(card.querySelector('.c-chip'))).toBe('from the tenant default')
    expect(visible(card)).not.toContain('The policy was not served.')
    expect(visible(card)).toContain("Inherited from the tenant default, which this page does not read")
  })

  it('a policy that was not served draws no inheritance chip', async () => {
    routes({ detail: { status: 200, body: detail({ policy: null, mode: null, inherited_from_tenant: false }) } })
    await mount('settings')
    const card = document.querySelector<HTMLElement>('.ur-policy')!
    expect(visible(card)).toContain('The policy was not served.')
    expect(card.querySelector('.c-chip')).toBeNull()
  })
})
