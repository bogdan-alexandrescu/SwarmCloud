// WORK › REPOSITORIES › ONE REPOSITORY › GRAPH (repositories.html screen 8,
// pick A: a force-directed canvas with a side inspector).
//
// WHAT EACH CASE HOLDS:
//   * the repository's tabs gain Graph and Impact after Overview, in the
//     frame's order, each its own address;
//   * the Modules view draws one node per module from GET .../graph, inside a
//     soft rectangle per package, coloured by hot-spots (or test reach), with
//     the frame's legend; a module with no served figure is hatched, not cool;
//   * clicking a node opens it in the inspector with its served figures and
//     its symbols (GET .../symbols?q=); picking a symbol draws its call graph
//     (?id=&depth=&direction=) and its test map (?id=&tests=1);
//   * the depth control asks the route again at the new depth and stops at
//     1 and 6, the route's bound;
//   * the Call graph view lays callers left and callees right, each edge
//     styled by its evidence (lsp / ast / import) or as low under 0.4, with
//     the evidence legend, and an edge's evidence and confidence as its title;
//   * a route that is not there yet says so in place and names itself; an
//     index with no graph says that, not "not served";
//   * the phone's view is the inspector with a module list, the canvas hidden.
//
// MUTATIONS: drop Graph from the tabs; colour a null hot-spot count as cool;
// fetch the call graph without `depth`; let More depth pass 6; draw an ast
// edge in the lsp style; render "Not served" for no_graph -- each turns red.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor, within } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { repo, serve, sha, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const ID = 'repo_0a1b2c3d4e5f6071'
const CSS = readFileSync(resolve(__dirname, '../styles/repograph.css'), 'utf8')

const DETAIL = { repository: repo({ repo_id: ID }), index_runs: [], used_by: [] }
const FRESH = { index_sha: sha('a1b2c3d'), head_sha: sha('a1b2c3d'), behind_by: 0, stale: false, freshness: { state: 'current' } }

const GRAPH = {
  ...FRESH,
  cluster: 'module',
  modules: [
    { id: 'src/api/orders.py', modules: 1, symbols: 30, tests: 0, hot_spot_changes: 25, test_reach: 0.5, languages: ['python'] },
    { id: 'src/core/orders.py', modules: 1, symbols: 14, tests: 0, hot_spot_changes: 23, test_reach: 0.78, languages: ['python'] },
    { id: 'src/core/money.py', modules: 1, symbols: 6, tests: 0, hot_spot_changes: 2, test_reach: 0.9, languages: ['python'] },
    { id: 'workers/invoices.py', modules: 1, symbols: 9, tests: 0, hot_spot_changes: null, test_reach: null, languages: ['python'] },
  ],
  edges: [
    { from: 'src/api/orders.py', to: 'src/core/orders.py', weight: 12, kinds: { call: 12 }, max_confidence: 0.95 },
    { from: 'workers/invoices.py', to: 'src/core/orders.py', weight: 3, kinds: { call: 3 }, max_confidence: 0.6 },
    { from: 'src/core/orders.py', to: 'src/core/money.py', weight: 5, kinds: { call: 5 }, max_confidence: 0.95 },
  ],
  counts: { files: 2014, symbols: 31240, edges: 148900 },
  truncated: [],
}

const TOTAL = 'src/core/orders.py#OrderService.total'
const SEARCH = {
  ...FRESH,
  q: 'src/core/orders.py',
  symbols: [
    { id: TOTAL, kind: 'method', path: 'src/core/orders.py', start_line: 88, end_line: 131, language: 'python' },
    { id: 'src/core/orders.py#OrderService._apply_tax', kind: 'method', path: 'src/core/orders.py', start_line: 133, end_line: 150, language: 'python' },
  ],
  more: false,
}

function callGraph(depth: number, direction: string) {
  return {
    ...FRESH,
    symbol: SEARCH.symbols[0],
    depth,
    direction,
    nodes: [
      { id: TOTAL, kind: 'method', path: 'src/core/orders.py', start_line: 88, end_line: 131, distance: 0 },
      { id: 'src/api/orders.py#checkout_total', kind: 'function', path: 'src/api/orders.py', distance: 1 },
      { id: 'workers/invoices.py#InvoiceJob.run', kind: 'method', path: 'workers/invoices.py', distance: 1 },
      { id: 'src/api/routes.py#admin_recalc', kind: 'function', path: 'src/api/routes.py', distance: 2 },
      { id: 'src/core/money.py#Money.round', kind: 'method', path: 'src/core/money.py', distance: 1 },
      { id: 'src/core/tax.py#TaxTable', kind: 'class', path: 'src/core/tax.py', distance: 1 },
    ],
    edges: [
      { from: 'src/api/orders.py#checkout_total', to: TOTAL, kind: 'call', evidence: 'lsp', also_evidence: [], confidence: 0.95 },
      { from: 'workers/invoices.py#InvoiceJob.run', to: TOTAL, kind: 'call', evidence: 'ast', also_evidence: ['import'], confidence: 0.6 },
      { from: 'src/api/routes.py#admin_recalc', to: 'workers/invoices.py#InvoiceJob.run', kind: 'call', evidence: 'ast', also_evidence: [], confidence: 0.3 },
      { from: TOTAL, to: 'src/core/money.py#Money.round', kind: 'call', evidence: 'lsp', also_evidence: [], confidence: 0.95 },
      { from: TOTAL, to: 'src/core/tax.py#TaxTable', kind: 'reference', evidence: 'import', also_evidence: [], confidence: 0.4 },
    ],
    truncated: false,
  }
}

