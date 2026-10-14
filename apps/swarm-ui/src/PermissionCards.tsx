import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { loadForgePermissions, loadMe, type ForgeAppCard, type ForgeGap, type ForgeOrgCard, type ForgePermissionsDoc, type ForgeRepoStatus } from './api'
import { Button, ButtonLink, Card, Chip, EmptyState, ToneMark } from './components'
import { notServed } from './RepositoriesData'
import { UrRegion, useUrRead, type UrRead } from './RepositoriesParts'
import { timeAgo } from './types'
import './styles/repositories.css'
import './styles/forgeperms.css'

/**
 * GITHUB PERMISSIONS HEALTH, DESIGN 2 "ORG CARDS" (#1086, PERM-2; owner pick
 * 2026-10-11). On 2026-10-11 sagaxyz/ai-studio's merges failed with
 * `token_lacks_rights` because the App lacked Commit statuses: read, and
 * nothing in the console said so. These cards say it before a run fails.
 *
 *   * one card for the App and one per org whose installation covers a
 *     registered repository (`GET /v1/forge/permissions`, forgeperms.py);
 *   * a card is COLLAPSED when everything is granted and OPEN, with a count
 *     chip, when anything is missing, pending or could not be read -- an
 *     unread card is not a healthy one;
 *   * each gap is a line in plain words, the server's `needed_for` ("merges
 *     can't see CI, so they fail"), with GitHub's name for it only beside it;
 *   * the fix is ALWAYS A LINK. GitHub lets nobody but an org owner accept a
 *     permission, and only the App's owner change what it asks for, so
 *     SwarmCloud never grants anything itself. "Review on GitHub" opens the
 *     installation's permission-update page; "Add to App" opens the App's
 *     permission settings and is drawn for a platform admin only -- the
 *     server already withholds the URL from everyone else, and this draws the
 *     button only when BOTH say admin, so neither alone can show it.
 *
 * WHAT A GREEN CARD SAYS (FP-DETAIL, owner report 2026-10-12: a card reading
 * only "all granted" told nobody anything). Collapsed, its header carries the
 * count of what SwarmCloud needs and the capabilities that buys ("6 of 6
 * needed · Push, Pull requests, ..."); opened, every org card -- green or not
 * -- shows the table of each permission SwarmCloud needs against what GitHub
 * granted and what it is for, the grants SwarmCloud does not use folded under
 * "Also granted" (an over-grant is a choice the owner should see), which
 * registered repositories the installation covers, the installation itself
 * with "Manage on GitHub", and when it was read. All of it is the one read
 * the card already makes; nothing new is fetched.
 *
 * RE-CHECK. "↻ Re-check" reads with `refresh=true`, past the server's 60 s
 * cache. A fix button arms a re-check that runs when the person comes back to
 * this tab (window focus, or the page becoming visible again): that is the
 * moment they have just accepted something on GitHub.
 *
 * Invariant 1: a read holds no capacity and creates no demand; nothing here
 * writes.
 */

/** A capability's name as a person reads it, in forgeperms.py's `CAPABILITIES` order. */
export const CAPABILITY_WORD: Readonly<Record<string, string>> = {
  push: 'Push',
  pull_request: 'Pull requests',
  merge: 'Merge',
  ci: 'CI',
  issues: 'Issues',
  actions: 'Actions',
}

const CAPABILITY_ORDER = Object.keys(CAPABILITY_WORD)

/**
 * GitHub's permission names in plain words, for the ones SwarmCloud needs
 * (forgeperms.py's `REQUIRED`) and the ones an installation commonly carries
 * besides. Anything else is GitHub's own name with its underscores opened.
 */
const PERMISSION_WORD: Readonly<Record<string, string>> = {
  contents: 'Repository contents',
  pull_requests: 'Pull requests',
  statuses: 'Commit statuses',
  checks: 'Checks',
  issues: 'Issues',
  actions: 'Actions',
  metadata: 'Metadata',
  workflows: 'Workflows',
  administration: 'Administration',
}

function permissionWord(name: string): string {
  const plain = name.replace(/_/g, ' ')
  return PERMISSION_WORD[name] ?? plain.charAt(0).toUpperCase() + plain.slice(1)
}

