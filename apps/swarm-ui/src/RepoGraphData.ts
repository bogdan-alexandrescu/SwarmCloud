/**
 * WORK › REPOSITORIES › GRAPH AND IMPACT: the shapes the Graph explorer (pick
 * A, repositories.html screen 8), the Impact view (pick A, screen 9), the run
 * page's Context card (pick B, screen 5) and the PR card's selected-tests gate
 * row (P3, screen 10) read, and the pure rules that turn them into drawings.
 *
 * THE ROUTES ARE lane RI11's (docs/repo-index.md §4.3a, §6.1):
 *
 *   GET  /v1/repositories/{repo_id}/graph     modules, weighted edges, hot-spot and test-reach figures
 *   GET  /v1/repositories/{repo_id}/symbols   ?q= search · ?id=&depth=&direction= call graph · ?id=&tests=1 test map
 *   POST /v1/repositories/{repo_id}/impact    a pull request or a commit -> the test plan
 *
 * Nothing here trusts a field to be present: every served value is kept only
 * when it has the type the route serves, and otherwise becomes null, which a
 * screen draws as a dash with its reason -- never 0 (redesign-v2.md,
 * "Honesty rules").
 *
 * THE LAYOUT IS DEPENDENCY-FREE. A force layout of a few hundred nodes is a
 * hundred lines; a graph library (d3-force, cytoscape) would be the biggest
 * dependency in the bundle for a picture the force layout below already draws.
 * It is SEEDED BY THE MODULE PATH, not by a random number, so the same graph
 * lands in the same place on every read and between index versions (the
 * frame's own risk: "positions move between index versions unless the layout
 * is seeded by module path").
 */

type Rec = Record<string, unknown>

function isRec(v: unknown): v is Rec {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}
function str(v: unknown): string | null {
  return typeof v === 'string' && v !== '' ? v : null
}
function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}
function strs(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string' && x !== '') : []
}
function recs(v: unknown): Rec[] {
  return Array.isArray(v) ? v.filter(isRec) : []
}

// ---------------------------------------------------------------------------
// Staleness, as every graph answer carries it (repo-index.md §5)
// ---------------------------------------------------------------------------

export type ServedFreshState = 'current' | 'behind' | 'stale' | 'unknown' | 'none'

export interface Staleness {
  index_sha: string | null
  head_sha: string | null
  behind_by: number | null
  stale: boolean | null
  state: ServedFreshState | null
  reason: string | null
}

const FRESH_STATES: readonly ServedFreshState[] = ['current', 'behind', 'stale', 'unknown', 'none']

export function normStaleness(v: Rec): Staleness {
  const f = isRec(v.freshness) ? v.freshness : {}
  const state = typeof f.state === 'string' && (FRESH_STATES as readonly string[]).includes(f.state) ? (f.state as ServedFreshState) : null
  return {
    index_sha: str(v.index_sha),
    head_sha: str(v.head_sha),
    behind_by: num(v.behind_by),
    stale: typeof v.stale === 'boolean' ? v.stale : null,
    state,
    reason: str(f.reason),
  }
}

/** The pill for a served staleness: its words, hue and the reason as its title. */
export function stalenessPill(s: Staleness): { word: string; hue: 'live' | 'warn' | 'bad' | 'neu'; mark: 'succeeded' | 'warn' | 'failed' | 'queued'; why: string | null } {
  const sha = s.index_sha === null ? null : s.index_sha.slice(0, 7)
  if (s.index_sha === null || s.state === 'none') return { word: 'no index', hue: 'neu', mark: 'queued', why: s.reason ?? 'No index has been promoted' }
  if (s.stale === true || s.state === 'stale') {
    return { word: s.behind_by === null ? `index stale · ${sha}` : `index stale · ${s.behind_by} behind head`, hue: 'bad', mark: 'failed', why: s.reason }
  }
  if (s.state === 'current') return { word: `index current · ${sha}`, hue: 'live', mark: 'succeeded', why: null }
  if (s.state === 'behind') {
    return { word: s.behind_by === null ? `index behind head · ${sha}` : `index ${s.behind_by} behind head`, hue: 'warn', mark: 'warn', why: s.reason }
  }
  return { word: `index ${sha} · head not compared`, hue: 'neu', mark: 'queued', why: s.reason ?? 'Whether this index is current was not served' }
}

