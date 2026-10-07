/**
 * WORK › REPOSITORIES: the shapes the screens read, and the rules that turn a
 * served record into what a card draws (repositories.html, picked 2026-10-05).
 *
 * THE BACKEND IS BUILT IN PARALLEL (lanes RI1 registrations, RI2 index runs,
 * GT1 token registry), so nothing here trusts a field to be present. Every
 * served value goes through a normaliser that keeps it only when it has the
 * type the design names (docs/repo-index.md §6, docs/git-tokens.md §7) and
 * otherwise makes it null -- and a null is drawn as a dash with its reason,
 * never as 0, "current" or "ok" (redesign-v2.md, "Honesty rules").
 *
 * NO TOKEN VALUE IS EVER READ OUT OF A RESPONSE. The token normaliser copies
 * the metadata fields by name and nothing else, so a response that carried a
 * value by mistake still could not put it on the page (owner rule 2026-09-25;
 * git-tokens.md §5.4). `last4` is the one part of a value any route serves,
 * and it is kept because the owner asked to see it.
 */
import type { ApiError } from './fetch'
import type { TaskState } from './types'

// ---------------------------------------------------------------------------
// Reading served JSON without trusting it
// ---------------------------------------------------------------------------

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
function bool(v: unknown): boolean | null {
  return typeof v === 'boolean' ? v : null
}
function oneOf<K extends string>(v: unknown, keys: readonly K[]): K | null {
  return typeof v === 'string' && (keys as readonly string[]).includes(v) ? (v as K) : null
}
function strs(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string' && x !== '') : []
}
function recs(v: unknown): Rec[] {
  return Array.isArray(v) ? v.filter(isRec) : []
}

// ---------------------------------------------------------------------------
// "Not served yet"
// ---------------------------------------------------------------------------

/**
 * Whether a failure means THE ROUTE IS NOT THERE rather than "this record is
 * missing". FastAPI answers an unknown path with `{"detail": "Not Found"}`,
 * which carries no `code`; a missing repository is the API's own envelope
 * with one (`not_found`). 405 is a path that exists for another method, and
 * 501 is a route declared and not built. Anything else is a real failure and
 * is drawn as one.
 */
export function notServed(e: ApiError): boolean {
  if (e.httpStatus === 501 || e.httpStatus === 405) return true
  return e.httpStatus === 404 && e.code === null
}

// ---------------------------------------------------------------------------
// Repositories (repo-index.md §1, §6.2)
// ---------------------------------------------------------------------------

export type ChangeTrigger = 'poll' | 'webhook' | 'off'

/** `index.interval_hours` as designed: 1-168, or `'off'`. Anything else is not that, so unknown. */
function intervalHours(v: unknown): number | 'off' | null {
  if (v === 'off') return 'off'
  const n = num(v)
  return n !== null && Number.isInteger(n) && n >= 1 && n <= 168 ? n : null
}

export interface RepoIndexState {
  /** 1-168 hours, `'off'`, or null when not served (repo-index.md §3.3). */
  interval_hours: number | 'off' | null
  /** What a move of the default branch does (repo-index.md §3.3); null when not served. */
  on_change: ChangeTrigger | null
  min_change_interval_minutes: number | null
  full_every_days: number | null
  paused: boolean | null
  current_sha: string | null
  last_indexed_at: string | null
  last_kind: string | null
  head_sha: string | null
  head_read_at: string | null
  behind_by: number | null
  stale: boolean | null
  in_flight_task_id: string | null
  /** The test map's reach in the index's own units (QA G4-03); null when not served. */
  coverage: TestCoverage | null
  next_run_at: string | null
}

/**
 * `index.coverage` AS THE API SERVES IT: counts, not a ratio
 * (`swarm_api/repoindex.py` `coverage()`, repo-index.md §6.2). The unit is
 * the index's MODULES -- directories -- with at least one test-map edge, not
 * source files: an edge's source is a glob, so a per-file share is not
 * something the index can say. Edges, not executed coverage. Each figure is
 * null when its key was not served; a 0-1 number (the shape first designed,
 * never served) is not read as one of these, so it is unknown.
 */
export interface TestCoverage {
  modules: number | null
  modules_with_tests: number | null
  test_map_edges: number | null
  always_tests: number | null
}

function count(v: unknown): number | null {
  const n = num(v)
  return n !== null && Number.isInteger(n) && n >= 0 ? n : null
}

function normCoverage(v: unknown): TestCoverage | null {
  if (!isRec(v)) return null
  const c = {
    modules: count(v.modules),
    modules_with_tests: count(v.modules_with_tests),
    test_map_edges: count(v.test_map_edges),
    always_tests: count(v.always_tests),
  }
  return Object.values(c).every((x) => x === null) ? null : c
}

