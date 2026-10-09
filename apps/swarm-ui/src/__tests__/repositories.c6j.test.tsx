// QA ON SWARMCLOUD, 2026-10-07 (#503), lane C6J: the repository page's Test
// map, Graph, Settings and tab strip.
//
// WHAT EACH CASE HOLDS:
//   * G4-08 the Test map tab asks the API's selection (`POST .../tests:select`,
//     repoindex.py `select_tests`) "which tests cover these paths?" and draws
//     its answer as Test · Because · Evidence: the tests, the always-run tests
//     with where they are declared, and every unmapped path with the fallback
//     command; below it the edges grouped by source glob (the `/**` dimmed),
//     then "Always run (n)" and "Suites (n)" from the document;
//   * G4-09 the symbol search runs as you type (300 ms, 3 characters) and has
//     a Find button; each symbol shows `path:line`; the graph's sub-mode is
//     "Tests reaching a symbol", and each Test map row links to it;
//   * G4-11 node labels that would overprint are moved or hidden until hover;
//   * G4-14 the full index run is a labelled button beside Index now, not an
//     enabled control on the tab that says nothing there can be changed;
//   * G4-19 the tab strip scrolls its active tab into view and fades the edge
//     that has more tabs behind it;
//   * G4-20 "Index runs are not served", not "index runs is not served".

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { liveIndex, repo, serve, sha, visible } from './repofixture'
import { placeLabels, type LabelSpot } from '../RepoGraphLayout'
import { globParts, pathsOf } from '../RepositoriesData'
import { stripFade, scrollToShow } from '../RepositoriesParts'

const WAIT = { timeout: 4000 }
const ID = 'repo_0a1b2c3d4e5f6071'
const REPO_CSS = readFileSync(resolve(__dirname, '../styles/repositories.css'), 'utf8')
const GRAPH_CSS = readFileSync(resolve(__dirname, '../styles/repograph.css'), 'utf8')

const DETAIL = {
  repository: { ...repo({ repo_id: ID }), graph: { depth: 3, min_confidence: 0.2 }, selection_policy: { policy: 'P3', mode: 'X2', inherited_from_tenant: false } },
  index_runs: [],
  used_by: [],
}

const ALWAYS = [
  { target: 'tests/unit/common/test_contract.py', because: 'the frozen contract is checked on every change', source: 'CLAUDE.md', command: 'uv run pytest tests/unit/common -q' },
  { target: 'tests/unit/scripts', because: 'guards the repository', source: null, command: null },
]
const SUITES = [
  { root: 'tests/unit', framework: 'pytest', command: 'uv run pytest tests/unit -q', needs: [], covers: ['apps/**'] },
  { root: 'tests/integration', framework: 'pytest', command: 'uv run pytest tests/integration -q', needs: ['emulator'], covers: [] },
  { root: 'apps/swarm-ui', framework: 'vitest', command: 'npx vitest run', needs: [], covers: ['apps/swarm-ui/**'] },
]

const INDEX = liveIndex(ID, { always_tests: ALWAYS, test_layout: SUITES })

/** `POST .../tests:select` as `select_repository_tests` answers it. */
const SELECTION = {
  repo_id: ID,
  tenant_id: 'eng',
  index_sha: sha('9f8e7d6'),
  head_sha: sha('9f8e7d6'),
  behind_by: 0,
  stale: false,
  tests: [
    { target: 'tests/unit/common/test_models.py', command: 'uv run pytest tests/unit/common/test_models.py', because: ['apps/common/swarm_common/models.py'], evidence: 'import' },
  ],
  always: ALWAYS.map((a) => ({ target: a.target, command: a.command, because: a.because })),
  unmapped: ['apps/agent-worker/worker/loop.py'],
  fallback: 'uv run pytest tests/unit -q',
  fallback_covers_every_unmapped_path: true,
}