// ---------------------------------------------------------------------------
// GET /graph: the module dependency graph
// ---------------------------------------------------------------------------

export interface GraphModule {
  id: string
  modules: number | null
  symbols: number | null
  tests: number | null
  hot_spot_changes: number | null
  /** The share of the module's non-test symbols a test reaches; null when it has none, or not served. */
  test_reach: number | null
  languages: string[]
}

export interface GraphEdge {
  from: string
  to: string
  weight: number
  kinds: Record<string, number>
  max_confidence: number | null
}

export interface ModuleGraph extends Staleness {
  modules: GraphModule[]
  edges: GraphEdge[]
  counts: Record<string, number>
  truncated: string[]
}

export function normModuleGraph(v: unknown): ModuleGraph | null {
  if (!isRec(v)) return null
  const modules = recs(v.modules)
    .map((m): GraphModule | null => {
      const id = str(m.id)
      return id === null
        ? null
        : {
            id,
            modules: num(m.modules),
            symbols: num(m.symbols),
            tests: num(m.tests),
            hot_spot_changes: num(m.hot_spot_changes),
            test_reach: num(m.test_reach),
            languages: strs(m.languages),
          }
    })
    .filter((m): m is GraphModule => m !== null)
  const known = new Set(modules.map((m) => m.id))
  const edges = recs(v.edges)
    .map((e): GraphEdge | null => {
      const from = str(e.from)
      const to = str(e.to)
      const weight = num(e.weight)
      if (from === null || to === null || weight === null || !known.has(from) || !known.has(to) || from === to) return null
      const kinds: Record<string, number> = {}
      if (isRec(e.kinds)) for (const [k, n] of Object.entries(e.kinds)) if (typeof n === 'number') kinds[k] = n
      return { from, to, weight, kinds, max_confidence: num(e.max_confidence) }
    })
    .filter((e): e is GraphEdge => e !== null)
  const counts: Record<string, number> = {}
  if (isRec(v.counts)) for (const [k, n] of Object.entries(v.counts)) if (typeof n === 'number' && Number.isFinite(n)) counts[k] = n
  return { ...normStaleness(v), modules, edges, counts, truncated: strs(v.truncated) }
}

/**
 * The package a module is drawn inside: its first two path segments under a
 * monorepo root (`apps/swarm-api`, `packages/web`), else its first segment
 * (`src`). The same rule as the route's `?cluster=package` (`package_of` in
 * apps/swarm-api/swarm_api/impact.py), so the console's clusters and the API's
 * aggregation name the same groups.
 */
const MONO_ROOTS = new Set(['apps', 'packages', 'services', 'libs', 'cmd', 'internal', 'modules'])
export function packageOf(module: string): string {
  if (module === '.') return '.'
  const parts = module.split('/')
  return MONO_ROOTS.has(parts[0] ?? '') ? parts.slice(0, 2).join('/') : (parts[0] ?? module)
}

/** The last path segment: what a node's label says on the canvas. */
export function leafOf(path: string): string {
  const parts = path.split('/').filter((p) => p !== '')
  return (parts[parts.length - 1] ?? path).replace(/\.(py|ts|tsx|js|jsx|go|tf|rs|java)$/, '')
}

// ---- colour ---------------------------------------------------------------

export type ColourBy = 'hot-spots' | 'test-reach'
/** `hot` / `warm` / `cool` are the frame's three node fills; `unmeasured` is hatched (components.html A). */
export type Heat = 'hot' | 'warm' | 'cool' | 'unmeasured'

/** Hot-spot bands from the frame's legend: 20+ changes in 90 days, 5-19, under 5. */
export const HOT_AT = 20
export const WARM_AT = 5
/** Test-reach bands: under 40% of symbols reached is the hot end, 75% and up the cool end. */
export const REACH_LOW = 0.4
export const REACH_OK = 0.75

