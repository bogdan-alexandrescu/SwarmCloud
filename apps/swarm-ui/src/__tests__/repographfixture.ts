// THE GR3 ACCEPTANCE FIXTURES (docs/design/graph-rendering.md §7, lane GR3):
// the raw `GET /v1/repositories/{id}/graph` body for a conventional src/
// repository, seeded, so every read, every test and every Chromium
// measurement draws the same graph.
//
// The layout is the one the design measured clustering into two nodes:
// seven packages under `src/` plus `lib/shared`. Leaves repeat across
// packages (`src/api/orders.py`, `src/core/orders.py`, ...), which is the
// label collision the disambiguation pass exists for. Past 400 modules each
// package also has subdirectories, so a 2,000-module repository has a tree to
// walk, not a flat list.
//
// It replaces nothing the GFY prototype's `protoGraphBody` served: that lives
// under src/proto/, which lane GR1 deletes.

export const SRC_PACKAGES = ['src/api', 'src/core', 'src/db', 'src/workers', 'src/ui', 'src/auth', 'src/billing', 'lib/shared'] as const
const LEAVES = ['orders', 'money', 'tax', 'users', 'auth', 'queue', 'cache', 'routes', 'models', 'schema', 'jobs', 'views', 'forms', 'client']
const SUBDIRS = ['handlers', 'models', 'services', 'utils']
/** Which packages a package's modules call: ui -> api -> core -> db, workers -> core, all -> shared. */
const CALLS: Record<string, readonly string[]> = {
  'src/api': ['src/core', 'src/auth', 'lib/shared'],
  'src/core': ['src/db', 'lib/shared'],
  'src/db': ['lib/shared'],
  'src/workers': ['src/core', 'src/db', 'lib/shared'],
  'src/ui': ['src/api', 'lib/shared'],
  'src/auth': ['src/db', 'lib/shared'],
  'src/billing': ['src/core', 'src/db', 'lib/shared'],
  'lib/shared': [],
}

function rng(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

/** The ids of an `n`-module src/ repository, round-robin over the packages. */
export function srcModuleIds(n: number): string[] {
  const nested = n > 400
  const ids: string[] = []
  for (let i = 0; i < n; i++) {
    const pkg = SRC_PACKAGES[i % SRC_PACKAGES.length]!
    const k = Math.floor(i / SRC_PACKAGES.length)
    const leaf = LEAVES[k % LEAVES.length]!
    if (nested) {
      const sub = SUBDIRS[Math.floor(k / LEAVES.length) % SUBDIRS.length]!
      const round = Math.floor(k / (LEAVES.length * SUBDIRS.length))
      ids.push(`${pkg}/${sub}/${leaf}${round === 0 ? '' : `_${round}`}.py`)
    } else {
      const round = Math.floor(k / LEAVES.length)
      ids.push(`${pkg}/${leaf}${round === 0 ? '' : `_${round}`}.py`)
    }
  }
  return ids
}

export function srcGraphBody(n: number, seed = 7): Record<string, unknown> {
  const r = rng(seed)
  const ids = srcModuleIds(n)
  const pkgOf = (id: string) => SRC_PACKAGES.find((p) => id.startsWith(`${p}/`))!
  const byPkg = new Map<string, string[]>()
  for (const id of ids) byPkg.set(pkgOf(id), [...(byPkg.get(pkgOf(id)) ?? []), id])
  const edges: Record<string, unknown>[] = []
  const seen = new Set<string>()
  for (const id of ids) {
    const own = byPkg.get(pkgOf(id))!
    const targets = CALLS[pkgOf(id)]!
    const calls = 1 + Math.floor(r() * 3)
    for (let k = 0; k < calls; k++) {
      const pool = r() < 0.45 || targets.length === 0 ? own : byPkg.get(targets[Math.floor(r() * targets.length)]!)!
      const to = pool[Math.floor(r() * pool.length)]!
      const key = `${id}>${to}`
      if (to === id || seen.has(key)) continue
      seen.add(key)
      const weight = 1 + Math.floor(r() * 20)
      edges.push({ from: id, to, weight, kinds: { call: weight }, max_confidence: r() < 0.15 ? 0.3 : 0.95 })
    }
  }
  const sha = 'a1b2c3d'.padEnd(40, '0')
  return {
    index_sha: sha,
    head_sha: sha,
    behind_by: 0,
    stale: false,
    freshness: { state: 'current' },
    cluster: 'module',
    modules: ids.map((id) => ({
      id,
      modules: 1,
      symbols: 4 + Math.floor(r() * 60),
      tests: Math.floor(r() * 4),
      hot_spot_changes: r() < 0.1 ? null : Math.floor(r() * 30),
      test_reach: r() < 0.1 ? null : Math.round(r() * 100) / 100,
      languages: ['python'],
    })),
    edges,
    counts: { files: n * 3, symbols: n * 30, edges: edges.length * 40 },
    truncated: [],
  }
}