/** GitHub's access levels, least first: forgeperms.py's `_RANK`, so the table and the server agree on "enough". */
const LEVEL_RANK: Readonly<Record<string, number>> = { read: 1, write: 2, admin: 3 }

function levelRank(level: string | null | undefined): number {
  return level == null ? 0 : (LEVEL_RANK[level] ?? 0)
}

/** The one read every surface shares, with Re-check and the focus re-check after a fix. */
export interface ForgePermsRead extends UrRead<ForgePermissionsDoc> {
  /** Read GitHub again now (`refresh=true`). */
  recheck: () => void
  /** A re-check is in flight. */
  checking: boolean
  /** A fix button was used: re-check when the person comes back to this tab. */
  armRecheck: () => void
}

export function useForgePermissions(): ForgePermsRead {
  const fresh = useRef(false)
  const armed = useRef(false)
  const [checking, setChecking] = useState(false)
  const read = useUrRead(() => {
    const refresh = fresh.current
    fresh.current = false
    return loadForgePermissions(refresh).finally(() => setChecking(false))
  }, 'forge-permissions')
  const { reload } = read
  const recheck = useCallback(() => {
    fresh.current = true
    setChecking(true)
    reload()
  }, [reload])
  const armRecheck = useCallback(() => {
    armed.current = true
  }, [])
  useEffect(() => {
    const back = () => {
      if (!armed.current || document.visibilityState === 'hidden') return
      armed.current = false
      recheck()
    }
    window.addEventListener('focus', back)
    document.addEventListener('visibilitychange', back)
    return () => {
      window.removeEventListener('focus', back)
      document.removeEventListener('visibilitychange', back)
    }
  }, [recheck])
  return { ...read, recheck, checking, armRecheck }
}

/**
 * WHO IS ASKING, from the session read the frame already makes. `null` is
 * "not known yet, or the read failed", which draws no admin button: the
 * server is the real gate and withholds the URL besides.
 */
export function useIsAdmin(): boolean | null {
  const [admin, setAdmin] = useState<boolean | null>(null)
  useEffect(() => {
    let live = true
    Promise.resolve(loadMe({ frame: true })).then(
      (r) => {
        if (live && r !== undefined && (r.status === 'ok' || r.status === 'stale')) setAdmin(r.data.principal?.is_admin === true)
      },
      () => {},
    )
    return () => {
      live = false
    }
  }, [])
  return admin
}

// ---------------------------------------------------------------------------
// the cards
// ---------------------------------------------------------------------------

/** One line to show on a card: the plain-words consequence, and GitHub's name for it. */
interface Line {
  key: string
  words: string
  jargon: string | null
  note?: string
}

function gapJargon(g: { permission: string | null; access: string | null; granted: string | null }): string | null {
  if (g.permission === null) return null
  return `${g.permission}: needs ${g.access ?? '?'}, has ${g.granted ?? 'none'}`
}

/**
 * An org's lines: each missing requirement, then each pending request the
 * missing list does not already name. A missing permission with no pending
 * request is one the App itself does not ask for -- accepting on the org's
 * side cannot fix it, and the line says so.
 */
function orgLines(org: ForgeOrgCard, required: readonly ForgeGap[]): Line[] {
  const pending = org.pending ?? []
  const pendingNames = new Set(pending.map((p) => p.permission))
  const lines: Line[] = (org.missing ?? []).map((g, n) => ({
    key: `m:${g.permission ?? n}`,
    words: g.needed_for,
    jargon: gapJargon(g),
    note: org.pending !== null && g.permission !== null && !pendingNames.has(g.permission) ? 'The App itself does not ask for this yet: an admin adds it to the App first.' : undefined,
  }))
  const named = new Set((org.missing ?? []).map((g) => g.permission))
  for (const p of pending) {
    if (named.has(p.permission)) continue
    const req = required.find((r) => r.permission === p.permission)
    lines.push({
      key: `p:${p.permission}`,
      words: req?.needed_for ?? 'The App was updated to ask for more access, and this org has not accepted it yet',
      jargon: `${p.permission}: requested ${p.requested}, accepted ${p.granted ?? 'none'}`,
    })
  }
  return lines
}