export function heatOf(n: { hot_spot_changes: number | null; test_reach: number | null }, by: ColourBy): Heat {
  if (by === 'hot-spots') {
    const c = n.hot_spot_changes
    if (c === null) return 'unmeasured'
    return c >= HOT_AT ? 'hot' : c >= WARM_AT ? 'warm' : 'cool'
  }
  const r = n.test_reach
  if (r === null) return 'unmeasured'
  return r < REACH_LOW ? 'hot' : r < REACH_OK ? 'warm' : 'cool'
}

export const LEGEND: Readonly<Record<ColourBy, readonly { heat: Heat; label: string }[]>> = {
  'hot-spots': [
    { heat: 'hot', label: `hot-spot (${HOT_AT}+ changes in 90 days)` },
    { heat: 'warm', label: `${WARM_AT}-${HOT_AT - 1}` },
    { heat: 'cool', label: `under ${WARM_AT}` },
    { heat: 'unmeasured', label: 'not measured' },
  ],
  'test-reach': [
    { heat: 'hot', label: `tests reach under ${Math.round(REACH_LOW * 100)}% of symbols` },
    { heat: 'warm', label: `${Math.round(REACH_LOW * 100)}-${Math.round(REACH_OK * 100) - 1}%` },
    { heat: 'cool', label: `${Math.round(REACH_OK * 100)}% and up` },
    { heat: 'unmeasured', label: 'no symbol to reach, or not measured' },
  ],
}

// ---- the view: packages collapsed until opened ------------------------------

/**
 * Past this many modules the canvas starts with ONE NODE PER PACKAGE and opens
 * a package on click: "a force layout of 2,000 modules is a hairball: the
 * built screen must start clustered and expand on click" (the frame's risk).
 */
export const COLLAPSE_AT = 60

export interface ViewNode {
  id: string
  label: string
  cluster: string
  /** A package drawn as one node, standing for `members` modules. */
  isPackage: boolean
  members: number
  symbols: number | null
  hot_spot_changes: number | null
  test_reach: number | null
  languages: string[]
}

export interface ViewEdge {
  from: string
  to: string
  weight: number
  /** The strongest edge folded into this one. */
  max_confidence: number | null
}

export interface GraphView {
  nodes: ViewNode[]
  edges: ViewEdge[]
  clusters: string[]
  collapsed: boolean
}

function sumOrNull(values: (number | null)[]): number | null {
  return values.some((v) => v === null) ? null : values.reduce<number>((a, b) => a + (b ?? 0), 0)
}

/**
 * The nodes and edges to draw. Below `COLLAPSE_AT` modules every module is a
 * node; above it every package is one node except those in `open`. A folded
 * package's test reach is null: reach is served per module as a ratio, and the
 * module's reached-symbol count that would weight it is not.
 */
export function graphView(g: ModuleGraph, open: ReadonlySet<string>, clusterByPackage = true): GraphView {
  const collapsed = clusterByPackage && g.modules.length > COLLAPSE_AT
  const clusterOf = (id: string) => (clusterByPackage ? packageOf(id) : '')
  const nodeOf = new Map<string, string>()
  const byPkg = new Map<string, GraphModule[]>()
  for (const m of g.modules) {
    const p = clusterOf(m.id)
    byPkg.set(p, [...(byPkg.get(p) ?? []), m])
  }
  const nodes: ViewNode[] = []
  for (const [pkg, members] of [...byPkg.entries()].sort(([a], [b]) => a.localeCompare(b))) {
    if (collapsed && !open.has(pkg)) {
      const id = `pkg:${pkg}`
      for (const m of members) nodeOf.set(m.id, id)
      nodes.push({
        id,
        label: pkg,
        cluster: pkg,
        isPackage: true,
        members: members.length,
        symbols: sumOrNull(members.map((m) => m.symbols)),
        hot_spot_changes: sumOrNull(members.map((m) => m.hot_spot_changes)),
        test_reach: members.length === 1 ? (members[0]?.test_reach ?? null) : null,
        languages: [...new Set(members.flatMap((m) => m.languages))].sort(),
      })
      continue
    }
    for (const m of members) {
      nodeOf.set(m.id, m.id)
      nodes.push({
        id: m.id,
        label: leafOf(m.id),
        cluster: pkg,
        isPackage: false,
        members: 1,
        symbols: m.symbols,
        hot_spot_changes: m.hot_spot_changes,
        test_reach: m.test_reach,
        languages: m.languages,
      })
    }
  }
  const folded = new Map<string, ViewEdge>()
  for (const e of g.edges) {
    const a = nodeOf.get(e.from)
    const b = nodeOf.get(e.to)
    if (a === undefined || b === undefined || a === b) continue
    const key = `${a}\u0000${b}`
    const was = folded.get(key)
    if (was === undefined) folded.set(key, { from: a, to: b, weight: e.weight, max_confidence: e.max_confidence })
    else {
      was.weight += e.weight
      was.max_confidence = was.max_confidence === null || e.max_confidence === null ? (was.max_confidence ?? e.max_confidence) : Math.max(was.max_confidence, e.max_confidence)
    }
  }
  const edges = [...folded.values()].sort((x, y) => (x.from + x.to).localeCompare(y.from + y.to))
  return { nodes, edges, clusters: [...byPkg.keys()].sort(), collapsed }
}

