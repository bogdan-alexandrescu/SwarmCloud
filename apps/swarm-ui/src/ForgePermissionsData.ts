/**
 * `GET /v1/forge/permissions` (#1086, PERM-1; swarm_api/forgeperms.py), read
 * into the shapes Work › Setup › GitHub, the repository list's dots and a
 * repository's page draw (PERM-2).
 *
 * NORMALISE OR DROP, NEVER GUESS (RepositoriesData.ts's rule). A card with no
 * owner, a gap with no permission or no `breaks`, a repository row with no
 * id: dropped. A status that is not one of the three words is `unknown`,
 * never `ok` -- the server's own rule is that unknown is never green, and a
 * client that read an odd word as green would break it. A `fix_url` that is
 * not a github.com page is dropped, so the gap draws no button rather than a
 * link to somewhere else.
 *
 * Pure: no React, no fetch, so it is tested by what it returns.
 */

export type PermStatus = 'ok' | 'missing' | 'unknown'

/** One catalogue entry an org or the App does not hold. */
export interface PermGap {
  permission: string
  level: string
  /** What is held instead, `null` for nothing. */
  held: string | null
  needed_for: string | null
  /** The catalogue's plain-words consequence: the sentence a person reads. */
  breaks: string
  /** `accept`: the org accepts the App's request. `app`: the App's owner changes what it asks for. */
  fix: 'accept' | 'app' | null
  /** The exact GitHub page that fixes it, or `null` (not sent, or not this caller's). */
  fix_url: string | null
}

export interface AppPermCard {
  slug: string | null
  owner: string | null
  status: PermStatus
  reason: string | null
  gaps: PermGap[]
  /** The App's permissions settings page: sent to platform admins only. */
  permissions_url: string | null
}

export interface OrgPermCard {
  owner: string
  installation_id: number | null
  repository_selection: string | null
  status: PermStatus
  reason: string | null
  gaps: PermGap[]
  /** Registered repositories this card covers (repo ids). */
  repositories: string[]
  /** The installation's settings page, sent with a suspended installation. */
  installation_url: string | null
}

export interface RepoProblem {
  permission: string
  level: string | null
  breaks: string
}

export interface RepoPermStatus {
  repo_id: string
  repository: string
  owner: string
  /** `app`: through the App's installation on its org. `token`: through a stored token. */
  source: 'app' | 'token' | null
  status: PermStatus
  reason: string | null
  problems: RepoProblem[]
  /** Permissions the token probe could not measure (a token-backed row only). */
  unread: string[]
}

export interface ForgePerms {
  tenant_id: string | null
  checked_at: string | null
  /** The server answered from its short cache rather than reading GitHub again. */
  cached: boolean
  app: AppPermCard | null
  orgs: OrgPermCard[]
  repositories: RepoPermStatus[]
}

type Rec = Record<string, unknown>

function isRec(v: unknown): v is Rec {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v !== '' ? v : null
}

function recs(v: unknown): Rec[] {
  return Array.isArray(v) ? v.filter(isRec) : []
}

function strs(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string' && x !== '') : []
}

function status(v: unknown): PermStatus {
  return v === 'ok' || v === 'missing' ? v : 'unknown'
}

/** A GitHub settings page, or nothing: a fix button opens github.com and nowhere else. */
export function githubUrl(v: unknown): string | null {
  const s = str(v)
  return s !== null && s.startsWith('https://github.com/') ? s : null
}

function normGap(g: Rec): PermGap | null {
  const permission = str(g.permission)
  const breaks = str(g.breaks)
  const level = str(g.level)
  if (permission === null || breaks === null || level === null) return null
  return {
    permission,
    level,
    held: str(g.held),
    needed_for: str(g.needed_for),
    breaks,
    fix: g.fix === 'accept' || g.fix === 'app' ? g.fix : null,
    fix_url: githubUrl(g.fix_url),
  }
}

function gapsOf(v: unknown): PermGap[] {
  return recs(v)
    .map(normGap)
    .filter((g): g is PermGap => g !== null)
}

/**
 * A card's status as drawn. `ok` with a gap in hand is not ok: the gaps are
 * what a person acts on, so they win over a word that contradicts them.
 */
function cardStatus(raw: unknown, gaps: PermGap[]): PermStatus {
  const s = status(raw)
  return s === 'ok' && gaps.length > 0 ? 'missing' : s
}

function normApp(v: unknown): AppPermCard | null {
  if (!isRec(v)) return null
  const gaps = gapsOf(v.gaps)
  return {
    slug: str(v.slug),
    owner: str(v.owner),
    status: cardStatus(v.status, gaps),
    reason: str(v.reason),
    gaps,
    permissions_url: githubUrl(v.permissions_url),
  }
}

function normOrg(v: Rec): OrgPermCard | null {
  const owner = str(v.owner)
  if (owner === null) return null
  const gaps = gapsOf(v.gaps)
  const inst = v.installation_id
  return {
    owner,
    installation_id: typeof inst === 'number' && Number.isInteger(inst) ? inst : null,
    repository_selection: str(v.repository_selection),
    status: cardStatus(v.status, gaps),
    reason: str(v.reason),
    gaps,
    repositories: strs(v.repositories),
    installation_url: githubUrl(v.installation_url),
  }
}