const TESTS = {
  ...FRESH,
  symbol: SEARCH.symbols[0],
  tests: [
    { test: 'tests/core/test_orders.py#test_empty_cart_total', depth: 1, confidence: 0.95, command: null },
    { test: 'tests/core/test_invoices.py#test_invoice_totals', depth: 3, confidence: 0.6, command: null },
  ],
}

type R = { status: number; body: unknown }

function routes(over: { graph?: R } = {}) {
  return serve((m, url) => {
    if (m !== 'GET') return null
    const u = new URL(url, 'http://x')
    if (u.pathname === `/v1/repositories/${ID}`) return { status: 200, body: DETAIL }
    if (u.pathname === `/v1/repositories/${ID}/graph`) return over.graph ?? { status: 200, body: GRAPH }
    if (u.pathname === `/v1/repositories/${ID}/symbols`) {
      const p = u.searchParams
      if (p.get('q') !== null) return { status: 200, body: SEARCH }
      if (p.get('id') === TOTAL && p.get('tests') === '1') return { status: 200, body: TESTS }
      if (p.get('id') === TOTAL) return { status: 200, body: callGraph(Number(p.get('depth')), p.get('direction') ?? 'both') }
    }
    return null
  })
}

async function mount(tab: string | null, go = vi.fn()) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RepositoriesScreen } = await import('../Repositories')
  const view = tab === null ? `repo=${ID}` : `repo=${ID}&tab=${tab}`
  const utils = render(<RepositoriesScreen view={view} go={go} />)
  await waitFor(() => expect(document.querySelector('h1')?.textContent).toBe('example-org/example-api'), WAIT)
  return { ...utils, go }
}

const nodes = () => Array.from(document.querySelectorAll<SVGGElement>('.rg-canvas .rg-node'))
const node = (id: string) => document.querySelector<SVGGElement>(`.rg-canvas .rg-node[data-id="${id}"]`)!
const inspector = () => document.querySelector<HTMLElement>('.rg-inspector')!
const symbolCalls = (calls: { url: string }[]) => calls.filter((c) => c.url.includes('/symbols?')).map((c) => new URL(c.url, 'http://x').searchParams)

afterEach(() => vi.unstubAllEnvs())

describe('the repository tabs carry Graph and Impact (Detail A, screens 8 and 9)', () => {
  it('lists Graph and Impact after Overview, each with its own address', async () => {
    routes()
    await mount(null)
    const tabs = Array.from(document.querySelectorAll<HTMLAnchorElement>('.ur-tabs a'))
    expect(tabs.map((t) => visible(t.querySelector('.c-tab-label')))).toEqual([
      'Overview', 'Graph', 'Impact', 'Test map', 'Hot-spots', 'Index runs', 'Settings', 'Used by',
    ])
    expect(tabs[1]!.getAttribute('href')).toBe(`/repositories/${ID}/graph`)
    expect(tabs[2]!.getAttribute('href')).toBe(`/repositories/${ID}/impact`)
  })
})