/** How many other drawn nodes call into / are called from this one. */
export function degree(view: GraphView, id: string): { callers: number; callees: number } {
  const callers = new Set<string>()
  const callees = new Set<string>()
  for (const e of view.edges) {
    if (e.to === id) callers.add(e.from)
    if (e.from === id) callees.add(e.to)
  }
  return { callers: callers.size, callees: callees.size }
}

// ---- the force layout ------------------------------------------------------

/** A 32-bit FNV-1a hash of a string: the seed of a node's first position. */
export function hash(s: string): number {
  let h = 0x811c9dc5
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return h >>> 0
}

export interface Placed {
  x: number
  y: number
  r: number
}

export interface Layout {
  pos: Map<string, Placed>
  /** One soft rectangle per cluster with more than one node. */
  boxes: { cluster: string; x: number; y: number; w: number; h: number }[]
  width: number
  height: number
}

/** A node's radius from its symbol count: 8 to 20, by square root; 10 when not served. */
export function radiusOf(symbols: number | null, isPackage: boolean): number {
  if (symbols === null) return isPackage ? 14 : 10
  return Math.max(8, Math.min(20, 6 + Math.sqrt(symbols) * 1.2))
}

/**
 * A deterministic force layout: clusters start on a ring, members around their
 * cluster's centre at a hashed angle, then `iterations` rounds of pairwise
 * repulsion, edge springs and a pull toward the cluster centre, cooled
 * linearly. No Math.random anywhere: the same view gives the same picture.
 */
