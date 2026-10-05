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

export interface RepoIndexState {
  interval_hours: number | null
  on_change: boolean | null
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
  /** Source files with at least one test edge ÷ source files, 0-1. Edges, not executed coverage. */
  coverage: number | null
  next_run_at: string | null
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
  index_runs: IndexRun[]
  used_by: UsedBy[]
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

/** A coverage the API served as a ratio; a value outside 0-1 is not one, so it is unknown. */
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
      interval_hours: num(ix.interval_hours),
      on_change: bool(ix.on_change),
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
      coverage: ratio(ix.coverage),
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
    index_runs: recs(v.index_runs).map(normIndexRun).filter((r): r is IndexRun => r !== null),
    used_by: recs(v.used_by).map(normUsedBy).filter((r): r is UsedBy => r !== null),
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
export function intervalWords(h: number | null): string | null {
  if (h === null || h <= 0) return null
  if (h % 24 === 0 && h >= 48) return `${h / 24} days`
  return `${h} h`
}

/** The card's schedule line: "24 h + on change", "168 h", "on change only", "off", "paused". */
export function scheduleWords(ix: RepoIndexState): string | null {
  if (ix.paused === true) return 'paused'
  const h = ix.interval_hours
  // An interval the API did not serve is unknown, not "off".
  if (h === null) return null
  const every = h > 0 ? `${h} h` : null
  if (every !== null) return ix.on_change === true ? `${every} + on change` : every
  return ix.on_change === true ? 'on change only' : 'off'
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
export interface TestEdge {
  source: string
  tests: string[]
  evidence: string | null
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
}

export function normIndexDoc(v: unknown): IndexDoc | null {
  if (!isRec(v)) return null
  // `?format=json` may answer the document itself or `{index: …, bytes}`.
  const d = isRec(v.index) ? v.index : v
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
  const co = recs(d.co_changes ?? (isRec(d.hot_spots_meta) ? d.hot_spots_meta.co_changes : undefined))
    .map((c) => {
      const p = strs(c.paths)
      const a = str(c.a) ?? p[0] ?? null
      const b = str(c.b) ?? p[1] ?? null
      return a === null || b === null ? null : { a, b, times: num(c.times) ?? num(c.count) }
    })
    .filter((c): c is CoChange => c !== null)
  const tm = recs(d.test_map)
    .map((t) => {
      const source = str(t.source) ?? str(t.path) ?? str(t.glob)
      return source === null ? null : { source, tests: strs(t.tests), evidence: str(t.evidence) }
    })
    .filter((t): t is TestEdge => t !== null)
  return {
    commit_sha: str(d.commit_sha),
    built_at: str(d.built_at),
    bytes: num(v.bytes) ?? num(d.bytes),
    truncated: strs(d.truncated),
    modules,
    entry_points: entry,
    hot_spots: hot,
    co_changes: co,
    test_map: tm,
    unmapped: strs(d.unmapped),
  }
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

export interface Readable {
  owner: string
  repo: string
  default_branch: string | null
  private: boolean | null
  pushed_at: string | null
  registered: boolean
}

export interface ReadableList {
  /** The secret the list was read with, by NAME. Never a value. */
  secret_name: string | null
  total: number | null
  repositories: Readable[]
}

export function normReadable(v: unknown): ReadableList {
  if (!isRec(v)) return { secret_name: null, total: null, repositories: [] }
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
        private: bool(r.private),
        pushed_at: str(r.pushed_at),
        registered: r.registered === true,
      }
    })
    .filter((r): r is Readable => r !== null)
  return { secret_name: str(v.secret_name), total: num(v.total), repositories }
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
    repositories: strs(v.repositories),
    last_error: str(v.last_error),
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
  { key: 'workflow_dispatch', label: 'workflow_dispatch' },
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

export interface ResolvedToken {
  order: string | null
  token: GitToken | null
  capabilities: CapRow | null
  /** Why nothing resolved, when nothing did. */
  reason: string | null
  /** The caller's own token, used only for attribution under R2. */
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