const GRAPH = {
  index_sha: sha('a1b2c3d'),
  head_sha: sha('a1b2c3d'),
  behind_by: 0,
  stale: false,
  cluster: 'module',
  modules: [
    { id: 'src/core/orders.py', modules: 1, symbols: 14, tests: 0, hot_spot_changes: 23, test_reach: 0.78, languages: ['python'] },
    { id: 'src/core/money.py', modules: 1, symbols: 6, tests: 0, hot_spot_changes: 2, test_reach: 0.9, languages: ['python'] },
  ],
  edges: [{ from: 'src/core/orders.py', to: 'src/core/money.py', weight: 5, kinds: { call: 5 }, max_confidence: 0.95 }],
  counts: { files: 20, symbols: 300, edges: 900 },
  truncated: [],
}

const ADMIT = 'apps/reconciler/reconciler/loop.py#Reconciler._admit'
const SEARCH = {
  q: 'Reconciler',
  symbols: [{ id: ADMIT, kind: 'method', path: 'apps/reconciler/reconciler/loop.py', start_line: 212, end_line: 260, language: 'python' }],
  more: false,
}

type R = { status: number; body: unknown }

function routes(over: { detail?: R; select?: R } = {}) {
  return serve((m, url, _body) => {
    const u = new URL(url, 'http://x')
    if (m === 'POST' && u.pathname === `/v1/repositories/${ID}/tests:select`) return over.select ?? { status: 200, body: SELECTION }
    if (m === 'POST' && u.pathname === `/v1/repositories/${ID}/index:run`) return { status: 202, body: { task_id: 'task_full' } }
    if (m !== 'GET') return null
    if (u.pathname === `/v1/repositories/${ID}`) return over.detail ?? { status: 200, body: DETAIL }
    if (u.pathname === `/v1/repositories/${ID}/index`) return { status: 200, body: INDEX }
    if (u.pathname === `/v1/repositories/${ID}/graph`) return { status: 200, body: GRAPH }
    if (u.pathname === `/v1/repositories/${ID}/symbols` && u.searchParams.get('q') !== null) return { status: 200, body: { ...SEARCH, q: u.searchParams.get('q') } }
    return null
  })
}

async function mount(view: string, go = vi.fn()) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RepositoriesScreen } = await import('../Repositories')
  const utils = render(<RepositoriesScreen view={view} go={go} />)
  await waitFor(() => expect(document.querySelector('h1')?.textContent).toBe('example-org/example-api'), WAIT)
  return { ...utils, go }
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.restoreAllMocks()
})