export function forceLayout(view: GraphView, width = 640, height = 440, iterations = 160): Layout {
  const pos = new Map<string, Placed>()
  const n = view.nodes.length
  if (n === 0) return { pos, boxes: [], width, height }
  const clusters = view.clusters.length === 0 ? [''] : view.clusters
  const cx = width / 2
  const cy = height / 2
  const ring = clusters.length === 1 ? 0 : Math.min(width, height) * 0.32
  const centre = new Map<string, { x: number; y: number }>()
  clusters.forEach((c, i) => {
    const a = (2 * Math.PI * i) / clusters.length - Math.PI / 2
    centre.set(c, { x: cx + ring * Math.cos(a), y: cy + ring * Math.sin(a) })
  })
  const ids = view.nodes.map((v) => v.id)
  const xs = new Float64Array(n)
  const ys = new Float64Array(n)
  const rs = new Float64Array(n)
  const home: { x: number; y: number }[] = []
  view.nodes.forEach((v, i) => {
    const c = centre.get(v.cluster) ?? { x: cx, y: cy }
    const h = hash(v.id)
    const a = ((h % 3600) / 3600) * 2 * Math.PI
    const d = 20 + ((h >>> 12) % 50)
    xs[i] = c.x + d * Math.cos(a)
    ys[i] = c.y + d * Math.sin(a)
    rs[i] = radiusOf(v.symbols, v.isPackage)
    home.push(c)
  })
  const index = new Map(ids.map((id, i) => [id, i]))
  const springs = view.edges
    .map((e) => [index.get(e.from), index.get(e.to)] as const)
    .filter((p): p is readonly [number, number] => p[0] !== undefined && p[1] !== undefined)
  const k = Math.sqrt((width * height) / Math.max(n, 1)) * 0.55
  for (let it = 0; it < iterations; it++) {
    const t = 1 - it / iterations
    const fx = new Float64Array(n)
    const fy = new Float64Array(n)
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        let dx = xs[i]! - xs[j]!
        let dy = ys[i]! - ys[j]!
        let d2 = dx * dx + dy * dy
        if (d2 < 0.01) {
          // Two nodes on one point: part them along a hashed direction.
          const a = ((hash(ids[i]! + ids[j]!) % 360) * Math.PI) / 180
          dx = Math.cos(a)
          dy = Math.sin(a)
          d2 = 1
        }
        const d = Math.sqrt(d2)
        const f = (k * k) / d
        fx[i] = fx[i]! + (dx / d) * f
        fy[i] = fy[i]! + (dy / d) * f
        fx[j] = fx[j]! - (dx / d) * f
        fy[j] = fy[j]! - (dy / d) * f
      }
    }
    for (const [a, b] of springs) {
      const dx = xs[b]! - xs[a]!
      const dy = ys[b]! - ys[a]!
      const d = Math.max(Math.sqrt(dx * dx + dy * dy), 0.01)
      const f = (d * d) / k / 4
      fx[a] = fx[a]! + (dx / d) * f
      fy[a] = fy[a]! + (dy / d) * f
      fx[b] = fx[b]! - (dx / d) * f
      fy[b] = fy[b]! - (dy / d) * f
    }
    const step = 12 * t + 0.5
    for (let i = 0; i < n; i++) {
      const h = home[i]!
      fx[i] = fx[i]! + (h.x - xs[i]!) * 0.9
      fy[i] = fy[i]! + (h.y - ys[i]!) * 0.9
      const m = Math.sqrt(fx[i]! * fx[i]! + fy[i]! * fy[i]!)
      if (m > 0) {
        xs[i] = xs[i]! + (fx[i]! / m) * Math.min(m, step)
        ys[i] = ys[i]! + (fy[i]! / m) * Math.min(m, step)
      }
      const pad = rs[i]! + 18
      xs[i] = Math.min(width - pad, Math.max(pad, xs[i]!))
      ys[i] = Math.min(height - pad, Math.max(pad + 14, ys[i]!))
    }
  }
  view.nodes.forEach((v, i) => pos.set(v.id, { x: Math.round(xs[i]! * 10) / 10, y: Math.round(ys[i]! * 10) / 10, r: rs[i]! }))
  const boxes: Layout['boxes'] = []
  for (const c of view.clusters) {
    const members = view.nodes.filter((v) => v.cluster === c).map((v) => pos.get(v.id)!)
    if (members.length < 2 || c === '') continue
    const x0 = Math.min(...members.map((p) => p.x - p.r)) - 14
    const y0 = Math.min(...members.map((p) => p.y - p.r)) - 24
    const x1 = Math.max(...members.map((p) => p.x + p.r)) + 14
    const y1 = Math.max(...members.map((p) => p.y + p.r)) + 22
    boxes.push({ cluster: c, x: x0, y: y0, w: x1 - x0, h: y1 - y0 })
  }
  return { pos, boxes, width, height }
}

// ---------------------------------------------------------------------------
// GET /symbols: search, one symbol's call graph, its test map
// ---------------------------------------------------------------------------

export interface SymbolRow {
  id: string
  kind: string | null
  path: string | null
  start_line: number | null
  end_line: number | null
  language: string | null
}

function symbolRow(v: unknown): SymbolRow | null {
  if (!isRec(v)) return null
  const id = str(v.id)
  return id === null
    ? null
    : { id, kind: str(v.kind), path: str(v.path), start_line: num(v.start_line), end_line: num(v.end_line), language: str(v.language) }
}

export interface SymbolSearch {
  symbols: SymbolRow[]
  more: boolean
}

export function normSymbolSearch(v: unknown): SymbolSearch | null {
  if (!isRec(v)) return null
  return { symbols: (Array.isArray(v.symbols) ? v.symbols : []).map(symbolRow).filter((s): s is SymbolRow => s !== null), more: v.more === true }
}

