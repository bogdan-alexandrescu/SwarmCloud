import { useState, useSyncExternalStore, type ReactNode } from 'react'
import { loadForgePermissions, loadMe } from './api'
import { Button, ButtonLink, Card, ToneMark } from './components'
import type { Result } from './fetch'
import {
  grantLabel, reasonWords, repoStatusLine,
  type AppPermCard, type ForgePerms, type OrgPermCard, type PermGap, type PermStatus, type RepoPermStatus,
} from './ForgePermissionsData'
import { MarkGlyph } from './marks'
import { UrRegion } from './RepositoriesParts'
import { notServed } from './RepositoriesData'
import { timeAgo } from './types'
import './styles/forgeperms.css'

/**
 * GITHUB PERMISSIONS, IN THE CONSOLE (#1086, lane PERM-2; owner decision
 * 2026-10-11, design 2 "Org cards"). One read, PERM-1's
 * `GET /v1/forge/permissions`, drawn three ways:
 *
 *   * Work › Setup › GitHub: the App's card and one card per connected org;
 *   * Work › Repositories: a dot per repository, its tooltip naming the
 *     problem;
 *   * a repository's page: its own org's card (or, for a token-backed
 *     repository, what its token was measured to do).
 *
 * A CARD IS COLLAPSED ONLY WHEN EVERYTHING IS GRANTED. Missing or unknown
 * opens it: an unknown is never green (the server's rule, kept here), and a
 * card nobody can read is a card somebody should look at.
 *
 * A FIX IS ALWAYS A LINK. GitHub has no API through which an App widens its
 * own permissions or accepts a request for an org, so every fix is a button
 * opening the exact GitHub page a person acts on: the installation's
 * permission-update review page for an org, the App's permissions page for
 * the App. The App's page is a platform admin's: the server sends its URL to
 * admins only, and this file also draws its button only when
 * `principal.is_admin === true` -- two locks, so a server that sent the URL
 * to the wrong caller still shows nobody a button they cannot use.
 *
 * ONE SHARED READ. Setup, the list's dots and the repository page subscribe
 * to one store, so they always agree: a Re-check pressed on Setup turns the
 * dots on the same screen green with it. The store reads when its first
 * subscriber arrives, again whenever the tab is shown again
 * (`visibilitychange`), and on Re-check with `refresh=true`, which skips the
 * server's one-minute cache -- the button a person presses after accepting
 * on GitHub.
 */

type PermRead = Result<ForgePerms | null>

interface Snapshot {
  state: PermRead
  /** `principal.is_admin === true` from `GET /v1/tenants/me`; false until it is read. */
  admin: boolean
  /** A Re-check is in flight. */
  checking: boolean
}

let snap: Snapshot = { state: { status: 'loading', since: Date.now() }, admin: false, checking: false }
const subscribers = new Set<() => void>()
/** The newest read's number: an older answer landing late is dropped. */
let seq = 0

function publish(next: Partial<Snapshot>) {
  snap = { ...snap, ...next }
  for (const f of subscribers) f()
}

function readPermissions(refresh: boolean) {
  const mine = ++seq
  if (refresh) publish({ checking: true })
  void loadForgePermissions({ refresh }).then((r) => {
    if (mine !== seq) return
    const was = snap.state
    // A re-read that fails with an answer in hand is stale, never an error
    // (fetch.ts's rule, as `useUrRead` keeps it): the cards stay, dimmed.
    const state: PermRead =
      r.status === 'error' && (was.status === 'ok' || was.status === 'stale')
        ? { status: 'stale', data: was.data, fetchedAt: was.fetchedAt, error: r.error }
        : r
    publish({ state, checking: false })
  })
}

function readAdmin() {
  void loadMe().then((r) => {
    if (r.status === 'ok' || r.status === 'stale') publish({ admin: r.data.principal.is_admin === true })
  })
}

function onVisibility() {
  if (document.visibilityState === 'visible') readPermissions(false)
}