/** Modules with a test edge ÷ modules, 0-1: the bar's fill. Null when either is unknown, or there are no modules. */
export function coverageRatio(c: TestCoverage | null): number | null {
  if (c === null || c.modules === null || c.modules_with_tests === null || c.modules === 0) return null
  return c.modules_with_tests > c.modules ? null : c.modules_with_tests / c.modules
}

/** "20 of 83 modules"; null when either count is unknown. */
export function coverageWords(c: TestCoverage | null): string | null {
  if (c === null || c.modules === null || c.modules_with_tests === null) return null
  return `${thousands(c.modules_with_tests)} of ${thousands(c.modules)} module${c.modules === 1 ? '' : 's'}`
}

/** "1,339 edges · 7 always-run": what stands behind the modules figure, each part only when served. */
export function coverageDetail(c: TestCoverage | null): string | null {
  if (c === null) return null
  const parts = [
    c.test_map_edges === null ? null : `${thousands(c.test_map_edges)} edge${c.test_map_edges === 1 ? '' : 's'}`,
    c.always_tests === null ? null : `${thousands(c.always_tests)} always-run`,
  ].filter((x): x is string => x !== null)
  return parts.length === 0 ? null : parts.join(' · ')
}

/** A count with thousands separators, the same in every locale. */
export function thousands(n: number): string {
  return n.toLocaleString('en-US')
}

export interface IndexRun {
  task_id: string
  commit_sha: string | null
  kind: string | null
  trigger: string | null
  state: TaskState | null
  queued_at: string | null
  started_at: string | null
  ended_at: string | null
  end_cause: string | null
}

export interface RepoRecord {
  repo_id: string
  owner: string
  repo: string
  default_branch: string | null
  allowed_profiles: string[]
  created_by: string | null
  created_at: string | null
  index: RepoIndexState
  last_run: IndexRun | null
  graph: { depth: number | null; min_confidence: number | null }
  selection_policy: { policy: string | null; mode: string | null; inherited_from_tenant: boolean | null }
}

export interface UsedBy {
  kind: 'run' | 'workflow' | 'task'
  id: string
  title: string | null
  index_sha: string | null
  state: string | null
  role: string | null
}

export interface RepoDetail {
  repository: RepoRecord
  /** Null when the detail did not serve the array: not served is not "none". */
  index_runs: IndexRun[] | null
  used_by: UsedBy[] | null
}

const TASK_STATES: readonly TaskState[] = [
  'SUBMITTED', 'QUEUED', 'PARKED', 'READY', 'LEASED', 'DISPATCHED',
  'STARTING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'DEAD_LETTERED',
]

function taskState(v: unknown): TaskState | null {
  return typeof v === 'string' && (TASK_STATES as readonly string[]).includes(v) ? (v as TaskState) : null
}

export function normIndexRun(v: unknown): IndexRun | null {
  if (!isRec(v)) return null
  const id = str(v.task_id)
  if (id === null) return null
  return {
    task_id: id,
    commit_sha: str(v.commit_sha),
    kind: str(v.kind),
    trigger: str(v.trigger),
    state: taskState(v.state),
    queued_at: str(v.queued_at),
    started_at: str(v.started_at),
    ended_at: str(v.ended_at),
    end_cause: str(v.end_cause),
  }
}

/** A share the API served as a ratio; a value outside 0-1 is not one, so it is unknown. */
function ratio(v: unknown): number | null {
  const n = num(v)
  return n !== null && n >= 0 && n <= 1 ? n : null
}

export function normRepo(v: unknown): RepoRecord | null {
  if (!isRec(v)) return null
  const id = str(v.repo_id)
  const owner = str(v.owner)
  const repo = str(v.repo)
  if (id === null || owner === null || repo === null) return null
  const ix = isRec(v.index) ? v.index : {}
  const graph = isRec(v.graph) ? v.graph : {}
  const pol = isRec(v.selection_policy) ? v.selection_policy : {}
  return {
    repo_id: id,
    owner,
    repo,
    default_branch: str(v.default_branch),
    allowed_profiles: strs(v.allowed_profiles),
    created_by: str(v.created_by),
    created_at: str(v.created_at),
    index: {
      interval_hours: intervalHours(ix.interval_hours),
      on_change: oneOf(ix.on_change, ['poll', 'webhook', 'off'] as const),
      min_change_interval_minutes: num(ix.min_change_interval_minutes),
      full_every_days: num(ix.full_every_days),
      paused: bool(ix.paused),
      current_sha: str(ix.current_sha),
      last_indexed_at: str(ix.last_indexed_at),
      last_kind: str(ix.last_kind),
      head_sha: str(ix.head_sha),
      head_read_at: str(ix.head_read_at),
      behind_by: num(ix.behind_by),
      stale: bool(ix.stale),
      in_flight_task_id: str(ix.in_flight_task_id),
      coverage: normCoverage(ix.coverage),
      next_run_at: str(ix.next_run_at),
    },
    last_run: normIndexRun(v.last_run),
    graph: { depth: num(graph.depth), min_confidence: num(graph.min_confidence) },
    selection_policy: {
      policy: str(pol.policy),
      mode: str(pol.mode),
      inherited_from_tenant: bool(pol.inherited_from_tenant),
    },
  }
}