/** How an edge is known (repo-index.md §2.5): what the legend and each edge's look say. */
export type Evidence = 'lsp' | 'ast' | 'import' | 'naming' | 'co-change'
export const EVIDENCE: readonly Evidence[] = ['lsp', 'ast', 'import', 'naming', 'co-change']

/** Below this an edge is drawn dotted and the impact query flags it (§4.3a, `LOW_CONFIDENCE`). */
export const LOW_CONFIDENCE = 0.4

export const EVIDENCE_LEGEND: readonly { key: Evidence | 'low'; label: string }[] = [
  { key: 'lsp', label: 'lsp: resolved by the language server' },
  { key: 'ast', label: 'ast: syntactic match' },
  { key: 'import', label: 'import: the file imports the module, no call resolved' },
  { key: 'low', label: `below ${LOW_CONFIDENCE} confidence` },
]

export interface CallEdge {
  from: string
  to: string
  kind: string | null
  evidence: Evidence | null
  also: string[]
  confidence: number | null
}

export interface CallNode extends SymbolRow {
  distance: number | null
}

export interface CallGraph {
  symbol: SymbolRow
  depth: number | null
  direction: string | null
  nodes: CallNode[]
  edges: CallEdge[]
  truncated: boolean
}

export function normCallGraph(v: unknown): CallGraph | null {
  if (!isRec(v)) return null
  const symbol = symbolRow(v.symbol)
  if (symbol === null) return null
  const nodes = recs(v.nodes)
    .map((n): CallNode | null => {
      const row = symbolRow(n)
      return row === null ? null : { ...row, distance: num(n.distance) }
    })
    .filter((n): n is CallNode => n !== null)
  const edges = recs(v.edges)
    .map((e): CallEdge | null => {
      const from = str(e.from)
      const to = str(e.to)
      if (from === null || to === null) return null
      const ev = typeof e.evidence === 'string' && (EVIDENCE as readonly string[]).includes(e.evidence) ? (e.evidence as Evidence) : null
      return { from, to, kind: str(e.kind), evidence: ev, also: strs(e.also_evidence), confidence: num(e.confidence) }
    })
    .filter((e): e is CallEdge => e !== null)
  return { symbol, depth: num(v.depth), direction: str(v.direction), nodes, edges, truncated: v.truncated === true }
}

/** An edge's look: its evidence, or `low` under the confidence floor, which wins. */
export function edgeLook(e: { evidence: Evidence | null; confidence: number | null }): Evidence | 'low' | 'unknown' {
  if (e.confidence !== null && e.confidence < LOW_CONFIDENCE) return 'low'
  return e.evidence ?? 'unknown'
}

export function edgeWords(e: CallEdge): string {
  const conf = e.confidence === null ? 'confidence not served' : `confidence ${e.confidence}`
  const ev = e.evidence ?? 'evidence not served'
  const also = e.also.length > 0 ? ` · also ${e.also.join(', ')}` : ''
  return `${short(e.from)} → ${short(e.to)} · ${ev} · ${conf}${also}`
}

export type Side = 'centre' | 'caller' | 'callee'

/**
 * Which side of the centre each node sits on, and how far: callers left at
 * -d, callees right at +d. The route serves one distance per node, not its
 * side, so the side is the walk over the served edges: a node reached by
 * walking callers is a caller. One reached both ways is drawn as a caller.
 */
export function callColumns(g: CallGraph): Map<string, { side: Side; depth: number }> {
  const out = new Map<string, { side: Side; depth: number }>([[g.symbol.id, { side: 'centre', depth: 0 }]])
  const walk = (side: 'caller' | 'callee') => {
    let frontier = [g.symbol.id]
    const seen = new Set(frontier)
    for (let d = 1; frontier.length > 0 && d <= 6; d++) {
      const next: string[] = []
      for (const at of frontier) {
        for (const e of g.edges) {
          const other = side === 'caller' ? (e.to === at ? e.from : null) : (e.from === at ? e.to : null)
          if (other === null || seen.has(other)) continue
          seen.add(other)
          next.push(other)
          if (!out.has(other)) out.set(other, { side, depth: d })
        }
      }
      frontier = next.sort()
    }
  }
  walk('caller')
  walk('callee')
  return out
}