function subscribe(f: () => void): () => void {
  subscribers.add(f)
  if (subscribers.size === 1) {
    readPermissions(false)
    readAdmin()
    document.addEventListener('visibilitychange', onVisibility)
  }
  return () => {
    subscribers.delete(f)
    if (subscribers.size === 0) document.removeEventListener('visibilitychange', onVisibility)
  }
}

const current = () => snap

/** The shared permissions read, and the Re-check that reads GitHub again. */
export function useForgePermissions(): Snapshot & { recheck: () => void } {
  const s = useSyncExternalStore(subscribe, current)
  return { ...s, recheck: () => readPermissions(true) }
}

function dataOf(state: PermRead): ForgePerms | null {
  return state.status === 'ok' || state.status === 'stale' ? state.data : null
}

// ---------------------------------------------------------------------------
// The cards
// ---------------------------------------------------------------------------

const STATUS_TONE: Readonly<Record<PermStatus, string>> = { ok: 'ok', missing: 'bad', unknown: 'unknown' }

function statusWord(status: PermStatus, gaps: number): string {
  if (status === 'ok') return 'all granted'
  if (status === 'missing') return gaps > 0 ? `${gaps} missing` : 'missing'
  return 'not known'
}

/** The fix for one gap: a link to its exact GitHub page, or the words for why there is none here. */
function GapFix({ gap, admin }: { gap: PermGap; admin: boolean }) {
  if (gap.fix === 'app') {
    if (!admin) return <span className="ur-hint fp-why">A platform admin adds this on the App's permissions page.</span>
    if (gap.fix_url === null) return null
    return (
      <ButtonLink size="sm" href={gap.fix_url} target="_blank" rel="noreferrer">
        Change the App's permissions on GitHub
      </ButtonLink>
    )
  }
  if (gap.fix_url === null) return null
  return (
    <ButtonLink size="sm" kind="primary" href={gap.fix_url} target="_blank" rel="noreferrer">
      Review and accept on GitHub
    </ButtonLink>
  )
}

function GapList({ gaps, admin }: { gaps: PermGap[]; admin: boolean }) {
  if (gaps.length === 0) return null
  return (
    <ul className="fp-gaps">
      {gaps.map((g) => (
        <li key={g.permission} className="fp-gap" data-gap={g.permission}>
          <p className="ur-small">
            <b>{grantLabel(g.permission, g.level)}</b>
            <span className="fp-held"> · {g.held === null ? 'not held' : `holds ${g.held}`}</span>
          </p>
          <p className="ur-small fp-breaks">Without it, {g.breaks}.</p>
          <GapFix gap={g} admin={admin} />
        </li>
      ))}
    </ul>
  )
}

/** A card's frame: its head always, its body only while open; open by default unless everything is granted. */
function PermCard({
  kind,
  owner,
  status,
  gaps,
  title,
  children,
}: {
  kind: 'org' | 'app'
  owner: string | null
  status: PermStatus
  gaps: number
  title: ReactNode
  children: ReactNode
}) {
  const [open, setOpen] = useState(status !== 'ok')
  return (
    <div className={`fp-card is-${status}`} data-forge-card={kind} data-owner={owner ?? undefined} data-status={status}>
      <Card
        level={3}
        title={title}
        action={
          <span className="ur-acts">
            <ToneMark tone={STATUS_TONE[status]}>{statusWord(status, gaps)}</ToneMark>
            <Button size="sm" kind="ghost" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
              {open ? 'Hide' : 'Details'}
            </Button>
          </span>
        }
      >
        {open ? <div className="fp-body">{children}</div> : null}
      </Card>
    </div>
  )
}