/** `GET /v1/repositories`: `{repositories: [...]}`. A record missing its id or name is dropped, not guessed. */
export function normRepoList(v: unknown): RepoRecord[] {
  if (!isRec(v)) return []
  return recs(v.repositories).map(normRepo).filter((r): r is RepoRecord => r !== null)
}

function normUsedBy(v: Rec): UsedBy | null {
  const id = str(v.id) ?? str(v.run_id) ?? str(v.workflow_id) ?? str(v.task_id)
  if (id === null) return null
  const k = str(v.kind)
  const kind: UsedBy['kind'] = k === 'workflow' ? 'workflow' : k === 'task' ? 'task' : 'run'
  return { kind, id, title: str(v.title), index_sha: str(v.index_sha), state: str(v.state), role: str(v.role) }
}

/** `GET /v1/repositories/{repo_id}`: the registration, its last index runs, and who used its index. */
export function normRepoDetail(v: unknown): RepoDetail | null {
  if (!isRec(v)) return null
  // The record itself, or the record under `repository` beside its runs.
  const repository = normRepo(isRec(v.repository) ? v.repository : v)
  if (repository === null) return null
  return {
    repository,
    index_runs: Array.isArray(v.index_runs) ? recs(v.index_runs).map(normIndexRun).filter((r): r is IndexRun => r !== null) : null,
    used_by: Array.isArray(v.used_by) ? recs(v.used_by).map(normUsedBy).filter((r): r is UsedBy => r !== null) : null,
  }
}

export function repoName(r: { owner: string; repo: string }): string {
  return `${r.owner}/${r.repo}`
}

/** A commit as the frames print it: seven characters, the whole sha in its title. */
export function shortSha(sha: string): string {
  return sha.slice(0, 7)
}

// ---------------------------------------------------------------------------
// Freshness against the head (repo-index.md §5.1)
// ---------------------------------------------------------------------------

/** Why a head sha is a dash (repo-index.md §5.1, "unknown"). */
export const HEAD_UNREAD = 'The default branch head has never been read: change polling is off or the token lost access'

export type FreshKind = 'current' | 'behind' | 'stale' | 'none' | 'unknown'

export interface Freshness {
  kind: FreshKind
  /** The words on the pill. */
  word: string
  /** The pill's hue (components.html A `c-pill is-…`). */
  hue: 'live' | 'warn' | 'bad' | 'neu'
  /** The brand mark the pill carries. */
  mark: 'succeeded' | 'warn' | 'failed' | 'queued'
  /** Why, when the pill says less than a reader needs. */
  why: string | null
}

/**
 * The four states of §5.1 plus "no index". UNKNOWN IS NEVER "CURRENT": an
 * index whose head was never read is a dash with that reason, because the
 * only thing worse than a stale index is one shown as fresh.
 */
export function freshness(ix: RepoIndexState): Freshness {
  if (ix.current_sha === null) {
    return { kind: 'none', word: 'no index', hue: 'neu', mark: 'queued', why: 'No index has been built yet' }
  }
  if (ix.head_sha === null) {
    return {
      kind: 'unknown',
      word: 'head not read',
      hue: 'neu',
      mark: 'queued',
      why: HEAD_UNREAD,
    }
  }
  if (ix.stale === true) {
    const n = ix.behind_by
    return {
      kind: 'stale',
      word: n === null ? 'stale' : `stale · ${n} behind`,
      hue: 'bad',
      mark: 'failed',
      why: 'Behind by more than 200 commits, older than 7 days, or no longer an ancestor of the head',
    }
  }
  if (ix.current_sha === ix.head_sha) {
    return { kind: 'current', word: 'current', hue: 'live', mark: 'succeeded', why: null }
  }
  const n = ix.behind_by
  return {
    kind: 'behind',
    word: n === null ? 'behind head' : `${n} behind`,
    hue: 'warn',
    mark: 'warn',
    why: n === null ? 'The index describes an older commit; the count between them was not read' : null,
  }
}