/** `src/core/orders.py#OrderService.total` -> `OrderService.total`; an external one keeps its name. */
export function short(id: string): string {
  const at = id.indexOf('#')
  if (at !== -1) return id.slice(at + 1)
  return id.startsWith('external:') ? id.slice('external:'.length) : id
}

export function linesWord(s: { start_line: number | null; end_line: number | null }): string | null {
  if (s.start_line === null) return null
  return s.end_line === null || s.end_line === s.start_line ? `${s.start_line}` : `${s.start_line}-${s.end_line}`
}

export interface SymbolTest {
  test: string
  depth: number | null
  confidence: number | null
  command: string | null
}

export interface SymbolTests {
  symbol: SymbolRow
  tests: SymbolTest[]
}

export function normSymbolTests(v: unknown): SymbolTests | null {
  if (!isRec(v)) return null
  const symbol = symbolRow(v.symbol)
  if (symbol === null) return null
  const tests = recs(v.tests)
    .map((t): SymbolTest | null => {
      const test = str(t.test)
      return test === null ? null : { test, depth: num(t.depth), confidence: num(t.confidence), command: str(t.command) }
    })
    .filter((t): t is SymbolTest => t !== null)
  return { symbol, tests }
}

/** The call graph's depth bound (impact.py `DEPTH_MAX`) and its default (`NEIGHBOURHOOD_DEPTH_DEFAULT`). */
export const DEPTH_MIN = 1
export const DEPTH_MAX = 6
export const DEPTH_DEFAULT = 2

export function clampDepth(d: number): number {
  return Math.max(DEPTH_MIN, Math.min(DEPTH_MAX, Math.round(d)))
}

// ---------------------------------------------------------------------------
// POST /impact: the test plan
// ---------------------------------------------------------------------------

export interface LineRange {
  start: number
  end: number
}

function ranges(v: unknown): LineRange[] {
  return recs(v)
    .map((r) => {
      const start = num(r.start)
      const end = num(r.end)
      return start === null || end === null ? null : { start, end }
    })
    .filter((r): r is LineRange => r !== null)
}

export function lineCount(rs: readonly LineRange[]): number {
  return rs.reduce((n, r) => n + (r.end - r.start + 1), 0)
}

export interface DiffRow {
  path: string
  status: string | null
  previous_path: string | null
  /** Whether the forge served a patch to count lines from. */
  patch: boolean
  added: LineRange[]
  removed: LineRange[]
}

export interface ChangedRow {
  id: string
  kind: string | null
  path: string | null
  start_line: number | null
  end_line: number | null
  side: string | null
  why: string | null
}

export interface AffectedRow {
  id: string
  path: string | null
  depth: number | null
  confidence: number | null
  evidence: string | null
  reaches: string | null
}

export interface PlanTest {
  id: string
  command: string | null
  reason: string | null
  evidence: string[]
  confidence: number | null
  source: string | null
}

export interface Trigger {
  kind: string
  reason: string
  path: string | null
}

export interface Cut {
  from: string
  to: string
  depth: number | null
  confidence: number | null
  evidence: string | null
}

export interface Unindexed {
  path: string
  reason: string | null
}

export interface ImpactPlan extends Staleness {
  plan_id: string | null
  pull_request: number | null
  commit: string | null
  base_sha: string | null
  policy: string | null
  depth: number | null
  min_confidence: number | null
  changed_symbols: number | null
  affected_callers: number | null
  targeted: number | null
  selected: number | null
  total_tests: number | null
  selection: 'targeted' | 'full_suite' | null
  full_suite_because: string[] | null
  diff: DiffRow[]
  diff_truncated: boolean
  changed: ChangedRow[]
  affected: AffectedRow[]
  tests: PlanTest[]
  fallback_triggers: Trigger[]
  unindexed: Unindexed[]
  low_confidence_cut: Cut[]
  node_cap_hit: boolean
  lists_cut: string[]
}

