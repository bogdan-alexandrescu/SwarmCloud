// THE GRAPH EXPLORER'S PURE RULES (repositories.html screen 8, pick A; screen
// 9, pick A): the force layout, the package clustering and its collapse past
// COLLAPSE_AT modules, the hot-spot / test-reach colour bands, the call
// graph's sides, the evidence look of an edge, the depth bound, and the
// served answers' normalisers -- an unknown figure stays null, never 0.
//
// MUTATIONS: seed the layout with Math.random (the same graph lands in two
// places); drop the cluster pull (members scatter away from their package);
// draw every module of a 2,000-module repository (no collapse); treat a null
// hot-spot count as 0 (cool instead of hatched); put a below-0.4 lsp edge in
// the lsp style; let depth reach 7 -- each turns a case red.

import { describe, expect, it } from 'vitest'

import {
  COLLAPSE_AT, DEPTH_MAX, callColumns, clampDepth, degree, edgeLook, forceLayout, fullSuiteWords, graphView, heatOf,
  leafOf, normCallGraph, normImpact, normModuleGraph, packageOf, stalenessPill, type ModuleGraph,
} from '../RepoGraphData'

function graph(modules: string[], edges: [string, string, number][] = []): ModuleGraph {
  return normModuleGraph({
    index_sha: 'a1b2c3d'.padEnd(40, '0'),
    freshness: { state: 'current' },
    modules: modules.map((id, i) => ({ id, modules: 1, symbols: 10 + i, tests: 0, hot_spot_changes: i, test_reach: 0.5, languages: ['python'] })),
    edges: edges.map(([from, to, weight]) => ({ from, to, weight, kinds: { call: weight }, max_confidence: 0.95 })),
    counts: { files: 12, symbols: 300 },
  })!
}

const SMALL = graph(
  ['src/api/orders.py', 'src/api/users.py', 'src/core/orders.py', 'src/core/pricing.py', 'src/core/money.py', 'workers/invoices.py'],
  [['src/api/orders.py', 'src/core/orders.py', 12], ['src/core/orders.py', 'src/core/pricing.py', 4], ['workers/invoices.py', 'src/core/orders.py', 3]],
)

describe('packages', () => {
  it('clusters a module by its first segment, or two under a monorepo root, as the route does', () => {
    expect(packageOf('src/core/orders.py')).toBe('src')
    expect(packageOf('apps/swarm-api/swarm_api/impact.py')).toBe('apps/swarm-api')
    expect(packageOf('.')).toBe('.')
    expect(leafOf('src/core/orders.py')).toBe('orders')
  })
})

describe('the force layout', () => {
  it('is deterministic: the same graph lands in the same place on every read', () => {
    const v = graphView(SMALL, new Set())
    const a = forceLayout(v)
    const b = forceLayout(v)
    expect([...a.pos.entries()]).toEqual([...b.pos.entries()])
  })

  it('keeps every node inside the canvas', () => {
    const l = forceLayout(graphView(SMALL, new Set()), 640, 440)
    for (const p of l.pos.values()) {
      expect(p.x - p.r).toBeGreaterThanOrEqual(0)
      expect(p.x + p.r).toBeLessThanOrEqual(640)
      expect(p.y - p.r).toBeGreaterThanOrEqual(0)
      expect(p.y + p.r).toBeLessThanOrEqual(440)
    }
  })

  it('draws a soft rectangle per package around its own members', () => {
    const l = forceLayout(graphView(SMALL, new Set()))
    const src = l.boxes.find((b) => b.cluster === 'src')!
    expect(src).toBeDefined()
    for (const id of ['src/api/orders.py', 'src/core/money.py']) {
      const p = l.pos.get(id)!
      expect(p.x).toBeGreaterThan(src.x)
      expect(p.x).toBeLessThan(src.x + src.w)
      expect(p.y).toBeGreaterThan(src.y)
      expect(p.y).toBeLessThan(src.y + src.h)
    }
    // One-member clusters get no rectangle.
    expect(l.boxes.find((b) => b.cluster === 'workers')).toBeUndefined()
  })

  it('places no two nodes on top of each other', () => {
    const l = forceLayout(graphView(SMALL, new Set()))
    const ps = [...l.pos.values()]
    for (let i = 0; i < ps.length; i++) {
      for (let j = i + 1; j < ps.length; j++) {
        const d = Math.hypot(ps[i]!.x - ps[j]!.x, ps[i]!.y - ps[j]!.y)
        expect(d).toBeGreaterThan(8)
      }
    }
  })
})