// ---------------------------------------------------------------------------
// Schedule
// ---------------------------------------------------------------------------

/** "every 24 h", "every 7 days", or null when the interval is off or not served. */
export function intervalWords(h: number | 'off' | null): string | null {
  if (h === null || h === 'off') return null
  if (h % 24 === 0 && h >= 48) return `${h / 24} days`
  return `${h} h`
}

/**
 * The card's schedule line: "24 h + on change", "168 h", "on change only",
 * "off", "paused". Null when the interval is not served; a served interval
 * with an unserved trigger says the trigger is unknown rather than dropping
 * it, so an unknown never reads as "off".
 */
export function scheduleWords(ix: RepoIndexState): string | null {
  if (ix.paused === true) return 'paused'
  const h = ix.interval_hours
  // An interval the API did not serve is unknown, not "off".
  if (h === null) return null
  const every = h === 'off' ? null : `${h} h`
  const on = ix.on_change
  const trigger = on === null ? 'change trigger unknown' : on === 'off' ? null : on === 'webhook' ? 'on change (webhook)' : 'on change'
  if (every !== null) return trigger === null ? every : `${every} + ${trigger}`
  return trigger === null ? 'off' : on === null ? `no interval · ${trigger}` : `${trigger} only`
}

/** A percentage for a 0-1 ratio. */
export function pct(r: number): string {
  return `${Math.round(r * 100)}%`
}

// ---------------------------------------------------------------------------
// The index document (repo-index.md §2.1), only the keys the screens draw
// ---------------------------------------------------------------------------

export interface IndexModule {
  path: string
  purpose: string | null
  files: number | null
  lines: number | null
  language: string | null
}
export interface HotSpot {
  path: string
  changes: number | null
}
export interface CoChange {
  a: string
  b: string
  times: number | null
}
/** One test-map edge: one source glob, ONE test (repoindex.py `TestEdge`), with how it was found and its command. */
export interface TestEdge {
  source: string
  /** Null for a source the index listed with no test. */
  test: string | null
  evidence: string | null
  command: string | null
}
/** The edges of one source, for the Test map's rows: many edges share a source (227 on swarmcloud). */
export interface TestMapRow {
  source: string
  tests: { test: string; evidence: string | null; command: string | null }[]
}
export interface IndexDoc {
  commit_sha: string | null
  built_at: string | null
  bytes: number | null
  truncated: string[]
  modules: IndexModule[]
  entry_points: string[]
  hot_spots: HotSpot[]
  co_changes: CoChange[]
  test_map: TestEdge[]
  unmapped: string[]
  /** The document's `languages` rows; null when the document carries no such array. */
  languages: LanguageRow[] | null
}

export function normIndexDoc(v: unknown): IndexDoc | null {
  if (!isRec(v)) return null
  // THE LIVE ANSWER IS `{index, document, freshness, produced_by, …}`
  // (routes/repositories.py `get_index`): `index` is the version's METADATA
  // (commit, digest, bytes, truncated, extractor) and `document` the index
  // itself. Reading `index` as the document drew "Modules 0" over an index
  // of 83 (QA G4-01). An envelope whose document is absent has no index yet.
  if ('document' in v && !isRec(v.document)) return null
  const meta: Rec = isRec(v.index) ? v.index : {}
  const d: Rec = isRec(v.document) ? v.document : Array.isArray(meta.modules) ? meta : v
  const modules = recs(d.modules)
    .map((m) => {
      const path = str(m.path)
      return path === null
        ? null
        : { path, purpose: str(m.purpose), files: num(m.files), lines: num(m.lines), language: str(m.language) }
    })
    .filter((m): m is IndexModule => m !== null)
  const entry = recs(d.entry_points).map((e) => str(e.name) ?? str(e.path)).filter((x): x is string => x !== null)
  const hot = recs(d.hot_spots)
    .map((h) => {
      const path = str(h.path)
      return path === null ? null : { path, changes: num(h.changes) ?? num(h.count) }
    })
    .filter((h): h is HotSpot => h !== null)
  const served = d.co_changes ?? (isRec(d.hot_spots_meta) ? d.hot_spots_meta.co_changes : undefined)
  const co = Array.isArray(served)
    ? recs(served)
        .map((c) => {
          const p = strs(c.paths)
          const a = str(c.a) ?? p[0] ?? null
          const b = str(c.b) ?? p[1] ?? null
          return a === null || b === null ? null : { a, b, times: num(c.times) ?? num(c.count) }
        })
        .filter((c): c is CoChange => c !== null)
    : // The document's own shape: each hot-spot names what changed with it (repoindex.py `HotSpot.co_changed`), with no count.
      recs(d.hot_spots).flatMap((h) => {
        const a = str(h.path)
        return a === null ? [] : strs(h.co_changed).map((b) => ({ a, b, times: null }))
      })
  // An edge is `{source, test, evidence, command}`, one test each; a list
  // under `tests` is read too, one edge per test, so no shape loses a test.
  const tm = recs(d.test_map).flatMap((t): TestEdge[] => {
    const source = str(t.source) ?? str(t.path) ?? str(t.glob)
    if (source === null) return []
    const evidence = str(t.evidence)
    const command = str(t.command)
    const one = str(t.test)
    const tests = one !== null ? [one] : strs(t.tests)
    return tests.length === 0 ? [{ source, test: null, evidence, command }] : tests.map((test) => ({ source, test, evidence, command }))
  })
  return {
    commit_sha: str(d.commit_sha) ?? str(meta.commit_sha),
    built_at: str(d.built_at) ?? str(meta.built_at),
    bytes: num(v.bytes) ?? num(meta.bytes) ?? num(d.bytes),
    truncated: Array.isArray(d.truncated) ? strs(d.truncated) : strs(meta.truncated),
    modules,
    entry_points: entry,
    hot_spots: hot,
    co_changes: co,
    test_map: tm,
    unmapped: strs(d.unmapped),
    languages: Array.isArray(d.languages) ? normLanguages(d.languages) : null,
  }
}