/** The count chip's words: "all granted", or what is open. */
function countWords(missing: number, pending: number): string {
  if (missing === 0 && pending === 0) return 'all granted'
  const parts: string[] = []
  if (missing > 0) parts.push(`${missing} missing`)
  if (pending > 0) parts.push(`${pending} pending`)
  return parts.join(' · ')
}

/**
 * ONE CARD, collapsed or open. It opens by itself when it has anything to
 * say; the person can fold or unfold it, and a new answer (a re-check that
 * turned it green, or a new gap) resets that to the answer's own state.
 */
function PermCard({
  kind,
  name,
  tone,
  chip,
  lines,
  error,
  hint,
  fix,
  sig,
  summary,
  detail,
}: {
  kind: 'app' | 'org' | 'account' | 'token'
  name: string
  tone: 'ok' | 'bad' | 'warn' | 'unknown'
  chip: string
  lines: Line[]
  error: string | null
  hint?: ReactNode
  fix?: ReactNode
  /** What the answer says, so a changed answer resets a person's fold. */
  sig: string
  /** The header's one line, read while the card is folded ("6 of 6 needed · Push, ..."). */
  summary?: string
  /** The open card's detail: the grants table, what it covers, where it came from. */
  detail?: ReactNode
}) {
  const healthy = tone === 'ok'
  const [open, setOpen] = useState(!healthy)
  const [heldSig, setHeldSig] = useState(sig)
  if (heldSig !== sig) {
    setHeldSig(sig)
    setOpen(!healthy)
  }
  const bodyId = useId()
  const kindWord = kind === 'app' ? 'GitHub App' : kind === 'token' ? 'tenant token' : kind
  return (
    <Card
      level={3}
      className={`fp-card is-${tone}${open ? ' is-open' : ''}`}
      title={
        <span className="fp-title">
          <ToneMark tone={tone} hidden />
          <b>{name}</b>
          <small>{kindWord}</small>
          {summary !== undefined && <span className="fp-summary">{summary}</span>}
        </span>
      }
      action={
        <span className="ur-acts fp-acts">
          <Chip tone={tone === 'bad' ? 'bad' : undefined}>{chip}</Chip>
          <Button kind="ghost" size="sm" aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen((o) => !o)}>
            {open ? 'Hide' : 'Details'}
          </Button>
        </span>
      }
    >
      {open && (
        <div className="fp-body" id={bodyId} data-open="true">
          {error !== null && (
            <p className="ur-small fp-error" role="status">
              <b className="ur-bad">Could not read</b> {error}
            </p>
          )}
          {lines.length > 0 && (
            <ul className="fp-lines">
              {lines.map((l) => (
                <li key={l.key} className="fp-line">
                  <span className="fp-words">{l.words}</span>
                  {l.jargon !== null && <code className="fp-jargon">{l.jargon}</code>}
                  {l.note !== undefined && <span className="ur-hint fp-note">{l.note}</span>}
                </li>
              ))}
            </ul>
          )}
          {lines.length === 0 && error === null && detail === undefined && <p className="ur-hint fp-ok">GitHub grants everything SwarmCloud needs here.</p>}
          {detail}
          {hint !== undefined && <p className="ur-hint">{hint}</p>}
          {fix !== undefined && <div className="fp-fix">{fix}</div>}
        </div>
      )}
    </Card>
  )
}

/** A fix button: the GitHub page, in a new tab, arming the re-check for when the person comes back. */
function FixLink({ href, label, onFix }: { href: string; label: string; onFix: () => void }) {
  return (
    <ButtonLink kind="primary" size="sm" href={href} target="_blank" rel="noreferrer" onClick={onFix}>
      {label}
    </ButtonLink>
  )
}

/** One permission SwarmCloud needs, against what an installation granted. */
interface GrantRow {
  permission: string
  needs: string | null
  has: string | null
  ok: boolean
  neededFor: string
  capabilities: string[]
}

/**
 * The table's rows, in `required`'s order, or null when the installation's
 * grants were not read. Met-or-not is the server's `missing` where it was
 * read (so the table, the chip and the dot agree) and the level rank otherwise.
 */