describe('a large repository starts clustered and expands on click', () => {
  const many = Array.from({ length: COLLAPSE_AT + 20 }, (_, i) => (i % 2 === 0 ? `src/m${i}.py` : `lib/m${i}.py`))
  const g = graph(many, [['src/m0.py', 'lib/m1.py', 5], ['src/m2.py', 'lib/m3.py', 2], ['src/m0.py', 'src/m2.py', 9]])

  it(`draws one node per package past ${COLLAPSE_AT} modules, with the edges folded between them`, () => {
    const v = graphView(g, new Set())
    expect(v.collapsed).toBe(true)
    expect(v.nodes.map((n) => n.id)).toEqual(['pkg:lib', 'pkg:src'])
    expect(v.nodes[0]!.members).toBe(40)
    // src -> lib folds 5 + 2; src -> src is inside one node and is dropped.
    expect(v.edges).toEqual([{ from: 'pkg:src', to: 'pkg:lib', weight: 7, max_confidence: 0.95 }])
    // A folded package's reach is a dash: it is served per module, unweighted.
    expect(v.nodes[0]!.test_reach).toBeNull()
  })

  it('opens one package into its modules and keeps the others folded', () => {
    const v = graphView(g, new Set(['src']))
    expect(v.nodes.filter((n) => n.isPackage).map((n) => n.id)).toEqual(['pkg:lib'])
    expect(v.nodes.filter((n) => !n.isPackage)).toHaveLength(40)
    expect(degree(v, 'pkg:lib').callers).toBe(2)
  })

  it('draws every module below the bound', () => {
    expect(graphView(SMALL, new Set()).collapsed).toBe(false)
  })
})

describe('colour', () => {
  it('bands hot-spots at 20+ and 5-19 changes, and hatches an unserved count rather than calling it cool', () => {
    expect(heatOf({ hot_spot_changes: 23, test_reach: null }, 'hot-spots')).toBe('hot')
    expect(heatOf({ hot_spot_changes: 5, test_reach: null }, 'hot-spots')).toBe('warm')
    expect(heatOf({ hot_spot_changes: 0, test_reach: null }, 'hot-spots')).toBe('cool')
    expect(heatOf({ hot_spot_changes: null, test_reach: null }, 'hot-spots')).toBe('unmeasured')
  })

  it('bands test reach low as the hot end, and hatches a module with nothing to reach', () => {
    expect(heatOf({ hot_spot_changes: 0, test_reach: 0.2 }, 'test-reach')).toBe('hot')
    expect(heatOf({ hot_spot_changes: 0, test_reach: 0.6 }, 'test-reach')).toBe('warm')
    expect(heatOf({ hot_spot_changes: 0, test_reach: 0.78 }, 'test-reach')).toBe('cool')
    expect(heatOf({ hot_spot_changes: 0, test_reach: null }, 'test-reach')).toBe('unmeasured')
  })
})