describe('G4-08: the Test map tab answers "which tests cover these paths?" from the API', () => {
  it('posts the typed paths to tests:select and draws tests, always-run and unmapped rows as Test · Because · Evidence', async () => {
    const calls = routes()
    await mount(`repo=${ID}&tab=test-map`)
    const box = await screen.findByRole('textbox', { name: 'Paths to look up' }, WAIT)
    fireEvent.change(box, { target: { value: 'apps/common/swarm_common/models.py,\n apps/agent-worker/worker/loop.py' } })
    fireEvent.click(screen.getByRole('button', { name: 'Find tests' }))
    await waitFor(() => expect(document.querySelectorAll('.ur-sel tbody tr')).toHaveLength(4), WAIT)
    const post = calls.find((c) => c.method === 'POST' && c.url === `/v1/repositories/${ID}/tests:select`)!
    expect(post.body).toEqual({ paths: ['apps/common/swarm_common/models.py', 'apps/agent-worker/worker/loop.py'] })
    const head = Array.from(document.querySelectorAll('.ur-sel thead th')).map(visible)
    expect(head).toEqual(['Test', 'Because', 'Evidence'])
    const rows = Array.from(document.querySelectorAll<HTMLElement>('.ur-sel tbody tr'))
    const cells = rows.map((r) => Array.from(r.querySelectorAll('td')).map(visible))
    expect(cells[0]![0]).toContain('tests/unit/common/test_models.py')
    expect(cells[0]![1]).toBe('apps/common/swarm_common/models.py')
    expect(cells[0]![2]).toBe('import')
    // An always-run test says it is always run and where it is declared.
    expect(rows[1]!.getAttribute('data-kind')).toBe('always')
    expect(cells[1]![1]).toContain('always run · declared in CLAUDE.md')
    expect(cells[1]![2]).toBe('declared')
    expect(rows[2]!.getAttribute('data-kind')).toBe('always')
    expect(cells[2]![1]).toContain('always run · guards the repository')
    // The unmapped path, with the suite to run instead.
    expect(rows[3]!.getAttribute('data-kind')).toBe('unmapped')
    expect(cells[3]![0]).toContain('unmapped')
    expect(cells[3]![0]).toContain('apps/agent-worker/worker/loop.py')
    expect(cells[3]![1]).toBe("no edge in the map; run the area's suite instead")
    expect(cells[3]![2]).toContain('uv run pytest tests/unit -q')
    expect(visible(document.querySelector('.ur-lookup'))).toContain(`from the test map at ${sha('9f8e7d6').slice(0, 7)}`)
  })

  it('says when no suite covers every unmapped path, and when no index has been promoted', async () => {
    routes({
      select: {
        status: 200,
        body: { ...SELECTION, tests: [], always: [], fallback: null, fallback_covers_every_unmapped_path: null, reason: 'no index has been promoted for this repository, so no path is mapped' },
      },
    })
    await mount(`repo=${ID}&tab=test-map`)
    fireEvent.change(await screen.findByRole('textbox', { name: 'Paths to look up' }, WAIT), { target: { value: 'apps/agent-worker/worker/loop.py' } })
    fireEvent.click(screen.getByRole('button', { name: 'Find tests' }))
    await waitFor(() => expect(document.querySelectorAll('.ur-sel tbody tr')).toHaveLength(1), WAIT)
    const row = document.querySelector('.ur-sel tbody tr')!
    expect(row.querySelector('.c-dash')).not.toBeNull()
    expect(visible(document.querySelector('.ur-lookup'))).toContain('no index has been promoted for this repository, so no path is mapped')
  })

  it('a selection route that is not served says so in place', async () => {
    routes({ select: { status: 404, body: { detail: 'Not Found' } } })
    await mount(`repo=${ID}&tab=test-map`)
    fireEvent.change(await screen.findByRole('textbox', { name: 'Paths to look up' }, WAIT), { target: { value: 'a/b.py' } })
    fireEvent.click(screen.getByRole('button', { name: 'Find tests' }))
    await waitFor(() => expect(document.querySelector('[data-notserved="POST /v1/repositories/{repo_id}/tests:select"]')).not.toBeNull(), WAIT)
  })

  it('groups the edges by source glob with the /** dimmed, a count, evidence chips and a copy-command button', async () => {
    routes()
    await mount(`repo=${ID}&tab=test-map`)
    await waitFor(() => expect(document.querySelectorAll('.ur-tmap-row')).toHaveLength(2), WAIT)
    const row = document.querySelector<HTMLElement>('.ur-tmap-row')!
    const glob = row.querySelector('.ur-glob')!
    expect(visible(glob)).toBe('apps/common/swarm_common/**')
    expect(visible(glob.querySelector('.ur-glob-tail'))).toBe('/**')
    expect(visible(row.querySelector('.ur-tmap-n'))).toContain('3 tests')
    expect(visible(row.querySelector('.ur-tmap-n'))).toContain('directory granularity — truncated')
    expect(within(row).getAllByRole('button', { name: /^Copy the command for / })).toHaveLength(2)
    // A file-level source has no granularity note.
    const file = document.querySelectorAll<HTMLElement>('.ur-tmap-row')[1]!
    expect(visible(file.querySelector('.ur-tmap-n'))).toBe('1 test')
    expect(file.querySelector('.ur-glob-tail')).toBeNull()
  })

  it('lists "Always run (n)" with where each is declared, and "Suites (n)" with their commands', async () => {
    routes()
    await mount(`repo=${ID}&tab=test-map`)
    await waitFor(() => expect(document.querySelectorAll('.ur-always-row')).toHaveLength(2), WAIT)
    const heads = Array.from(document.querySelectorAll('.ur-tmap .ur-subh')).map(visible)
    expect(heads).toContain('Always run (2)')
    expect(heads).toContain('Suites (3)')
    const always = visible(document.querySelector('.ur-always-row'))
    expect(always).toContain('tests/unit/common/test_contract.py')
    expect(always).toContain('the frozen contract is checked on every change')
    expect(always).toContain('declared in CLAUDE.md')
    expect(always).toContain('uv run pytest tests/unit/common -q')
    expect(document.querySelectorAll<HTMLElement>('.ur-always-row')[1]!.querySelector('.c-dash')).not.toBeNull()
    const suites = Array.from(document.querySelectorAll<HTMLElement>('.ur-suite-row')).map(visible)
    expect(suites).toHaveLength(3)
    expect(suites[1]).toContain('tests/integration')
    expect(suites[1]).toContain('pytest')
    expect(suites[1]).toContain('needs emulator')
    expect(suites[1]).toContain('uv run pytest tests/integration -q')
  })

  it('reads paths split by commas, spaces and new lines, once each; a glob splits at its /**', () => {
    expect(pathsOf(' a/b.py, c/d.py\n\na/b.py  e.ts ')).toEqual(['a/b.py', 'c/d.py', 'e.ts'])
    expect(globParts('apps/common/swarm_common/**')).toEqual({ stem: 'apps/common/swarm_common', tail: '/**' })
    expect(globParts('src/state/cart.ts')).toEqual({ stem: 'src/state/cart.ts', tail: null })
    expect(REPO_CSS).toMatch(/\.ur-glob-tail \{[^}]*opacity/)
  })
})

