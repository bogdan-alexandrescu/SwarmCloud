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
 * THE LAYOUT (RepoGraphLayout.ts, its own module so it leaves the main
 * bundle: the worker and a lazy import load it) IS DEPENDENCY-FREE. A force layout of a few hundred nodes is a
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

// ---- clusters on real structure ---------------------------------------------

/**
 * STRUCTURAL CLUSTERING (design graph-rendering.md §3.2 defect 7). `packageOf`
 * above is the route's package: the first segment, two under a monorepo root.
 * A conventional `src/api`, `src/core`, ... layout is all `src` to it, so a
 * 200-module src/ repository folded into two nodes (`src`, `lib`) and the
 * rectangles overlapped. The console now clusters by the DIRECTORY TREE the
 * modules actually sit in:
 *
 *   * a group whose members all share one next directory is not a split: the
 *     walk descends through it (`apps` -> `apps/swarm-api` -> ...), so a
 *     pass-through root never becomes a cluster of its own;
 *   * the biggest cluster of at least CLUSTER_SPLIT_AT modules is split into
 *     its subdirectories, again and again, while the total stays within
 *     CLUSTER_MAX -- a dozen-odd groups is what a reader can tell apart;
 *   * modules sitting directly in a directory that also has subdirectories
 *     stay together under that directory's own name.
 *
 * Every cluster is a real directory prefix of its members: nothing is
 * inferred from edges, so a cluster's name is always a path the reader can
 * find in the repository. A community id served by the indexer (Graphify's
 * Leiden step) would replace this rule if the graph route ever serves one.
 */
export const CLUSTER_SPLIT_AT = 8
export const CLUSTER_MAX = 16

interface Part<M> {
  key: string
  /** How many directory segments `key` spans: where a further split looks next. */
  depth: number
  members: M[]
  /** Its members have no deeper directory to split on. */
  final: boolean
}

/** A module's directories: every segment but its own last one (`src/api/orders.py` -> src, api). */
function dirsOf(id: string): string[] {
  if (id === '.') return []
  return id.split('/').filter((p) => p !== '').slice(0, -1)
}

function splitPart<M extends { id: string }>(p: Part<M>, dirs: (m: M) => string[]): Part<M>[] | null {
  for (let d = p.depth; ; d++) {
    const groups = new Map<string, M[]>()
    const rest: M[] = []
    for (const m of p.members) {
      const ds = dirs(m)
      const seg = ds[d]
      if (seg === undefined) rest.push(m)
      else {
        const g = groups.get(seg)
        if (g === undefined) groups.set(seg, [m])
        else g.push(m)
      }
    }
    if (groups.size === 0) return null
    // Every member is in one subdirectory: descend through it, it is not a split.
    if (groups.size === 1 && rest.length === 0) continue
    const at = (m: M, n: number) => dirs(m).slice(0, n).join('/')
    const parts: Part<M>[] = [...groups.values()].map((ms) => ({ key: at(ms[0]!, d + 1), depth: d + 1, members: ms, final: false }))
    if (rest.length > 0) parts.push({ key: at(rest[0]!, d) || '.', depth: d, members: rest, final: true })
    return parts
  }
}

/**
 * The members of one directory (`prefixDepth` segments deep; 0 is the whole
 * repository), cut into clusters by the rule above. Sorted by name.
 */
export function structuralClusters<M extends { id: string }>(members: readonly M[], prefixDepth = 0): { key: string; depth: number; members: M[] }[] {
  const dirs = (m: M) => dirsOf(m.id)
  const root: Part<M> = { key: '', depth: prefixDepth, members: [...members], final: false }
  let parts = splitPart(root, dirs) ?? [{ ...root, key: members[0] === undefined ? '.' : dirs(members[0]).slice(0, prefixDepth).join('/') || '.', final: true }]
  for (;;) {
    const next = parts
      .filter((p) => !p.final && p.members.length >= CLUSTER_SPLIT_AT)
      .sort((a, b) => b.members.length - a.members.length || a.key.localeCompare(b.key))[0]
    if (next === undefined) break
    const split = splitPart(next, dirs)
    if (split === null || parts.length - 1 + split.length > CLUSTER_MAX) {
      next.final = true
      continue
    }
    parts = [...parts.filter((p) => p !== next), ...split]
  }
  // A cluster is named for the deepest directory all its members share: `lib/shared`, not `lib`.
  return parts
    .map(({ key, depth, members: ms }) => {
      const common = commonDir(ms.map(dirs))
      return { key: common.length > key.length && key !== '.' ? common : key, depth, members: ms }
    })
    .sort((a, b) => a.key.localeCompare(b.key))
}