/** The edges grouped by source, in the order the index lists them; a test listed twice under one source is drawn once. */
export function testMapRows(edges: readonly TestEdge[]): TestMapRow[] {
  const rows = new Map<string, TestMapRow>()
  for (const e of edges) {
    let row = rows.get(e.source)
    if (row === undefined) {
      row = { source: e.source, tests: [] }
      rows.set(e.source, row)
    }
    if (e.test !== null && !row.tests.some((t) => t.test === e.test)) row.tests.push({ test: e.test, evidence: e.evidence, command: e.command })
  }
  return [...rows.values()]
}

// ---------------------------------------------------------------------------
// Languages (repo-index.md §2.1 `languages`, §6.1)
// ---------------------------------------------------------------------------

export interface LanguageRow {
  language: string
  grammar: string | null
  server: string | null
  status: string | null
  files: number | null
  resolved: number | null
  fallback: string | null
}

export function normLanguages(v: unknown): LanguageRow[] {
  const rows = isRec(v) ? recs(v.languages) : recs(v)
  return rows
    .map((l) => {
      const language = str(l.language) ?? str(l.name)
      return language === null
        ? null
        : {
            language,
            grammar: str(l.grammar),
            server: str(l.server),
            status: str(l.status),
            files: num(l.files),
            resolved: ratio(l.resolved),
            fallback: str(l.fallback),
          }
    })
    .filter((l): l is LanguageRow => l !== null)
}

// ---------------------------------------------------------------------------
// Readable repositories (Register C)
// ---------------------------------------------------------------------------

export type Visibility = 'public' | 'private' | 'internal'

export interface Readable {
  owner: string
  repo: string
  default_branch: string | null
  /**
   * As the API serves it (`forge.py::_visibility`): public, private or
   * internal; null when it answered `unknown` or nothing. An older answer
   * that carried only `private` is read from that (QA G4-16).
   */
  visibility: Visibility | null
  archived: boolean | null
  pushed_at: string | null
  registered: boolean
}

/** A page after the first that did not come back: the list holds the pages before it. */
export interface ReadableGap {
  page: number
  message: string
}

export interface ReadableList {
  /** The secret the list was read with, by NAME. Never a value. */
  secret_name: string | null
  total: number | null
  repositories: Readable[]
  /** The page the API offers next; null once it offers none. */
  next_page: number | null
  /** The API reached its page cap with a full page: the token may read more than is listed. */
  capped: boolean
  /** The API's page cap and page size, when it said them. */
  max_pages: number | null
  per_page: number | null
  /** How many pages this list was read from. */
  pages: number
  /** A later page that failed, so the list is short; null when every page came back. */
  gap: ReadableGap | null
}

function visibilityOf(r: Record<string, unknown>): Visibility | null {
  const v = str(r.visibility)
  if (v === 'public' || v === 'private' || v === 'internal') return v
  const p = bool(r.private)
  return p === null ? null : p ? 'private' : 'public'
}