describe('G4-09: the symbol search answers as you type, says where each symbol is, and the sub-mode says what it answers', () => {
  it('searches 300 ms after typing 3 characters, not before; and Find searches at once', async () => {
    const calls = routes()
    await mount(`repo=${ID}&tab=graph`)
    fireEvent.click(within(await screen.findByRole('radiogroup', { name: 'Graph view' }, WAIT)).getByRole('radio', { name: 'Tests reaching a symbol' }))
    const box = within(document.querySelector('.rg-testview')!).getByRole('searchbox', { name: 'Find a symbol' })
    const asked = () => calls.filter((c) => c.url.includes('/symbols?')).map((c) => new URL(c.url, 'http://x').searchParams.get('q'))
    fireEvent.change(box, { target: { value: 'Re' } })
    await act(() => new Promise((r) => setTimeout(r, 400)))
    expect(asked()).not.toContain('Re')
    fireEvent.change(box, { target: { value: 'Reconciler' } })
    expect(asked()).not.toContain('Reconciler')
    await waitFor(() => expect(asked()).toContain('Reconciler'), WAIT)
    // Each symbol says where it is before it is clicked.
    await waitFor(() => expect(document.querySelectorAll('.rg-testview .rg-sym')).toHaveLength(1), WAIT)
    expect(visible(document.querySelector('.rg-testview .rg-sym small'))).toContain('apps/reconciler/reconciler/loop.py:212')
    fireEvent.change(box, { target: { value: 'ad' } })
    fireEvent.click(within(document.querySelector('.rg-testview')!).getByRole('button', { name: 'Find' }))
    await waitFor(() => expect(asked()).toContain('ad'), WAIT)
  })

  it('names the sub-mode "Tests reaching a symbol", never a second "Test map"', async () => {
    routes()
    await mount(`repo=${ID}&tab=graph`)
    const group = await screen.findByRole('radiogroup', { name: 'Graph view' }, WAIT)
    expect(within(group).getAllByRole('radio').map(visible)).toEqual(['Modules', 'Call graph', 'Tests reaching a symbol'])
  })

  it('each Test map row links to the sub-mode, searching its directory', async () => {
    const go = vi.fn()
    routes()
    await mount(`repo=${ID}&tab=test-map`, go)
    await waitFor(() => expect(document.querySelectorAll('.ur-tmap-row')).toHaveLength(2), WAIT)
    const link = within(document.querySelector<HTMLElement>('.ur-tmap-row')!).getByRole('link', { name: 'Tests reaching a symbol' })
    fireEvent.click(link)
    expect(go).toHaveBeenCalledWith(`work/repositories?repo=${ID}&tab=graph&view=tests&q=apps%2Fcommon%2Fswarm_common`)
  })

  it('the graph opens on that sub-mode and runs that search', async () => {
    const calls = routes()
    await mount(`repo=${ID}&tab=graph&view=tests&q=apps%2Freconciler`)
    await waitFor(() => expect(document.querySelector('.rg-testview')).not.toBeNull(), WAIT)
    await waitFor(() => expect(calls.some((c) => c.url.includes('/symbols?') && new URL(c.url, 'http://x').searchParams.get('q') === 'apps/reconciler')).toBe(true), WAIT)
  })
})

