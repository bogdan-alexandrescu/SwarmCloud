// LANE GR3'S PURE RULES (docs/design/graph-rendering.md §3.2 defects 7-11,
// §7 the GR3 row): structural clustering, the aggregated first view and its
// drill-in, labels unique within a view, soft bounds, the neighbour list, and
// the layout plan the Web Worker runs.
//
// THE ACCEPTANCE FIGURES ARE ASSERTED HERE, each against its control:
//   * the 200-module src/ fixture folds to at least 6 clusters, where the
//     route's own package rule (`packageOf`) folds it to 2;
//   * no node rests on the canvas boundary: every node is EDGE_CLEAR clear of
//     every edge (the old clamp left r + 18, and put a row of nodes on one
//     line along the floor; no three nodes share an extreme line now);
//   * labels are unique within every view, at every drill level, where the
//     leaves alone (`leafOf`) repeat.
//
// MUTATIONS: cluster by `packageOf` again (2 clusters, red); drop the
// pass-through descent (`apps` becomes a cluster of one child, red); put the
// hard clamp back (nodes on the wall, red); label by `leafOf` (duplicates,
// red); fold a cluster without counting its members (sum != modules, red);
// put the cluster pull back to 0.9, or drop the evict / separate passes (a
// stray node or a touching pair, red);
// read neighbours off the raw graph instead of the view (a folded node's
// neighbour is a module the canvas does not draw, red).

import { describe, expect, it, vi } from 'vitest'

import {
  CLUSTER_MAX, COLLAPSE_AT, displayLabel, graphView, leafOf, neighbours, normModuleGraph,
  packageOf, structuralClusters, uniqueLabels, type GraphView, type ModuleGraph, type ViewNode,
} from '../RepoGraphData'
import { EDGE_CLEAR, forceLayout, layoutPlan } from '../RepoGraphLayout'
import { srcGraphBody, srcModuleIds } from './repographfixture'

const G200 = normModuleGraph(srcGraphBody(200))!
const G2000 = normModuleGraph(srcGraphBody(2000))!

function graph(modules: string[], edges: [string, string, number][] = []): ModuleGraph {
  return normModuleGraph({
    index_sha: 'a1b2c3d'.padEnd(40, '0'),
    modules: modules.map((id) => ({ id, modules: 1, symbols: 10, hot_spot_changes: 3, test_reach: 0.5, languages: ['python'] })),
    edges: edges.map(([from, to, weight]) => ({ from, to, weight, kinds: { call: weight }, max_confidence: 0.95 })),
  })!
}

/** Every module of `g` is in the view exactly once: as a node, or counted inside one folded cluster. */
function accounted(v: GraphView, g: ModuleGraph): number {
  return v.nodes.reduce((n, x) => n + x.members, 0) - g.modules.length
}

function labelsOf(v: GraphView): string[] {
  return v.nodes.map(displayLabel)
}