describe('the Modules view (Graph A)', () => {
  it('draws one node per module inside a rectangle per package, coloured by hot-spots', async () => {
    routes()
    await mount('graph')
    await waitFor(() => expect(nodes()).toHaveLength(4), WAIT)
    const svg = document.querySelector('.rg-canvas svg')!
    expect(svg.getAttribute('role')).toBe('img')
    expect(svg.getAttribute('aria-label')).toBe('Module dependency graph of example-org/example-api, clustered by package')
    expect(Array.from(document.querySelectorAll('.rg-cluster')).map((r) => r.getAttribute('data-cluster'))).toEqual(['src'])
    expect(node('src/api/orders.py').getAttribute('data-heat')).toBe('hot')
    expect(node('src/core/money.py').getAttribute('data-heat')).toBe('cool')
    // Not served is hatched, never "cool" (components.html A: hatching means "not measured").
    expect(node('workers/invoices.py').getAttribute('data-heat')).toBe('unmeasured')
    expect(document.querySelectorAll('.rg-canvas .rg-edge')).toHaveLength(3)
    const legend = visible(document.querySelector('.rg-legend'))
    expect(legend).toContain('hot-spot (20+ changes in 90 days)')
    expect(legend).toContain('5-19')
    expect(legend).toContain('under 5')
    expect(legend).toContain('edge width = resolved calls between modules')
    expect(visible(document.querySelector('.rg-foot'))).toContain('Index a1b2c3d · 2,014 files · 31,240 symbols · 148,900 edges')
  })

  it('recolours by test reach, with that legend', async () => {
    routes()
    await mount('graph')
    await waitFor(() => expect(nodes()).toHaveLength(4), WAIT)
    fireEvent.click(within(document.querySelector('.rg-tools')!).getByRole('radio', { name: 'test reach' }))
    expect(node('src/api/orders.py').getAttribute('data-heat')).toBe('warm')
    expect(node('src/core/orders.py').getAttribute('data-heat')).toBe('cool')
    expect(visible(document.querySelector('.rg-legend'))).toContain('tests reach under 40% of symbols')
  })

  it('opens the hottest module in the inspector first, and a clicked one after', async () => {
    const calls = routes()
    await mount('graph')
    await waitFor(() => expect(visible(inspector().querySelector('h2'))).toBe('src/api/orders.py'), WAIT)
    fireEvent.click(node('src/core/orders.py'))
    await waitFor(() => expect(visible(inspector().querySelector('h2'))).toBe('src/core/orders.py'), WAIT)
    expect(node('src/core/orders.py').getAttribute('aria-pressed')).toBe('true')
    const meta = Array.from(inspector().querySelectorAll('.rg-meta')).map(visible).join(' ')
    expect(meta).toContain('14 symbols')
    expect(meta).toContain('23 changes in 90 days')
    expect(meta).toContain('called from 2 modules')
    expect(meta).toContain('test reach 78% of symbols')
    // Lines per module are not served by the graph: a dash with its reason.
    expect(inspector().querySelector('.rg-meta .c-dash')?.getAttribute('title')).toMatch(/not served/)
    await waitFor(() => expect(inspector().querySelectorAll('.rg-sym')).toHaveLength(2), WAIT)
    expect(symbolCalls(calls).some((p) => p.get('q') === 'src/core/orders.py')).toBe(true)
  })

  it('zooms in, out and back to fit', async () => {
    routes()
    await mount('graph')
    await waitFor(() => expect(nodes()).toHaveLength(4), WAIT)
    const g = () => document.querySelector('.rg-canvas .rg-world')!.getAttribute('transform')
    expect(g()).toBe('translate(0 0) scale(1)')
    fireEvent.click(document.querySelector('button[aria-label="Zoom in"]')!)
    expect(g()).not.toBe('translate(0 0) scale(1)')
    fireEvent.click(document.querySelector('button[aria-label="Fit"]')!)
    expect(g()).toBe('translate(0 0) scale(1)')
  })
})