function commonDir(all: string[][]): string {
  const first = all[0] ?? []
  let n = first.length
  for (const ds of all) {
    let i = 0
    while (i < n && ds[i] === first[i]) i++
    n = i
  }
  return first.slice(0, n).join('/')
}

// ---- the view: clusters folded until opened ---------------------------------

/**
 * Past this many modules the canvas starts with ONE NODE PER CLUSTER -- the
 * meta-graph, its edges the calls crossing between clusters -- and opens a
 * cluster on click: "a force layout of 2,000 modules is a hairball: the built
 * screen must start clustered and expand on click" (the frame's risk). An
 * opened cluster that is itself past the bound opens into ITS clusters, so a
 * 2,000-module repository is walked a directory level at a time and never
 * drawn whole unless the reader turns clustering off.
 */
export const COLLAPSE_AT = 60

export interface ViewNode {
  id: string
  /** What the canvas prints: unique within the view (`uniqueLabels`). */
  label: string
  cluster: string
  /** A cluster drawn as one node, standing for `members` modules. */
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

/** What a node says on the canvas: a folded cluster carries its member count. */
export function displayLabel(n: Pick<ViewNode, 'label' | 'isPackage' | 'members'>): string {
  return n.isPackage ? `${n.label} (${n.members})` : n.label
}

/** A module's names from shortest to longest: `orders`, `api/orders`, `src/api/orders`, then the id itself. */
function nameLadder(id: string): string[] {
  const parts = id.split('/').filter((p) => p !== '')
  if (parts.length === 0) return [id]
  const leaf = leafOf(id)
  const out: string[] = []
  for (let k = 1; k <= parts.length; k++) out.push([...parts.slice(parts.length - k, -1), leaf].join('/'))
  out.push(id)
  return [...new Set(out)]
}

/**
 * DISAMBIGUATED LABELS (§3.2 defect 10). `leafOf` printed `orders` for
 * `src/api/orders.py`, `src/core/orders.py` and `src/db/orders.py` alike.
 * Every module starts at its leaf; every group that still collides climbs one
 * directory, until no two printed labels in the view are equal. A folded
 * cluster's label is its full directory, already unique among clusters. The
 * last rung is the module's id, which is unique by the route.
 */
export function uniqueLabels(nodes: readonly ViewNode[]): ViewNode[] {
  const ladders = nodes.map((n) => (n.isPackage ? [n.label] : nameLadder(n.id)))
  const rung = nodes.map(() => 0)
  const text = (i: number) => {
    const n = nodes[i]!
    const l = ladders[i]![rung[i]!]!
    return n.isPackage ? `${l} (${n.members})` : l
  }
  for (;;) {
    const seen = new Map<string, number[]>()
    nodes.forEach((_, i) => {
      const t = text(i)
      const idx = seen.get(t)
      if (idx === undefined) seen.set(t, [i])
      else idx.push(i)
    })
    let moved = false
    for (const idx of seen.values()) {
      if (idx.length < 2) continue
      for (const i of idx) {
        if (rung[i]! < ladders[i]!.length - 1) {
          rung[i] = rung[i]! + 1
          moved = true
        }
      }
    }
    if (!moved) break
  }
  const out = nodes.map((n, i) => ({ ...n, label: ladders[i]![rung[i]!]! }))
  // A module whose full id prints like a folded cluster's label ("lib (3)"): name it by its id outright.
  const counts = new Map<string, number>()
  for (const n of out) counts.set(displayLabel(n), (counts.get(displayLabel(n)) ?? 0) + 1)
  return out.map((n) => (!n.isPackage && (counts.get(displayLabel(n)) ?? 0) > 1 ? { ...n, label: `${n.label} · module` } : n))
}

/**
 * The nodes and edges to draw. At or below `COLLAPSE_AT` modules every module
 * is a node, inside its cluster's rectangle; above it every cluster is one
 * node except those in `open`. An opened cluster past the bound folds into its
 * own sub-clusters. A folded cluster's test reach is null: reach is served per
 * module as a ratio, and the module's reached-symbol count that would weight
 * it is not. With `clusterByPackage` off, every module is drawn, unclustered.
 */
export function graphView(g: ModuleGraph, open: ReadonlySet<string>, clusterByPackage = true): GraphView {
  const nodeOf = new Map<string, string>()
  const nodes: ViewNode[] = []
  const clusters = new Set<string>()
  const moduleNode = (m: GraphModule, cluster: string): ViewNode => {
    nodeOf.set(m.id, m.id)
    return { id: m.id, label: leafOf(m.id), cluster, isPackage: false, members: 1, symbols: m.symbols, hot_spot_changes: m.hot_spot_changes, test_reach: m.test_reach, languages: m.languages }
  }
  const collapsed = clusterByPackage && g.modules.length > COLLAPSE_AT
  const emit = (parts: { key: string; depth: number; members: GraphModule[] }[], fold: boolean) => {
    for (const p of parts) {
      if (fold && !open.has(p.key)) {
        const id = `pkg:${p.key}`
        for (const m of p.members) nodeOf.set(m.id, id)
        clusters.add(p.key)
        nodes.push({
          id,
          label: p.key,
          cluster: p.key,
          isPackage: true,
          members: p.members.length,
          symbols: sumOrNull(p.members.map((m) => m.symbols)),
          hot_spot_changes: sumOrNull(p.members.map((m) => m.hot_spot_changes)),
          test_reach: p.members.length === 1 ? (p.members[0]?.test_reach ?? null) : null,
          languages: [...new Set(p.members.flatMap((m) => m.languages))].sort(),
        })
        continue
      }
      if (fold && p.members.length > COLLAPSE_AT) {
        const sub = structuralClusters(p.members, p.depth)
        if (sub.length > 1) {
          emit(sub, true)
          continue
        }
      }
      clusters.add(p.key)
      for (const m of p.members) nodes.push(moduleNode(m, p.key))
    }
  }
  if (clusterByPackage) emit(structuralClusters(g.modules), collapsed)
  else for (const m of [...g.modules].sort((a, b) => byCode(a.id, b.id))) nodes.push(moduleNode(m, ''))
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
  const edges = [...folded.values()].sort((x, y) => byCode(x.from, y.from) || byCode(x.to, y.to))
  return { nodes: uniqueLabels(nodes), edges, clusters: clusterByPackage ? [...clusters].sort() : [], collapsed }
}

/**
 * Order by code point. `localeCompare` sorting the ~4,000 edges of an
 * unclustered 2,000-module view was most of a 103 ms main-thread task; ids
 * are paths, and only a stable order matters here, not a collation.
 */
function byCode(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0
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

/** One drawn neighbour of a node, with the resolved calls of the folded edge between them. */
export interface Neighbour {
  node: ViewNode
  weight: number
  max_confidence: number | null
}

/**
 * THE INSPECTOR'S NEIGHBOUR LIST (§3.2 defect 11): who calls this node and
 * whom it calls, as nodes of the same view, heaviest first. Read off the
 * view's own edges, so a neighbour is always a node the canvas draws.
 */
export function neighbours(view: GraphView, id: string): { callers: Neighbour[]; callees: Neighbour[] } {
  const byId = new Map(view.nodes.map((n) => [n.id, n]))
  const callers: Neighbour[] = []
  const callees: Neighbour[] = []
  for (const e of view.edges) {
    const other = e.to === id ? byId.get(e.from) : e.from === id ? byId.get(e.to) : undefined
    if (other === undefined) continue
    ;(e.to === id ? callers : callees).push({ node: other, weight: e.weight, max_confidence: e.max_confidence })
  }
  const order = (a: Neighbour, b: Neighbour) => b.weight - a.weight || a.node.label.localeCompare(b.node.label)
  return { callers: callers.sort(order), callees: callees.sort(order) }
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