describe('structural clustering (defect 7)', () => {
  it('folds the 200-module src/ fixture into at least 6 clusters, where the route package rule gives 2', () => {
    expect(G200.modules).toHaveLength(200)
    // The control: today's rule.
    expect(new Set(G200.modules.map((m) => packageOf(m.id))).size).toBe(2)
    const v = graphView(G200, new Set())
    expect(v.collapsed).toBe(true)
    expect(v.nodes.every((n) => n.isPackage)).toBe(true)
    expect(v.nodes.length).toBeGreaterThanOrEqual(6)
    expect(v.nodes.map((n) => n.cluster)).toEqual(['lib/shared', 'src/api', 'src/auth', 'src/billing', 'src/core', 'src/db', 'src/ui', 'src/workers'])
    expect(accounted(v, G200)).toBe(0)
  })

  it('names every cluster by a directory all its members sit in, and puts each module in exactly one', () => {
    const parts = structuralClusters(G2000.modules)
    expect(parts.length).toBeGreaterThanOrEqual(6)
    expect(parts.length).toBeLessThanOrEqual(CLUSTER_MAX)
    const seen = new Set<string>()
    for (const p of parts) {
      for (const m of p.members) {
        expect(m.id.startsWith(`${p.key}/`)).toBe(true)
        expect(seen.has(m.id)).toBe(false)
        seen.add(m.id)
      }
    }
    expect(seen.size).toBe(2000)
  })

  it('descends through a directory that has one child instead of making it a cluster', () => {
    const ids = [
      ...Array.from({ length: 10 }, (_, i) => `apps/swarm-api/swarm_api/m${i}.py`),
      ...Array.from({ length: 10 }, (_, i) => `apps/swarm-api/tests/t${i}.py`),
      ...Array.from({ length: 10 }, (_, i) => `apps/worker/worker/w${i}.py`),
    ]
    const keys = structuralClusters(ids.map((id) => ({ id }))).map((p) => p.key)
    expect(keys).toEqual(['apps/swarm-api/swarm_api', 'apps/swarm-api/tests', 'apps/worker/worker'])
  })

  it('keeps modules that sit directly in a split directory together, under its name', () => {
    const ids = [...Array.from({ length: 6 }, (_, i) => `src/a/m${i}.py`), ...Array.from({ length: 6 }, (_, i) => `src/b/m${i}.py`), 'src/main.py', 'src/cli.py']
    const parts = structuralClusters(ids.map((id) => ({ id })))
    expect(parts.map((p) => [p.key, p.members.length])).toEqual([['src', 2], ['src/a', 6], ['src/b', 6]])
  })

  it('draws a flat root repository as one cluster rather than one per file', () => {
    const v = graphView(graph(Array.from({ length: 70 }, (_, i) => `m${i}.py`)), new Set())
    expect(v.nodes.map((n) => n.id)).toEqual(['pkg:.'])
    expect(v.nodes[0]!.members).toBe(70)
  })
})

describe('the aggregated first view, and drilling in (Graphify meta-graph)', () => {
  it(`starts the 2,000-module fixture as at most ${CLUSTER_MAX} clusters, never the hairball`, () => {
    const v = graphView(G2000, new Set())
    expect(v.collapsed).toBe(true)
    expect(v.nodes.length).toBeLessThanOrEqual(CLUSTER_MAX)
    expect(v.nodes.length).toBeGreaterThanOrEqual(6)
    expect(accounted(v, G2000)).toBe(0)
    // Edges between clusters carry the summed calls crossing them, and none loops on itself.
    expect(v.edges.length).toBeGreaterThan(0)
    expect(v.edges.every((e) => e.from !== e.to && e.from.startsWith('pkg:') && e.to.startsWith('pkg:'))).toBe(true)
  })

  it(`opens a cluster past ${COLLAPSE_AT} modules into its own sub-clusters, and one under it into modules`, () => {
    const first = graphView(G2000, new Set())
    const big = first.nodes.filter((n) => n.members > COLLAPSE_AT).sort((a, b) => b.members - a.members)[0]!
    const opened = graphView(G2000, new Set([big.cluster]))
    const inside = opened.nodes.filter((n) => n.cluster.startsWith(`${big.cluster}/`))
    expect(inside.length).toBeGreaterThan(1)
    expect(inside.every((n) => n.isPackage)).toBe(true)
    expect(inside.reduce((s, n) => s + n.members, 0)).toBe(big.members)
    expect(accounted(opened, G2000)).toBe(0)
    const small = inside.sort((a, b) => a.members - b.members)[0]!
    expect(small.members).toBeLessThanOrEqual(COLLAPSE_AT)
    const deeper = graphView(G2000, new Set([big.cluster, small.cluster]))
    const modules = deeper.nodes.filter((n) => !n.isPackage)
    expect(modules).toHaveLength(small.members)
    expect(modules.every((n) => n.id.startsWith(`${small.cluster}/`))).toBe(true)
    expect(accounted(deeper, G2000)).toBe(0)
  })

  it('draws every module of a small repository inside its cluster, folded nowhere', () => {
    const v = graphView(graph(srcModuleIds(48)), new Set())
    expect(v.collapsed).toBe(false)
    expect(v.nodes).toHaveLength(48)
    // Eight directories, eight rectangles: the route's package rule drew two overlapping ones (screens 9-10).
    expect(new Set(v.nodes.map((n) => n.cluster)).size).toBe(8)
  })
})