/** One connected org: what its installation holds against what SwarmCloud needs. */
export function OrgCard({ card, admin }: { card: OrgPermCard; admin: boolean }) {
  const why = reasonWords(card.reason)
  const n = card.repositories.length
  return (
    <PermCard kind="org" owner={card.owner} status={card.status} gaps={card.gaps.length} title={card.owner}>
      {card.status === 'ok' && <p className="ur-hint">Every permission SwarmCloud's runs need is granted on this org.</p>}
      {why !== null && <p className={`ur-small fp-reason is-${card.status}`}>{why}</p>}
      {card.installation_url !== null && (
        <ButtonLink size="sm" href={card.installation_url} target="_blank" rel="noreferrer">
          Open the installation on GitHub
        </ButtonLink>
      )}
      <GapList gaps={card.gaps} admin={admin} />
      <p className="ur-hint">
        {n === 1 ? 'Covers 1 registered repository' : `Covers ${n} registered repositories`}
        {card.repository_selection === 'selected' ? ', under an installation limited to selected repositories.' : '.'}
      </p>
    </PermCard>
  )
}

/** The App itself: what it asks GitHub for. A permission it does not ask for, no org can grant. */
export function AppCard({ card, admin }: { card: AppPermCard; admin: boolean }) {
  const why = reasonWords(card.reason)
  return (
    <PermCard
      kind="app"
      owner={card.owner}
      status={card.status}
      gaps={card.gaps.length}
      title={
        <>
          GitHub App{card.slug !== null && <code className="fp-slug">{card.slug}</code>}
        </>
      }
    >
      {card.status === 'ok' && <p className="ur-hint">The App asks for every permission SwarmCloud's runs need.</p>}
      {why !== null && <p className={`ur-small fp-reason is-${card.status}`}>{why}</p>}
      {card.gaps.length > 0 && (
        <p className="ur-hint">The App does not ask for these, so no org can grant them until the App's owner adds them.</p>
      )}
      <GapList gaps={card.gaps} admin={admin} />
    </PermCard>
  )
}

const NO_GRANT = "GitHub does not let SwarmCloud grant permissions itself, so a fix is always a link to the GitHub page where a person accepts or changes it. Press Re-check once that is done."

function Recheck({ checking, recheck }: { checking: boolean; recheck: () => void }) {
  return (
    <Button size="sm" busy={checking} onClick={recheck}>
      Re-check
    </Button>
  )
}

/** Work › Setup › GitHub: the App's card and one card per connected org. */
export function GitHubPermissionsSection() {
  const { state, admin, checking, recheck } = useForgePermissions()
  const data = dataOf(state)
  return (
    <section className="fp-section" aria-label="GitHub" data-forge-admin={String(admin)}>
      <div className="fp-section-h">
        <h2>GitHub</h2>
        <span className="ur-acts">
          {data?.checked_at != null && <span className="ur-hint fp-at">checked {timeAgo(data.checked_at)}</span>}
          <Recheck checking={checking} recheck={recheck} />
        </span>
      </div>
      <p className="ur-hint">
        What SwarmCloud's runs need from GitHub, against what the App asks for and what each connected org has accepted.
        {' '}
        {NO_GRANT}
      </p>
      <UrRegion
        state={state}
        route="GET /v1/forge/permissions"
        what="The GitHub permissions check"
        onRetry={() => readPermissions(false)}
        empty={<p className="ur-hint">The permissions check answered with nothing this console can read.</p>}
      >
        {(d) =>
          d === null ? null : (
            <div className="fp-cards">
              {d.app !== null && <AppCard key={`app:${d.app.status}:${d.app.gaps.length}`} card={d.app} admin={admin} />}
              {d.orgs.map((o) => (
                <OrgCard key={`${o.owner}:${o.status}:${o.gaps.length}`} card={o} admin={admin} />
              ))}
              {d.orgs.length === 0 && <p className="ur-hint">No org is connected through the App yet.</p>}
            </div>
          )
        }
      </UrRegion>
    </section>
  )
}

// ---------------------------------------------------------------------------
// A repository's status
// ---------------------------------------------------------------------------

const DOT_MARK = { ok: 'succeeded', missing: 'failed', unknown: 'queued' } as const

/** The dot's status and its sentence, from the shared read. */
function dotOf(state: PermRead, repoId: string): { status: PermStatus; line: string } {
  if (state.status === 'loading') return { status: 'unknown', line: repoStatusLine(null, 'checking now') }
  if (state.status === 'error') {
    return { status: 'unknown', line: repoStatusLine(null, notServed(state.error) ? 'the check is not served by this API yet' : state.error.message) }
  }
  const row = dataOf(state)?.repositories.find((r) => r.repo_id === repoId) ?? null
  if (row === null) return { status: 'unknown', line: repoStatusLine(null, 'this repository was not in the answer') }
  return { status: row.status, line: repoStatusLine(row) }
}

/** One repository's GitHub permissions as a dot: green ok, red missing, grey unknown. The tooltip names the problem. */
export function RepoPermDot({ repoId }: { repoId: string }) {
  const { state } = useForgePermissions()
  const { status, line } = dotOf(state, repoId)
  return (
    <span className={`fp-dot is-${status}`} data-perm-dot={repoId} data-status={status} role="img" aria-label={line} title={line}>
      <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
        <MarkGlyph mark={DOT_MARK[status]} />
      </svg>
    </span>
  )
}

/** A token-backed repository: what the token probe measured, since no org card covers it. */
function TokenRepoCard({ row }: { row: RepoPermStatus }) {
  const why = reasonWords(row.reason)
  return (
    <div className={`fp-card is-${row.status}`} data-forge-card="token" data-status={row.status}>
      <Card
        level={3}
        title="GitHub token"
        action={<ToneMark tone={STATUS_TONE[row.status]}>{statusWord(row.status, row.problems.length)}</ToneMark>}
      >
        <p className="ur-hint">This repository is reached through a stored token, so this is what the token probe measured it can do.</p>
        {why !== null && <p className={`ur-small fp-reason is-${row.status}`}>{why}</p>}
        {row.problems.length > 0 && (
          <ul className="fp-gaps">
            {row.problems.map((p) => (
              <li key={p.permission} className="fp-gap" data-gap={p.permission}>
                <p className="ur-small">
                  <b>{grantLabel(p.permission, p.level)}</b>
                </p>
                <p className="ur-small fp-breaks">Without it, {p.breaks}.</p>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  )
}

/**
 * A repository's page: its own org's card, never another org's; for a
 * token-backed repository, its token's measured status. Problems the org
 * card does not carry (the repository left out of a selected-repositories
 * installation) are said above it. Nothing is drawn until the read lands or
 * when it is not served: Setup is where the read's own state is shown.
 */
export function RepoForgeCard({ repoId, owner }: { repoId: string; owner: string }) {
  const { state, admin, checking, recheck } = useForgePermissions()
  const data = dataOf(state)
  if (data === null) return null
  const row = data.repositories.find((r) => r.repo_id === repoId) ?? null
  let body: ReactNode = null
  if (row?.source === 'token') {
    body = <TokenRepoCard row={row} />
  } else {
    const on = (row?.owner || owner).toLowerCase()
    const org = data.orgs.find((o) => o.owner.toLowerCase() === on) ?? null
    if (org === null) return null
    const held = new Set(org.gaps.map((g) => g.permission))
    const extra = (row?.problems ?? []).filter((p) => !held.has(p.permission) && p.permission !== 'installation')
    body = (
      <>
        {extra.map((p) => (
          <p key={p.permission} className="ur-small fp-reason is-missing" data-repo-problem={p.permission}>
            {p.breaks}.
          </p>
        ))}
        <OrgCard key={`${org.owner}:${org.status}:${org.gaps.length}`} card={org} admin={admin} />
      </>
    )
  }
  return (
    <section className="fp-section fp-repo" aria-label="GitHub permissions" data-forge-admin={String(admin)}>
      <div className="fp-section-h">
        <span className="ur-hint">{NO_GRANT}</span>
        <span className="ur-acts">
          <Recheck checking={checking} recheck={recheck} />
        </span>
      </div>
      {body}
    </section>
  )
}