describe('G4-11: graph labels do not overprint', () => {
  const spot = (id: string, x: number, y: number, weight: number, text = id): LabelSpot => ({ id, x, y, r: 10, text, weight })

  it('keeps the busier label below its node, moves the other above, and hides a third that fits nowhere', () => {
    const p = placeLabels([spot('apps/quota-broker', 300, 100, 1, 'apps/quota-broker (1)'), spot('apps/agent-worker', 310, 104, 4, 'apps/agent-worker (2)'), spot('apps/x', 305, 102, 0, 'apps/x-also-close (3)')])
    expect(p.get('apps/agent-worker')!.side).toBe('below')
    expect(p.get('apps/quota-broker')!.side).toBe('above')
    expect(p.get('apps/quota-broker')!.y).toBeLessThan(100)
    expect(p.get('apps/x')!.side).toBe('hidden')
  })

  it('leaves labels apart from each other where they are', () => {
    const p = placeLabels([spot('a', 100, 100, 1), spot('b', 400, 300, 1)])
    expect([p.get('a')!.side, p.get('b')!.side]).toEqual(['below', 'below'])
  })

  it('draws a hidden label only on hover, focus or selection', async () => {
    expect(GRAPH_CSS).toMatch(/\.rg-node text\.is-hidden \{[^}]*opacity: 0/)
    expect(GRAPH_CSS).toMatch(/\.rg-node:hover text\.is-hidden/)
    routes()
    await mount(`repo=${ID}&tab=graph`)
    await waitFor(() => expect(document.querySelectorAll('.rg-canvas .rg-node text[data-label]')).toHaveLength(2), WAIT)
  })
})

describe('G4-14: the full index run is a labelled head action, not a control on the locked Settings tab', () => {
  it('Settings carries no enabled mutating button; the head queues a full run, saying so', async () => {
    const calls = routes()
    await mount(`repo=${ID}&tab=settings`)
    await waitFor(() => expect(document.querySelector('.ur-locked')).not.toBeNull(), WAIT)
    expect(screen.queryByRole('button', { name: /Re-run LSP pass/ })).toBeNull()
    const cols = document.querySelector<HTMLElement>('.ur-cols')!
    expect(within(cols).queryAllByRole('button').filter((b) => !(b as HTMLButtonElement).disabled && b.getAttribute('aria-checked') === null)).toEqual([])
    const full = screen.getByRole('button', { name: 'Queue full index run (re-resolves with LSP)' })
    // Beside Index now, in the page head.
    const head = full.closest('.c-phead')!
    expect(head).not.toBeNull()
    expect(within(head as HTMLElement).getByRole('button', { name: 'Index now' })).toBeTruthy()
    fireEvent.click(full)
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url === `/v1/repositories/${ID}/index:run`)).toBe(true), WAIT)
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ kind: 'full' })
  })
})