describe('labels unique within a view (defect 10)', () => {
  it('climbs one directory for each label that collides, and no further', () => {
    const v = graphView(graph(['src/api/orders.py', 'src/core/orders.py', 'src/db/orders.py', 'src/db/money.py']), new Set())
    const byId = new Map(v.nodes.map((n) => [n.id, n.label]))
    expect(byId.get('src/api/orders.py')).toBe('api/orders')
    expect(byId.get('src/core/orders.py')).toBe('core/orders')
    expect(byId.get('src/db/money.py')).toBe('money')
  })

  it('falls back to the full id when only the extension differs', () => {
    const v = graphView(graph(['web/orders.ts', 'web/orders.py']), new Set())
    expect(v.nodes.map((n) => n.label).sort()).toEqual(['web/orders.py', 'web/orders.ts'])
  })

  it('never prints a module like a folded cluster', () => {
    const nodes: ViewNode[] = [
      { id: 'pkg:lib', label: 'lib', cluster: 'lib', isPackage: true, members: 3, symbols: null, hot_spot_changes: null, test_reach: null, languages: [] },
      { id: 'lib (3)', label: 'lib (3)', cluster: '', isPackage: false, members: 1, symbols: null, hot_spot_changes: null, test_reach: null, languages: [] },
    ]
    const out = uniqueLabels(nodes).map(displayLabel)
    expect(new Set(out).size).toBe(2)
  })

  it('is unique in every view of the fixtures: flat, folded, and at every drill level', () => {
    const views = [
      graphView(G200, new Set(), false),
      graphView(G200, new Set(['src/api', 'src/core'])),
      graphView(G2000, new Set()),
      graphView(G2000, new Set(['src/core', 'src/core/handlers'])),
      graphView(G2000, new Set(), false),
    ]
    for (const v of views) {
      const ls = labelsOf(v)
      expect(new Set(ls).size).toBe(ls.length)
    }
    // The control: by leaf alone, the 200 fixture repeats.
    const leaves = G200.modules.map((m) => leafOf(m.id))
    expect(new Set(leaves).size).toBeLessThan(leaves.length)
  })
})

describe('soft bounds (defect 8)', () => {
  const cases: [string, GraphView][] = [
    ['48 modules in two clusters', graphView(graph(srcModuleIds(48)), new Set())],
    ['200 modules unclustered', graphView(G200, new Set(), false)],
    ['the 2,000-module first view', graphView(G2000, new Set())],
  ]
  for (const [name, v] of cases) {
    it(`keeps every node clear of the canvas edge: ${name}`, () => {
      const W = 640
      const H = 440
      const l = forceLayout(v, W, H)
      const ps = [...l.pos.values()]
      expect(ps).toHaveLength(v.nodes.length)
      for (const p of ps) {
        expect(p.x - p.r).toBeGreaterThanOrEqual(EDGE_CLEAR - 0.1)
        expect(p.y - p.r).toBeGreaterThanOrEqual(EDGE_CLEAR - 0.1)
        expect(p.x + p.r).toBeLessThanOrEqual(W - EDGE_CLEAR + 0.1)
        expect(p.y + p.r).toBeLessThanOrEqual(H - EDGE_CLEAR + 0.1)
      }
      // No row along the floor or any wall. A clamp leaves its nodes on the
      // SAME coordinate (screen 9's floor); the rim of a round cluster has a
      // few nodes near its extreme, never three on one line.
      const lines = [ps.map((p) => p.y + p.r), ps.map((p) => -(p.y - p.r)), ps.map((p) => p.x + p.r), ps.map((p) => -(p.x - p.r))]
      for (const line of lines) {
        const far = Math.max(...line)
        expect(line.filter((y) => far - y < 0.15).length).toBeLessThanOrEqual(2)
      }
    })
  }
})