function grantRows(org: ForgeOrgCard, required: readonly ForgeGap[]): GrantRow[] | null {
  const granted = org.granted
  if (granted === null) return null
  const short = org.missing === null ? null : new Set(org.missing.map((g) => g.permission))
  return required.flatMap((r) => {
    if (r.permission === null) return []
    const has = granted[r.permission] ?? null
    const ok = short !== null ? !short.has(r.permission) : levelRank(has) >= Math.max(1, levelRank(r.access))
    return [{ permission: r.permission, needs: r.access, has, ok, neededFor: r.needed_for, capabilities: r.capabilities }]
  })
}

/** The capabilities, in `CAPABILITY_WORD` order, that the rows grant and that they block. */
function capabilitySplit(rows: readonly GrantRow[]): { enabled: string[]; blocked: string[] } {
  const named = new Set(rows.flatMap((r) => r.capabilities))
  const blockedSet = new Set(rows.filter((r) => !r.ok).flatMap((r) => r.capabilities))
  const order = [...CAPABILITY_ORDER, ...[...named].filter((c) => !CAPABILITY_ORDER.includes(c))]
  const words = (keep: (c: string) => boolean) => order.filter((c) => named.has(c) && keep(c)).map((c) => CAPABILITY_WORD[c] ?? c)
  return { enabled: words((c) => !blockedSet.has(c)), blocked: words((c) => blockedSet.has(c)) }
}

/** The header's line: "6 of 6 needed · Push, Pull requests, ...", and what is blocked when anything is. */
function grantSummary(rows: readonly GrantRow[]): string {
  const met = rows.filter((r) => r.ok).length
  const { enabled, blocked } = capabilitySplit(rows)
  const parts = [`${met} of ${rows.length} needed`, enabled.length > 0 ? enabled.join(', ') : 'nothing enabled']
  if (blocked.length > 0) parts.push(`blocked: ${blocked.join(', ')}`)
  return parts.join(' · ')
}

/**
 * The installation's own settings page, where its owner sees and changes
 * everything it grants: `review_url` is that page's `/permissions/update`
 * child (forgeperms.py's `review_url()`), so the parent is the page itself.
 */
function installationUrl(org: ForgeOrgCard): string | null {
  if (org.review_url === null) return null
  const suffix = '/permissions/update'
  return org.review_url.endsWith(suffix) ? org.review_url.slice(0, -suffix.length) : org.review_url
}

function checkedWords(checkedAt: string | null): string {
  return `Checked ${checkedAt === null ? 'at an unknown time' : timeAgo(checkedAt)}`
}