describe('G4-19: the tab strip shows there is more of it', () => {
  it('fades the edge with tabs behind it', () => {
    expect(stripFade({ scrollLeft: 0, scrollWidth: 390, clientWidth: 390 })).toBe('none')
    expect(stripFade({ scrollLeft: 0, scrollWidth: 800, clientWidth: 390 })).toBe('end')
    expect(stripFade({ scrollLeft: 410, scrollWidth: 800, clientWidth: 390 })).toBe('start')
    expect(stripFade({ scrollLeft: 100, scrollWidth: 800, clientWidth: 390 })).toBe('both')
    expect(REPO_CSS).toMatch(/\.ur-tabs\[data-fade='end'\] \{[^}]*mask-image/)
    expect(REPO_CSS).toMatch(/\.ur-tabs\[data-fade='both'\] \{[^}]*mask-image/)
  })

  it('scrolls an off-screen tab into view, and leaves a visible one where it is', () => {
    // A tab at 600-680 in a 390-wide strip at 0: scrolled so its end shows, with the fade's room.
    expect(scrollToShow({ scrollLeft: 0, clientWidth: 390 }, { left: 600, width: 80 })).toBe(600 + 80 - 390 + 32)
    expect(scrollToShow({ scrollLeft: 300, clientWidth: 390 }, { left: 100, width: 80 })).toBe(100 - 32)
    expect(scrollToShow({ scrollLeft: 0, clientWidth: 390 }, { left: 100, width: 80 })).toBeNull()
  })

  it('the page scrolls the active tab into view on mount', async () => {
    const set = vi.fn()
    vi.spyOn(Element.prototype, 'scrollLeft', 'set').mockImplementation(set)
    vi.spyOn(Element.prototype, 'clientWidth', 'get').mockReturnValue(390)
    vi.spyOn(Element.prototype, 'scrollWidth', 'get').mockReturnValue(900)
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
      const tabs = Array.from(this.parentElement?.children ?? [])
      const i = this.parentElement?.classList.contains('ur-tabs') === true ? tabs.indexOf(this) : 0
      return { left: i * 110, width: 100, top: 0, right: i * 110 + 100, bottom: 30, height: 30, x: i * 110, y: 0, toJSON: () => ({}) } as DOMRect
    })
    routes()
    await mount(`repo=${ID}&tab=used-by`)
    await waitFor(() => expect(set).toHaveBeenCalled(), WAIT)
    // Used by is the eighth tab, at 770-870.
    expect(set).toHaveBeenLastCalledWith(770 + 100 - 390 + 32)
    expect(document.querySelector('.ur-tabs')!.getAttribute('data-fade')).toBe('end')
  })
})

describe('G4-20: "not served" agrees with its subject', () => {
  it('says "Index runs are not served" and "The runs and workflows that used this index are not served"', async () => {
    const { index_runs: _r, used_by: _u, ...bare } = DETAIL
    void _r
    void _u
    routes({ detail: { status: 200, body: bare } })
    await mount(`repo=${ID}&tab=index-runs`)
    await waitFor(() => expect(document.querySelector('[data-notserved]')).not.toBeNull(), WAIT)
    expect(visible(document.querySelector('[data-notserved]'))).toContain('Index runs are not served by this API yet.')
    expect(document.body.textContent).not.toContain('runs is not served')
  })

  it('Used by agrees too', async () => {
    const { used_by: _u, ...bare } = DETAIL
    void _u
    routes({ detail: { status: 200, body: bare } })
    await mount(`repo=${ID}&tab=used-by`)
    await waitFor(() => expect(document.querySelector('[data-notserved]')).not.toBeNull(), WAIT)
    expect(visible(document.querySelector('[data-notserved]'))).toContain('The runs and workflows that used this index are not served by this API yet.')
  })
})