/** One page of `GET /v1/repositories/readable` (`swarm_api/repositories.py::readable`). */
export function normReadable(v: unknown): ReadableList {
  if (!isRec(v)) {
    return { secret_name: null, total: null, repositories: [], next_page: null, capped: false, max_pages: null, per_page: null, pages: 0, gap: null }
  }
  const repositories = recs(v.repositories)
    .map((r) => {
      let owner = str(r.owner)
      let repo = str(r.repo)
      const full = str(r.full_name) ?? str(r.repository)
      if ((owner === null || repo === null) && full !== null && full.includes('/')) {
        ;[owner, repo] = [full.slice(0, full.indexOf('/')), full.slice(full.indexOf('/') + 1)]
      }
      if (owner === null || repo === null) return null
      return {
        owner,
        repo,
        default_branch: str(r.default_branch),
        visibility: visibilityOf(r),
        archived: bool(r.archived),
        pushed_at: str(r.pushed_at),
        registered: r.registered === true,
      }
    })
    .filter((r): r is Readable => r !== null)
  const next = num(v.next_page)
  return {
    secret_name: str(v.secret_name),
    total: num(v.total),
    repositories,
    next_page: next !== null && Number.isInteger(next) && next > 1 ? next : null,
    capped: v.capped === true,
    max_pages: num(v.max_pages),
    per_page: num(v.per_page),
    pages: 1,
    gap: null,
  }
}

/**
 * The list so far with one more page appended. The later page decides where
 * paging stands (`next_page`, `capped`); a repository already listed is not
 * listed twice, in case the forge's order shifted between two pages.
 */
export function appendReadable(sofar: ReadableList, more: ReadableList): ReadableList {
  const seen = new Set(sofar.repositories.map((r) => `${r.owner}/${r.repo}`.toLowerCase()))
  const added = more.repositories.filter((r) => !seen.has(`${r.owner}/${r.repo}`.toLowerCase()))
  return {
    ...more,
    secret_name: sofar.secret_name ?? more.secret_name,
    total: sofar.total ?? more.total,
    max_pages: more.max_pages ?? sofar.max_pages,
    per_page: more.per_page ?? sofar.per_page,
    repositories: [...sofar.repositories, ...added],
    pages: sofar.pages + more.pages,
    gap: null,
  }
}

// ---------------------------------------------------------------------------
// Git tokens (git-tokens.md §1, §5, §7)
// ---------------------------------------------------------------------------

export type TokenScope = 'tenant' | 'repository' | 'user'

export interface GitToken {
  token_id: string
  tenant_id: string | null
  scope: TokenScope
  repo_ids: string[]
  user: string | null
  provider_suffix: string | null
  secret_name: string | null
  forge: string | null
  kind: string | null
  forge_login: string | null
  last4: string | null
  expires_at: string | null
  verified_at: string | null
  state: string | null
  /** The covered repositories by name, when the API names them. */
  repositories: string[]
  last_error: string | null
  /**
   * The caller's own user token. The API computes it per request from the
   * verified identity (`GET /v1/git-tokens`); only an exact `true` counts.
   */
  yours: boolean
  /** A user token's email: served on the caller's own records, and to an admin. */
  owner: string | null
}

function scopeOf(v: unknown): TokenScope | null {
  return v === 'tenant' || v === 'repository' || v === 'user' ? v : null
}

/** Four characters of [A-Za-z0-9] or nothing: anything longer is not a `last4` and is never drawn. */
function last4(v: unknown): string | null {
  return typeof v === 'string' && /^[A-Za-z0-9]{4}$/.test(v) ? v : null
}

/**
 * One token record, METADATA ONLY. Fields are copied by name; a `value`,
 * `token`, `secret` or anything else a response carries is never read.
 */
export function normToken(v: unknown): GitToken | null {
  if (!isRec(v)) return null
  const id = str(v.token_id)
  const scope = scopeOf(v.scope)
  if (id === null || scope === null) return null
  return {
    token_id: id,
    tenant_id: str(v.tenant_id),
    scope,
    repo_ids: strs(v.repo_ids),
    user: str(v.user),
    provider_suffix: safeName(v.provider_suffix),
    secret_name: safeName(v.secret_name),
    forge: str(v.forge),
    kind: str(v.kind),
    forge_login: str(v.forge_login),
    last4: last4(v.last4),
    expires_at: str(v.expires_at),
    verified_at: str(v.verified_at),
    state: str(v.state),
    // The API names the covered repositories as {repo_id: "owner/repo"}.
    repositories: isRec(v.repositories) ? Object.values(v.repositories).filter((x): x is string => typeof x === 'string') : strs(v.repositories),
    last_error: str(v.last_error) ?? str(v.probe_error),
    yours: v.yours === true,
    owner: str(v.owner),
  }
}