describe('the call graph', () => {
  const g = normCallGraph({
    symbol: { id: 'src/core/orders.py#OrderService.total', kind: 'method', path: 'src/core/orders.py', start_line: 88, end_line: 131 },
    depth: 2,
    direction: 'both',
    nodes: [
      { id: 'src/core/orders.py#OrderService.total', distance: 0 },
      { id: 'src/api/orders.py#checkout_total', distance: 1 },
      { id: 'src/api/routes.py#post_checkout', distance: 2 },
      { id: 'src/core/money.py#Money.round', distance: 1 },
    ],
    edges: [
      { from: 'src/api/orders.py#checkout_total', to: 'src/core/orders.py#OrderService.total', kind: 'call', evidence: 'lsp', confidence: 0.95 },
      { from: 'src/api/routes.py#post_checkout', to: 'src/api/orders.py#checkout_total', kind: 'call', evidence: 'ast', confidence: 0.3 },
      { from: 'src/core/orders.py#OrderService.total', to: 'src/core/money.py#Money.round', kind: 'call', evidence: 'lsp', confidence: 0.95 },
    ],
    truncated: false,
  })!

  it('puts callers left and callees right, each at its own depth', () => {
    const cols = callColumns(g)
    expect(cols.get('src/core/orders.py#OrderService.total')).toEqual({ side: 'centre', depth: 0 })
    expect(cols.get('src/api/orders.py#checkout_total')).toEqual({ side: 'caller', depth: 1 })
    expect(cols.get('src/api/routes.py#post_checkout')).toEqual({ side: 'caller', depth: 2 })
    expect(cols.get('src/core/money.py#Money.round')).toEqual({ side: 'callee', depth: 1 })
  })

  it('draws an edge below 0.4 in the low style whatever its evidence, and keeps lsp and ast apart above it', () => {
    expect(g.edges.map(edgeLook)).toEqual(['lsp', 'low', 'lsp'])
    expect(edgeLook({ evidence: 'import', confidence: 0.4 })).toBe('import')
    expect(edgeLook({ evidence: null, confidence: null })).toBe('unknown')
  })

  it('bounds depth to 1-6, the route’s own bound', () => {
    expect(DEPTH_MAX).toBe(6)
    expect(clampDepth(0)).toBe(1)
    expect(clampDepth(7)).toBe(6)
    expect(clampDepth(3)).toBe(3)
  })
})

describe('served answers', () => {
  it('keeps an unserved figure null, never 0', () => {
    const g = normModuleGraph({ modules: [{ id: 'src/a.py' }], edges: [{ from: 'src/a.py', to: 'src/missing.py', weight: 2 }] })!
    expect(g.modules[0]).toMatchObject({ symbols: null, hot_spot_changes: null, test_reach: null })
    // An edge to a module the answer does not list is dropped, not drawn to nowhere.
    expect(g.edges).toEqual([])
    const p = normImpact({ selection: 'targeted' })!
    expect([p.selected, p.total_tests, p.changed_symbols]).toEqual([null, null, null])
  })

  it('says the full suite is not needed for a targeted plan, and names the first trigger of a fallback', () => {
    expect(fullSuiteWords(normImpact({ selection: 'targeted' })!)).toMatchObject({ needed: false, words: 'not required for this change' })
    const fb = fullSuiteWords(normImpact({
      selection: 'full_suite',
      full_suite: { because: ['shared_fixture_changed'] },
      fallback_triggers: [
        { kind: 'shared_fixture_changed', path: 'tests/conftest.py', reason: 'tests/conftest.py is a shared fixture' },
        { kind: 'stale_index', reason: 'the index is stale' },
      ],
    })!)!
    expect(fb.needed).toBe(true)
    expect(fb.reason).toBe('tests/conftest.py is a shared fixture (and 1 more)')
    expect(fullSuiteWords(normImpact({})!)).toBeNull()
  })

  it('pills a served staleness: current, behind by n, stale, none', () => {
    const sha = 'a1b2c3d'.padEnd(40, '0')
    expect(stalenessPill({ index_sha: sha, head_sha: sha, behind_by: 0, stale: false, state: 'current', reason: null }).word).toBe('index current · a1b2c3d')
    expect(stalenessPill({ index_sha: sha, head_sha: null, behind_by: 3, stale: false, state: 'behind', reason: null }).word).toBe('index 3 behind head')
    expect(stalenessPill({ index_sha: sha, head_sha: null, behind_by: 300, stale: true, state: 'stale', reason: 'old' }).hue).toBe('bad')
    expect(stalenessPill({ index_sha: null, head_sha: null, behind_by: null, stale: null, state: 'none', reason: null }).word).toBe('no index')
  })
})