describe('clusters read as clusters', () => {
  // The old pull let an opened cluster of 25 spread over the whole canvas, so
  // its rectangle swallowed folded clusters, and the shrink-to-fit pressed
  // nodes onto each other: 7-50 strays and up to 126 touching pairs on these
  // same views before the change.
  const cases: [string, GraphView][] = [
    ['48 modules, eight clusters', graphView(graph(srcModuleIds(48)), new Set())],
    ['200 modules, lib/shared opened', graphView(G200, new Set(['lib/shared']))],
    ['200 modules, two clusters opened', graphView(G200, new Set(['src/api', 'src/db']))],
    ['2,000 modules, opened two levels', graphView(G2000, new Set(['src/core', 'src/core/handlers']))],
  ]
  for (const [name, v] of cases) {
    it(`puts no node inside another cluster's rectangle, and no two nodes touching: ${name}`, () => {
      const l = forceLayout(v)
      for (const b of l.boxes) {
        for (const n of v.nodes) {
          if (n.cluster === b.cluster) continue
          const p = l.pos.get(n.id)!
          const inside = p.x > b.x && p.x < b.x + b.w && p.y > b.y && p.y < b.y + b.h
          expect(inside, `${n.id} inside ${b.cluster}`).toBe(false)
        }
      }
      const ps = [...l.pos.entries()]
      for (let i = 0; i < ps.length; i++) {
        for (let j = i + 1; j < ps.length; j++) {
          const [a, p] = ps[i]!
          const [b, q] = ps[j]!
          expect(Math.hypot(p.x - q.x, p.y - q.y) - p.r - q.r, `${a} and ${b}`).toBeGreaterThan(1)
        }
      }
    })
  }
})

describe('the neighbour list (defect 11)', () => {
  it('lists callers and callees as drawn nodes, heaviest first', () => {
    const g = graph(['a/x.py', 'a/y.py', 'b/z.py', 'c/w.py'], [['a/x.py', 'b/z.py', 2], ['a/y.py', 'b/z.py', 9], ['b/z.py', 'c/w.py', 4]])
    const v = graphView(g, new Set())
    const nb = neighbours(v, 'b/z.py')
    expect(nb.callers.map((n) => [n.node.id, n.weight])).toEqual([['a/y.py', 9], ['a/x.py', 2]])
    expect(nb.callees.map((n) => [n.node.id, n.weight])).toEqual([['c/w.py', 4]])
    expect(neighbours(v, 'c/w.py').callees).toEqual([])
  })

  it('names a folded cluster, not a module the canvas does not draw', () => {
    const v = graphView(G200, new Set(['src/api']))
    const api = v.nodes.filter((n) => n.id.startsWith('src/api/'))
    expect(api).toHaveLength(25)
    const ids = new Set(v.nodes.map((n) => n.id))
    let folded = 0
    for (const m of api) {
      const nb = neighbours(v, m.id)
      for (const x of [...nb.callers, ...nb.callees]) expect(ids.has(x.node.id)).toBe(true)
      folded += nb.callees.filter((x) => x.node.isPackage).length
    }
    // api calls core, auth and shared: those arrive as their folded clusters.
    expect(folded).toBeGreaterThan(0)
  })
})

describe('the layout plan the worker runs (defect 9)', () => {
  it('is the force layout plus a label place for every node, and survives a structured clone', () => {
    const v = graphView(G200, new Set(), false)
    const plan = layoutPlan(v)
    expect([...plan.pos.entries()]).toEqual([...forceLayout(v).pos.entries()])
    expect(plan.labels.size).toBe(v.nodes.length)
    const copy = structuredClone(plan)
    expect(copy.pos.get(v.nodes[0]!.id)).toEqual(plan.pos.get(v.nodes[0]!.id))
  })

  it('the worker module answers a request with that plan', async () => {
    const posted: unknown[] = []
    const spy = vi.spyOn(self, 'postMessage').mockImplementation((m: unknown) => {
      posted.push(m)
    })
    vi.resetModules()
    await import('../repoGraphLayout.worker')
    const v = graphView(graph(srcModuleIds(30)), new Set())
    ;(self.onmessage as (e: MessageEvent) => void)(new MessageEvent('message', { data: { view: v, width: 640, height: 440 } }))
    spy.mockRestore()
    self.onmessage = null
    expect(posted).toHaveLength(1)
    const plan = (posted[0] as { plan: ReturnType<typeof layoutPlan> }).plan
    expect([...plan.pos.keys()].sort()).toEqual(v.nodes.map((n) => n.id).sort())
  })
})