describe('the call graph and its depth control', () => {
  async function pickTotal() {
    const calls = routes()
    await mount('graph')
    await waitFor(() => expect(nodes()).toHaveLength(4), WAIT)
    fireEvent.click(node('src/core/orders.py'))
    await waitFor(() => expect(inspector().querySelectorAll('.rg-sym')).toHaveLength(2), WAIT)
    fireEvent.click(within(inspector()).getByRole('button', { name: /OrderService\.total/ }))
    await waitFor(() => expect(inspector().querySelectorAll('.rg-mini .rg-cnode').length).toBeGreaterThan(0), WAIT)
    return calls
  }

  it('asks for the picked symbol at depth 2, both ways, and lists the tests that reach it', async () => {
    const calls = await pickTotal()
    const asked = symbolCalls(calls).filter((p) => p.get('id') === TOTAL && p.get('tests') === null)
    expect(asked.map((p) => [p.get('depth'), p.get('direction')])).toEqual([['2', 'both']])
    expect(visible(inspector().querySelector('.rg-depth'))).toBe('2')
    await waitFor(() => expect(inspector().querySelectorAll('.rg-test')).toHaveLength(2), WAIT)
    const first = visible(inspector().querySelector('.rg-test'))
    expect(first).toContain('test_empty_cart_total')
    expect(first).toContain('depth 1 · confidence 0.95')
    expect(visible(inspector().querySelector('.rg-tests-h'))).toContain('2 tests reach it')
  })

  it('asks again one deeper on More depth and stops at 6; one shallower on Less depth and stops at 1', async () => {
    const calls = await pickTotal()
    const more = within(inspector()).getByRole('button', { name: 'More depth' })
    const less = within(inspector()).getByRole('button', { name: 'Less depth' })
    for (let i = 0; i < 6; i++) fireEvent.click(more)
    await waitFor(() => expect(visible(inspector().querySelector('.rg-depth'))).toBe('6'), WAIT)
    expect(more).toHaveProperty('disabled', true)
    const depths = () => symbolCalls(calls).filter((p) => p.get('id') === TOTAL && p.get('tests') === null).map((p) => p.get('depth'))
    await waitFor(() => expect(depths()).toContain('6'), WAIT)
    expect(depths()).not.toContain('7')
    for (let i = 0; i < 7; i++) fireEvent.click(less)
    await waitFor(() => expect(visible(inspector().querySelector('.rg-depth'))).toBe('1'), WAIT)
    expect(less).toHaveProperty('disabled', true)
    await waitFor(() => expect(depths()).toContain('1'), WAIT)
    expect(depths()).not.toContain('0')
  })

  it('draws the Call graph view with callers left and callees right, each edge in its evidence, and the legend', async () => {
    const calls = await pickTotal()
    fireEvent.click(within(document.querySelector('.rg-tools')!).getByRole('radio', { name: 'Call graph' }))
    const big = () => document.querySelector('.rg-callview svg')!
    await waitFor(() => expect(big()).not.toBeNull(), WAIT)
    expect(big().getAttribute('aria-label')).toBe('Call graph centred on OrderService.total, callers left, callees right, depth 2')
    const x = (id: string) => Number(big().querySelector(`.rg-cnode[data-id="${id}"]`)!.getAttribute('data-x'))
    expect(x('src/api/orders.py#checkout_total')).toBeLessThan(x(TOTAL))
    expect(x('src/api/routes.py#admin_recalc')).toBeLessThan(x('src/api/orders.py#checkout_total'))
    expect(x('src/core/money.py#Money.round')).toBeGreaterThan(x(TOTAL))
    const looks = Array.from(big().querySelectorAll('.rg-cedge')).map((e) => e.getAttribute('data-look')).sort()
    expect(looks).toEqual(['ast', 'import', 'low', 'lsp', 'lsp'])
    const ast = big().querySelector('.rg-cedge[data-look="ast"] title')!
    expect(ast.textContent).toBe('InvoiceJob.run → OrderService.total · ast · confidence 0.6 · also import')
    const legend = Array.from(document.querySelectorAll('.rg-evlegend .rg-lg')).map((l) => [l.getAttribute('data-look'), visible(l)])
    expect(legend).toEqual([
      ['lsp', 'lsp: resolved by the language server'],
      ['ast', 'ast: syntactic match'],
      ['import', 'import: the file imports the module, no call resolved'],
      ['low', 'below 0.4 confidence'],
    ])
    // Direction asks the route again.
    fireEvent.click(within(document.querySelector('.rg-callview')!).getByRole('radio', { name: 'callers' }))
    await waitFor(() => expect(symbolCalls(calls).some((p) => p.get('direction') === 'callers')).toBe(true), WAIT)
  })

  it('draws the Test map view for the picked symbol', async () => {
    await pickTotal()
    fireEvent.click(within(document.querySelector('.rg-tools')!).getByRole('radio', { name: 'Test map' }))
    await waitFor(() => expect(document.querySelectorAll('.rg-testview .rg-test')).toHaveLength(2), WAIT)
    expect(visible(document.querySelector('.rg-testview'))).toContain('tests/core/test_invoices.py#test_invoice_totals')
  })
})

describe('honest states', () => {
  it('says the graph route is not served yet, naming it, while the page still draws', async () => {
    routes({ graph: { status: 404, body: { detail: 'Not Found' } } })
    await mount('graph')
    await waitFor(() => expect(document.querySelector('[data-notserved="GET /v1/repositories/{repo_id}/graph"]')).not.toBeNull(), WAIT)
    expect(document.querySelector('.ur-tabs')).not.toBeNull()
    expect(nodes()).toHaveLength(0)
  })

  it('says an index without a graph has none, and does not call that "not served"', async () => {
    routes({ graph: { status: 404, body: { code: 'no_graph', message: 'the index of commit a1b2c3d has no graph' } } })
    await mount('graph')
    await waitFor(() => expect(visible(document.querySelector('.rg-graph'))).toContain('No graph for this index'), WAIT)
    expect(visible(document.querySelector('.rg-graph'))).toContain('the index of commit a1b2c3d has no graph')
    expect(document.querySelector('[data-notserved]')).toBeNull()
  })
})

describe('the phone frame (390px)', () => {
  it('lists the modules as rows for the inspector, and hides the canvas there', async () => {
    routes()
    await mount('graph')
    await waitFor(() => expect(document.querySelectorAll('.rg-phone .rg-prow')).toHaveLength(4), WAIT)
    expect(CSS).toMatch(/@media \(max-width: 640px\)[^}]*\.rg-canvas[^}]*display: none/)
    expect(CSS).toMatch(/\.rg-phone \{[^}]*display: none/)
  })
})