function normProblem(p: Rec): RepoProblem | null {
  const permission = str(p.permission)
  const breaks = str(p.breaks)
  if (permission === null || breaks === null) return null
  return { permission, level: str(p.level), breaks }
}

function normRepoStatus(v: Rec): RepoPermStatus | null {
  const repoId = str(v.repo_id)
  if (repoId === null) return null
  const problems = recs(v.problems)
    .map(normProblem)
    .filter((p): p is RepoProblem => p !== null)
  const s = status(v.status)
  return {
    repo_id: repoId,
    repository: str(v.repository) ?? repoId,
    owner: str(v.owner) ?? '',
    source: v.source === 'app' || v.source === 'token' ? v.source : null,
    status: s === 'ok' && problems.length > 0 ? 'missing' : s,
    reason: str(v.reason),
    problems,
    unread: strs(v.unread),
  }
}

/** The whole answer, or `null` when it is not one this console can read. */
export function normForgePermissions(raw: unknown): ForgePerms | null {
  if (!isRec(raw) || !Array.isArray(raw.orgs) || !Array.isArray(raw.repositories)) return null
  return {
    tenant_id: str(raw.tenant_id),
    checked_at: str(raw.checked_at),
    cached: raw.cached === true,
    app: normApp(raw.app),
    orgs: recs(raw.orgs)
      .map(normOrg)
      .filter((c): c is OrgPermCard => c !== null),
    repositories: recs(raw.repositories)
      .map(normRepoStatus)
      .filter((r): r is RepoPermStatus => r !== null),
  }
}

// ---------------------------------------------------------------------------
// Words
// ---------------------------------------------------------------------------

/** GitHub's permission names as its settings pages print them. */
const PERMISSION_LABEL: Readonly<Record<string, string>> = {
  metadata: 'Metadata',
  contents: 'Contents',
  pull_requests: 'Pull requests',
  checks: 'Checks',
  statuses: 'Commit statuses',
  issues: 'Issues',
  installation: 'App installation',
  repository_access: 'Repository access',
}

export function permissionLabel(permission: string): string {
  return PERMISSION_LABEL[permission] ?? permission.replace(/_/g, ' ')
}

/** "Commit statuses: read", the way GitHub's permission pages name a grant. */
export function grantLabel(permission: string, level: string | null): string {
  const name = permissionLabel(permission)
  return level === null ? name : `${name}: ${level}`
}

/**
 * The server's fixed `reason` words (forgeperms.py, forgeapp.AppUnavailable),
 * as a sentence. An unlisted word is printed as itself: a reason nobody wrote
 * words for is still the reason, never nothing.
 */
const REASON: Readonly<Record<string, string>> = {
  app_id_not_configured: "The App's id is not configured on this platform, so nothing could be read.",
  app_slug_not_configured: "The App's name is not configured on this platform, so nothing could be read.",
  private_key_missing: "The App's private key is not stored, so SwarmCloud could not read GitHub as the App.",
  private_key_malformed: "The App's private key could not be read, so SwarmCloud could not read GitHub as the App.",
  key_rejected: "GitHub refused the App's key, so nothing could be read.",
  github_refused: 'GitHub refused the read, so this is not known.',
  github_unreachable: 'GitHub did not answer, so this is not known.',
  out_of_time: 'GitHub was too slow to answer in time, so this is not known.',
  app_not_installed: 'The App is not installed on this org, so runs cannot reach its repositories.',
  installations_capped: 'The App is installed on more accounts than one read lists, so this org was not found.',
  installation_suspended: "The App's installation on this org is suspended, so runs cannot reach its repositories.",
  // The one word `forgeperms._TokenView` sends when R2 resolves nothing here.
  ['no' + '_token']: 'Nothing resolves a stored credential for this repository.',
  not_probed: "This repository's token has not been probed yet.",
  not_measured: 'The token probe could not measure every permission this repository needs.',
}

export function reasonWords(reason: string | null): string | null {
  if (reason === null) return null
  return REASON[reason] ?? reason.replace(/_/g, ' ')
}

/** A repository's status in one line: the dot's tooltip and its accessible name. */
export function repoStatusLine(row: RepoPermStatus | null, unread: string | null = null): string {
  if (row === null) return `GitHub permissions: not checked${unread !== null ? ` (${unread})` : ''}`
  if (row.status === 'ok') return 'GitHub permissions: everything this repository needs is granted'
  if (row.status === 'missing') {
    const problems = row.problems.map((p) => `${grantLabel(p.permission, p.level)} missing: ${p.breaks}`)
    return `GitHub permissions: ${problems.length > 0 ? problems.join('; ') : (reasonWords(row.reason) ?? 'something is missing')}`
  }
  return `GitHub permissions: not known. ${reasonWords(row.reason) ?? 'The check did not say why.'}`
}