/** The grants table, then the extras, then what the installation covers and where it came from. */
function OrgDetail({
  org,
  rows,
  repositories,
  checkedAt,
  onFix,
}: {
  org: ForgeOrgCard
  rows: GrantRow[] | null
  repositories: readonly ForgeRepoStatus[] | undefined
  checkedAt: string | null | undefined
  onFix: () => void
}) {
  const needed = new Set((rows ?? []).map((r) => r.permission))
  const extra = Object.entries(org.granted ?? {})
    .filter(([k]) => !needed.has(k))
    .sort(([a], [b]) => a.localeCompare(b))
  const covered = repositories?.filter((r) => r.installation_id === org.installation_id).map((r) => r.repo)
  const manage = installationUrl(org)
  return (
    <div className="fp-detail">
      {rows !== null && rows.length > 0 && (
        <div className="fp-grants-wrap">
          <table className="fp-grants">
            <thead>
              <tr>
                <th scope="col">GitHub permission</th>
                <th scope="col">Needs</th>
                <th scope="col">Granted</th>
                <th scope="col" aria-label="Enough" />
                <th scope="col">For</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.permission} data-permission={r.permission} data-ok={r.ok ? 'true' : 'false'} className={r.ok ? undefined : 'is-short'}>
                  <td>
                    <span className="fp-perm">{permissionWord(r.permission)}</span> <code className="fp-jargon">{r.permission}</code>
                  </td>
                  <td>{r.needs ?? '?'}</td>
                  <td>{r.has ?? 'none'}</td>
                  <td className="fp-mark">
                    <ToneMark tone={r.ok ? 'ok' : 'bad'} hidden />
                    <span className="fp-sr">{r.ok ? 'enough' : 'not enough'}</span>
                  </td>
                  <td className="fp-for">
                    <span className="fp-words">{r.neededFor}</span>
                    {r.capabilities.length > 0 && (
                      <small className="fp-caps">{r.capabilities.map((c) => CAPABILITY_WORD[c] ?? c).join(', ')}</small>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {extra.length > 0 && (
        <details className="fp-extra">
          <summary>Also granted (not used by SwarmCloud): {extra.length}</summary>
          <ul className="fp-extra-list">
            {extra.map(([k, level]) => (
              <li key={k}>
                {permissionWord(k)} <code className="fp-jargon">{`${k}: ${level}`}</code>
              </li>
            ))}
          </ul>
        </details>
      )}
      {covered !== undefined && (
        <p className="ur-small fp-covers">
          {covered.length === 0
            ? 'Covers: no registered repository yet'
            : `Covers: ${covered.length} registered ${covered.length === 1 ? 'repository' : 'repositories'} (${covered.join(', ')})`}
        </p>
      )}
      <p className="ur-small fp-via">
        <span>Via: SwarmCloud GitHub App installation {org.installation_id}</span>
        {manage !== null && (
          <ButtonLink kind="ghost" size="sm" href={manage} target="_blank" rel="noreferrer" onClick={onFix}>
            Manage on GitHub
          </ButtonLink>
        )}
      </p>
      {checkedAt !== undefined && <p className="ur-hint fp-when">{checkedWords(checkedAt)}</p>}
    </div>
  )
}

export function OrgCard({
  org,
  required,
  onFix,
  repositories,
  checkedAt,
}: {
  org: ForgeOrgCard
  required: readonly ForgeGap[]
  onFix: () => void
  /** The read's repositories, for "Covers": the ones this installation reaches. */
  repositories?: readonly ForgeRepoStatus[]
  /** The read's `checked_at`, for "Checked 3m ago". */
  checkedAt?: string | null
}) {
  const lines = orgLines(org, required)
  const missing = org.missing?.length ?? 0
  const pending = lines.length - missing
  const unread = org.error !== null || org.missing === null
  const tone = lines.length > 0 ? 'bad' : unread ? 'unknown' : 'ok'
  const rows = unread ? null : grantRows(org, required)
  const chip =
    unread && lines.length === 0
      ? 'not read'
      : tone === 'ok' && rows !== null && rows.length > 0
        ? `${rows.filter((r) => r.ok).length}/${rows.length} granted`
        : countWords(missing, pending)
  return (
    <PermCard
      kind={org.account_type === 'User' ? 'account' : 'org'}
      name={org.org}
      tone={tone}
      chip={chip}
      lines={lines}
      error={org.error ?? (org.missing === null ? 'The installation was not read.' : null)}
      sig={`${tone}:${lines.map((l) => l.key).join(',')}:${org.error ?? ''}`}
      summary={rows !== null && rows.length > 0 ? grantSummary(rows) : undefined}
      detail={unread ? undefined : <OrgDetail org={org} rows={rows} repositories={repositories} checkedAt={checkedAt} onFix={onFix} />}
      hint={lines.length > 0 ? `Only an owner of ${org.org} can accept on GitHub.` : undefined}
      fix={lines.length > 0 && org.review_url !== null ? <FixLink href={org.review_url} label="Review on GitHub" onFix={onFix} /> : undefined}
    />
  )
}

export function AppCard({ app, admin, onFix }: { app: ForgeAppCard; admin: boolean | null; onFix: () => void }) {
  const gaps = app.missing ?? []
  const lines: Line[] = gaps.map((g, n) => ({ key: `m:${g.permission ?? n}`, words: g.needed_for, jargon: gapJargon(g) }))
  const unread = app.error !== null || app.missing === null
  const tone = lines.length > 0 ? 'bad' : unread ? 'unknown' : 'ok'
  // Both must say admin: the session (`admin`) and the server (`settings_url`, withheld from members).
  const canFix = admin === true && app.settings_url !== null
  return (
    <PermCard
      kind="app"
      name={app.slug ?? 'SwarmCloud App'}
      tone={tone}
      chip={unread && lines.length === 0 ? 'not read' : countWords(lines.length, 0)}
      lines={lines}
      error={app.error ?? (app.missing === null ? 'The App was not read.' : null)}
      sig={`${tone}:${lines.map((l) => l.key).join(',')}:${app.error ?? ''}`}
      hint={lines.length > 0 && !canFix ? 'A platform admin adds these to the App; then each org accepts the update.' : undefined}
      fix={lines.length > 0 && canFix && app.settings_url !== null ? <FixLink href={app.settings_url} label="Add to App" onFix={onFix} /> : undefined}
    />
  )
}

/** A token-backed repository's own card: what the tenant token was measured to do there. */
function TokenCard({ row }: { row: ForgeRepoStatus }) {
  const lines: Line[] = row.blocked_by.map((g, n) => ({ key: `b:${n}`, words: g.needed_for, jargon: gapJargon(g) }))
  const status = repoStatus(row)
  return (
    <PermCard
      kind="token"
      name={row.repo}
      tone={status.tone}
      chip={lines.length > 0 ? `${lines.length} blocked` : status.tone === 'ok' ? 'all granted' : 'not all measured'}
      lines={lines}
      error={row.error}
      sig={`${status.tone}:${lines.length}:${row.error ?? ''}`}
      hint={lines.length > 0 ? 'This repository is reached through the tenant token, not the App: a token with more access fixes it (Git tokens).' : status.tone === 'unknown' && row.error === null ? status.tip : undefined}
    />
  )
}

// ---------------------------------------------------------------------------
// one repository's status: the list's dot, and the card on its page
// ---------------------------------------------------------------------------

export interface RepoStatus {
  tone: 'ok' | 'bad' | 'unknown'
  /** The tooltip: the first blocked capability and its cause, or why it is not known. */
  tip: string
}

/**
 * A repository's verdict. RED when any capability was REFUSED, naming the
 * first one (in `CAPABILITY_WORD` order) and the cause that blocks it; GREEN
 * only when every capability was measured granted; otherwise the grey ring,
 * saying what was not measured -- a capability GitHub did not answer for is
 * never drawn as granted.
 */
export function repoStatus(row: ForgeRepoStatus): RepoStatus {
  const caps = row.capabilities
  const order = [...CAPABILITY_ORDER, ...Object.keys(caps).filter((k) => !CAPABILITY_ORDER.includes(k))]
  const blocked = order.find((c) => caps[c] === false)
  if (blocked !== undefined) {
    const cause = row.blocked_by.find((g) => g.capabilities.includes(blocked)) ?? row.blocked_by[0]
    const word = CAPABILITY_WORD[blocked] ?? blocked
    return { tone: 'bad', tip: cause === undefined ? `${word} is blocked` : `${word} is blocked: ${cause.needed_for}` }
  }
  if (row.error !== null) return { tone: 'unknown', tip: `Not checked: ${row.error}` }
  const unmeasured = order.filter((c) => c in caps && caps[c] === null)
  if (Object.keys(caps).length === 0) return { tone: 'unknown', tip: 'Not checked yet' }
  if (unmeasured.length > 0) {
    return { tone: 'unknown', tip: `Nothing refused; not measurable here: ${unmeasured.map((c) => CAPABILITY_WORD[c] ?? c).join(', ')}` }
  }
  return { tone: 'ok', tip: 'GitHub grants everything SwarmCloud needs here' }
}

/** The list's dot: the tone's mark, with its words as the tooltip and the accessible name. */
export function RepoDot({ row }: { row: ForgeRepoStatus | undefined }) {
  if (row === undefined) return null
  const s = repoStatus(row)
  return (
    <span className={`fp-dot is-${s.tone}`} role="img" aria-label={`GitHub permissions: ${s.tip}`} title={s.tip} data-status={s.tone}>
      <ToneMark tone={s.tone} hidden />
    </span>
  )
}

// ---------------------------------------------------------------------------
// the surfaces
// ---------------------------------------------------------------------------

function RecheckButton({ perms }: { perms: ForgePermsRead }) {
  return (
    <Button size="sm" kind="ghost" busy={perms.checking ? 'Re-checking…' : false} onClick={perms.recheck}>
      ↻ Re-check
    </Button>
  )
}

function checkedLine(doc: ForgePermissionsDoc): ReactNode {
  return (
    <>
      Checked {doc.checked_at === null ? 'at an unknown time' : timeAgo(doc.checked_at)}
      {doc.cached ? ' (from the last minute’s read)' : ''}. GitHub does not let SwarmCloud grant permissions itself, so a
      fix is always a link.
    </>
  )
}

/** Work › Setup › GitHub: the App's card, then one per org. */
export function GitHubPermissions() {
  const perms = useForgePermissions()
  const admin = useIsAdmin()
  return (
    <section className="fp-section" aria-labelledby="fp-github-h">
      <div className="c-card-h fp-head">
        <h2 id="fp-github-h">GitHub</h2>
        <span className="ur-acts">
          {(perms.state.status === 'ok' || perms.state.status === 'stale') && (
            <small className="ur-hint fp-when">{checkedWords(perms.state.data.checked_at)}</small>
          )}
          <RecheckButton perms={perms} />
        </span>
      </div>
      <UrRegion state={perms.state} route="GET /v1/forge/permissions" what="GitHub permissions" plural onRetry={perms.reload} lines={4}>
        {(doc) => (
          <div className="fp-cards">
            <p className="ur-hint fp-checked">{checkedLine(doc)}</p>
            <AppCard app={doc.app} admin={admin} onFix={perms.armRecheck} />
            {doc.orgs.length === 0 ? (
              <EmptyState kind="empty" heading="No connected org">
                No registered repository is reached through the App yet, so there is no org to check. Install the App
                where your repositories are, then register one.
              </EmptyState>
            ) : (
              doc.orgs.map((o) => (
                <OrgCard key={o.installation_id} org={o} required={doc.required} onFix={perms.armRecheck} repositories={doc.repositories} checkedAt={doc.checked_at} />
              ))
            )}
          </div>
        )}
      </UrRegion>
    </section>
  )
}

/** A repository's page: its own org's card (or, token-backed, what its token can do). */
export function RepoPermissions({ repoId }: { repoId: string }) {
  const perms = useForgePermissions()
  const state = perms.state
  // Said once read, and quiet while reading: a repository page has its own reads to show first. Quiet too on an
  // API that does not serve the read yet: Setup says "not served" in its own section, and a second "not served"
  // panel above every repository's tabs would read as something wrong with this repository. A failure still shows.
  if (state.status === 'loading' || (state.status === 'error' && notServed(state.error))) return null
  return (
    <section className="fp-section fp-repo" aria-label="GitHub permissions">
      <UrRegion state={state} route="GET /v1/forge/permissions" what="GitHub permissions" plural onRetry={perms.reload} lines={2}>
        {(doc) => {
          const row = doc.repositories.find((r) => r.repo_id === repoId)
          const org = row?.installation_id == null ? undefined : doc.orgs.find((o) => o.installation_id === row.installation_id)
          return (
            <div className="fp-cards">
              {row === undefined ? (
                <p className="ur-hint">This repository is not in the permissions read yet; re-check once it is registered.</p>
              ) : org !== undefined ? (
                <OrgCard org={org} required={doc.required} onFix={perms.armRecheck} repositories={doc.repositories} />
              ) : row.credential === 'token' ? (
                <TokenCard row={row} />
              ) : (
                <p className="ur-small fp-error">
                  <b className="ur-bad">Could not read</b> {row.error ?? 'Which credential reaches this repository was not read.'}
                </p>
              )}
              <p className="ur-hint fp-checked">
                {checkedLine(doc)} <RecheckButton perms={perms} />
              </p>
            </div>
          )
        }}
      </UrRegion>
    </section>
  )
}

/** The list's dots: one read, indexed by repository id. */
export function useRepoStatuses(): ReadonlyMap<string, ForgeRepoStatus> {
  const perms = useForgePermissions()
  const s = perms.state
  const rows = s.status === 'ok' || s.status === 'stale' ? s.data.repositories : []
  return new Map(rows.map((r) => [r.repo_id, r]))
}