/**
 * A secret NAME or provider suffix as Secret Manager names them
 * (`swarm-tenant-eng-git-r-<16 hex>`): lower-case letters, digits and hyphens.
 * Anything else is not a name this page will print or copy.
 */
function safeName(v: unknown): string | null {
  return typeof v === 'string' && /^[a-z0-9][a-z0-9-]{0,254}$/.test(v) ? v : null
}

export function normTokens(v: unknown): GitToken[] {
  const rows = isRec(v) ? recs(v.git_tokens ?? v.tokens) : recs(v)
  return rows.map(normToken).filter((t): t is GitToken => t !== null)
}

/** The eight capabilities of git-tokens.md §5.1, in the frames' column order. */
export const CAPABILITIES = [
  { key: 'clone', label: 'Clone' },
  { key: 'push', label: 'Push branches' },
  { key: 'open_prs', label: 'Open PRs' },
  { key: 'read_checks', label: 'Read checks' },
  { key: 'merge', label: 'Merge' },
  { key: 'close_issues', label: 'Close issues' },
  { key: 'read_issues', label: 'Read issues' },
  { key: 'workflow_dispatch', label: 'Workflow dispatch' },
] as const

export type CapabilityKey = (typeof CAPABILITIES)[number]['key']
export type CapState = 'ok' | 'missing' | 'unknown'

export interface CapCell {
  state: CapState
  reason: string | null
}

/**
 * A capability as served. ANYTHING NOT EXACTLY `ok` OR `missing` IS UNKNOWN:
 * a cell the probe has not decided is grey, never green (§5.2).
 */
export function capCell(v: unknown): CapCell {
  if (!isRec(v)) return { state: 'unknown', reason: typeof v === 'string' ? null : 'Not served for this pair' }
  const s = v.state
  return { state: s === 'ok' || s === 'missing' ? s : 'unknown', reason: str(v.reason) }
}

export type CapRow = Record<CapabilityKey, CapCell>

export function capRow(v: unknown): CapRow {
  const src = isRec(v) ? v : {}
  const out = {} as CapRow
  for (const c of CAPABILITIES) out[c.key] = capCell(src[c.key])
  return out
}

export interface PermissionRow {
  repo_id: string
  repository: string | null
  token: GitToken
  resolves: boolean
  capabilities: CapRow
  expires_at: string | null
  verified_at: string | null
  last_error: string | null
}

export interface Permissions {
  order: string | null
  rows: PermissionRow[]
}

/** `GET /v1/git-tokens/permissions`: one row per token × repository. */
export function normPermissions(v: unknown): Permissions {
  if (!isRec(v)) return { order: null, rows: [] }
  const rows = recs(v.rows ?? v.permissions)
    .map((r) => {
      const repoId = str(r.repo_id)
      const token = normToken(r.token)
      if (repoId === null || token === null) return null
      return {
        repo_id: repoId,
        repository: str(r.repository),
        token,
        resolves: r.resolves === true,
        capabilities: capRow(r.capabilities),
        expires_at: str(r.expires_at) ?? token.expires_at,
        verified_at: str(r.verified_at) ?? token.verified_at,
        last_error: str(r.last_error),
      }
    })
    .filter((r): r is PermissionRow => r !== null)
  return { order: str(v.order), rows }
}

/** Revoked and expired records are passed over by resolution (git-tokens.md section 3.1). */
function usable(t: GitToken): boolean {
  return t.state !== 'revoked' && t.state !== 'expired'
}

/** R2 without a caller: the repository's own token, else the tenant's. A user token is the caller's and is not derivable here. */
function resolver(tokens: GitToken[], repoId: string): GitToken | null {
  return (
    tokens.find((t) => t.scope === 'repository' && t.repo_ids.includes(repoId) && usable(t)) ??
    tokens.find((t) => t.scope === 'tenant' && usable(t)) ??
    null
  )
}

/**
 * The caller's own user token that attributes work in this repository: the
 * record the API marked `yours`, usable, and not narrowed away from it. Null
 * when there is none -- the attribution line is then not drawn at all.
 */
function ownUserToken(tokens: GitToken[], repoId: string): GitToken | null {
  return (
    tokens.find((t) => t.yours && t.scope === 'user' && usable(t) && (t.repo_ids.length === 0 || t.repo_ids.includes(repoId))) ?? null
  )
}

/** The probe rows `GET /v1/git-tokens` carries for each token: `probe.repositories`. */
function probeRows(raw: Rec): Rec[] {
  return isRec(raw.probe) ? recs(raw.probe.repositories) : []
}