export function normImpact(v: unknown): ImpactPlan | null {
  if (!isRec(v)) return null
  const full = isRec(v.full_suite) ? v.full_suite : null
  const bound = isRec(v.bound) ? v.bound : {}
  return {
    ...normStaleness(v),
    plan_id: str(v.plan_id),
    pull_request: num(v.pull_request),
    commit: str(v.commit),
    base_sha: str(v.base_sha),
    head_sha: str(v.head_sha),
    policy: str(v.policy),
    depth: num(v.depth),
    min_confidence: num(v.min_confidence),
    changed_symbols: num(v.changed_symbols),
    affected_callers: num(v.affected_callers),
    targeted: num(v.targeted),
    selected: num(v.selected),
    total_tests: num(v.total_tests),
    selection: v.selection === 'targeted' || v.selection === 'full_suite' ? v.selection : null,
    full_suite_because: full === null ? null : strs(full.because),
    diff: recs(v.diff)
      .map((d): DiffRow | null => {
        const path = str(d.path)
        return path === null ? null : { path, status: str(d.status), previous_path: str(d.previous_path), patch: d.patch === true, added: ranges(d.added), removed: ranges(d.removed) }
      })
      .filter((d): d is DiffRow => d !== null),
    diff_truncated: v.diff_truncated === true,
    changed: recs(v.changed)
      .map((c): ChangedRow | null => {
        const id = str(c.id)
        return id === null ? null : { id, kind: str(c.kind), path: str(c.path), start_line: num(c.start_line), end_line: num(c.end_line), side: str(c.side), why: str(c.why) }
      })
      .filter((c): c is ChangedRow => c !== null),
    affected: recs(v.affected)
      .map((a): AffectedRow | null => {
        const id = str(a.id)
        return id === null ? null : { id, path: str(a.path), depth: num(a.depth), confidence: num(a.confidence), evidence: str(a.evidence), reaches: str(a.reaches) }
      })
      .filter((a): a is AffectedRow => a !== null),
    tests: recs(v.tests)
      .map((t): PlanTest | null => {
        const id = str(t.id)
        return id === null ? null : { id, command: str(t.command), reason: str(t.reason), evidence: strs(t.evidence), confidence: num(t.confidence), source: str(t.source) }
      })
      .filter((t): t is PlanTest => t !== null),
    fallback_triggers: recs(v.fallback_triggers)
      .map((t): Trigger | null => {
        const kind = str(t.kind)
        return kind === null ? null : { kind, reason: str(t.reason) ?? kind, path: str(t.path) }
      })
      .filter((t): t is Trigger => t !== null),
    unindexed: recs(v.unindexed)
      .map((u): Unindexed | null => {
        const path = str(u.path)
        return path === null ? null : { path, reason: str(u.reason) }
      })
      .filter((u): u is Unindexed => u !== null),
    low_confidence_cut: recs(v.low_confidence_cut)
      .map((c): Cut | null => {
        const from = str(c.from)
        const to = str(c.to)
        return from === null || to === null ? null : { from, to, depth: num(c.depth), confidence: num(c.confidence), evidence: str(c.evidence) }
      })
      .filter((c): c is Cut => c !== null),
    node_cap_hit: bound.node_cap_hit === true,
    lists_cut: strs(v.lists_cut),
  }
}

/** "1,480" -- a count as the frames write it. */
export function fmt(n: number): string {
  return n.toLocaleString('en-US')
}

/**
 * The full suite's line on the PR card (P3, screen 10): not needed, or the
 * fallback with its first reason. Null when the selection was not served.
 */
export function fullSuiteWords(p: ImpactPlan): { needed: boolean; words: string; reason: string | null } | null {
  if (p.selection === null) return null
  if (p.selection === 'targeted') {
    return { needed: false, words: 'not required for this change', reason: 'no fallback trigger: every changed symbol is reached by a test above the confidence floor' }
  }
  const first = p.fallback_triggers[0]
  const more = p.fallback_triggers.length > 1 ? ` (and ${p.fallback_triggers.length - 1} more)` : ''
  return { needed: true, words: 'fallback', reason: first === undefined ? 'the plan named no trigger' : `${first.reason}${more}` }
}