/**
 * The permission matrix, built from the per-repository probe summaries each
 * token record carries (git-tokens.md sections 5, 6). There is no separate
 * matrix route. A token whose probe has not run has no rows, so no cells.
 */
export function normPermissionsFromTokens(v: unknown): Permissions {
  if (!isRec(v)) return { order: null, rows: [] }
  const raws = recs(v.git_tokens ?? v.tokens)
  const tokens = raws.map(normToken).filter((t): t is GitToken => t !== null)
  const rows: PermissionRow[] = []
  for (const raw of raws) {
    const token = normToken(raw)
    if (token === null) continue
    for (const r of probeRows(raw)) {
      const repoId = str(r.repo_id)
      if (repoId === null) continue
      rows.push({
        repo_id: repoId,
        repository: str(r.repository),
        token,
        resolves: resolver(tokens, repoId)?.token_id === token.token_id,
        capabilities: capRow(r.capabilities),
        expires_at: str(r.expires_at) ?? token.expires_at,
        verified_at: str(r.verified_at) ?? token.verified_at,
        last_error: str(r.error) ?? token.last_error,
      })
    }
  }
  return { order: str(v.resolution_order), rows }
}

export interface ResolvedToken {
  order: string | null
  token: GitToken | null
  capabilities: CapRow | null
  /** Why nothing resolved, when nothing did. */
  reason: string | null
  /** The caller's own token (the record marked `yours`), used only for attribution under R2. */
  user_token: GitToken | null
}

/** `GET /v1/repositories/{repo_id}/token?user=me`. */
export function normResolved(v: unknown): ResolvedToken {
  if (!isRec(v)) return { order: null, token: null, capabilities: null, reason: null, user_token: null }
  return {
    order: str(v.order),
    token: normToken(v.token),
    capabilities: isRec(v.capabilities) ? capRow(v.capabilities) : null,
    reason: str(v.reason),
    user_token: normToken(v.user_token),
  }
}

/** "fine-grained PAT", "classic PAT", "App installation", or the served word. */
export function kindWords(kind: string | null): string | null {
  if (kind === null) return null
  return (
    { fine_grained_pat: 'fine-grained PAT', classic_pat: 'classic PAT', app_installation: 'App installation' } as Record<string, string>
  )[kind] ?? kind.replace(/_/g, ' ')
}

export const SCOPE_WORD: Readonly<Record<TokenScope, string>> = { tenant: 'Tenant', repository: 'Repository', user: 'User' }

/**
 * The one way a token value enters SwarmCloud (CLAUDE.md, owner rule
 * 2026-09-25; PICKS.md S1): from the operator's terminal, on stdin. The
 * command names the slot; it carries no value, and the page has no field
 * that could take one.
 */
export function createSecretsCommand(tenant: string, providerSuffix: string): string {
  return `scripts/create-secrets.sh --tenant ${tenant} --provider ${providerSuffix} --stdin`
}

/** Days until an expiry, rounded down; negative once it has passed. */
export function daysUntil(iso: string, now: number): number | null {
  const t = new Date(iso).getTime()
  if (!Number.isFinite(t)) return null
  return Math.floor((t - now) / 86_400_000)
}

/** "in 9 days", "today", "expired 2 days ago". */
export function expiryWords(iso: string, now: number): string | null {
  const d = daysUntil(iso, now)
  if (d === null) return null
  if (d < 0) return `expired ${-d} day${d === -1 ? '' : 's'} ago`
  if (d === 0) return 'today'
  return `in ${d} day${d === 1 ? '' : 's'}`
}

/**
 * The token that resolves for one repository, derived from
 * `GET /v1/git-tokens` and the repository id (R2: the repository's own token,
 * else the tenant's), and the caller's own user token beside it, for
 * attribution. Metadata only; there is no per-repository token route.
 */
export function normResolvedFromTokens(v: unknown, repoId: string): ResolvedToken {
  const order = isRec(v) ? str(v.resolution_order) : null
  const raws = isRec(v) ? recs(v.git_tokens ?? v.tokens) : []
  const tokens = raws.map(normToken).filter((t): t is GitToken => t !== null)
  const token = resolver(tokens, repoId)
  const user_token = ownUserToken(tokens, repoId)
  if (token === null) {
    return { order, token: null, capabilities: null, reason: 'No repository or tenant token is registered for this repository', user_token }
  }
  const raw = raws.find((r) => r.token_id === token.token_id)
  const row = raw === undefined ? undefined : probeRows(raw).find((r) => r.repo_id === repoId)
  return { order, token, capabilities: row === undefined ? null : capRow(row.capabilities), reason: null, user_token }
}
