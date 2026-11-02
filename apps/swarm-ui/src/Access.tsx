import { Fragment, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react'
import {
  disableAccessOwner, disconnectGitHub, enableAccessOwner, githubAppName, loadAccess, loadAccessOwners,
  loadAccessRepositories, putAccessGrant, requestAccessInstall, revokeAccessGrant, typedRepoId,
} from './api'
import { Banner, Button, ButtonLink, Card, Chip, Dash, EmptyState, Tabs, ToneMark, type TabDef } from './components'
import { setupCount, setupNext, useSetupSteps } from './ConfigSetup'
import type { ApiError, Result } from './fetch'
import { ConnectButton, Refusal } from './GitHubConnect'
import { WORKSPACE_SETUP_ADDRESS, addressToPath } from './paths'
import { UrCap, UrRadio, UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { notServed, type CapCell } from './RepositoriesData'
import { githubTokenState } from './githubTokenState'
import { PageHead } from './Shell'
import { usePhoneTables } from './capacityPoll'
import type {
  AccessDisableResponse, AccessGrant, AccessMode, AccessOverview, AccessOwner, AccessRepository, GitHubConnection,
} from './types'
import { humaniseUntil, timeAgo } from './types'
import { AGE_TICK_MS, useNow } from './useNow'
import './styles/repositories.css'
import './styles/keys.css'
import './styles/onboarding.css'

/**
 * WORK › ACCESS (#780, lane OB8; docs/onboarding.md §2.4, owner picks D10:
 * Access A for the person, Access B for an admin, Chooser A inside it, Verify
 * A for the grants). Everything here is OB4's routes (swarm_api/routes/
 * access.py) through api.ts's typed clients, plus OB3's Connect and
 * Disconnect, and every one of them acts as THE CALLER, through the caller's
 * own GitHub connection, in the caller's tenant.
 *
 * WHAT THE PAGE STATES, BECAUSE THE OWNER DECIDED IT (#780, 2026-10-07):
 *
 *   * D9 -- a read grant is enforced by SwarmCloud, at submission and in the
 *     worker. GitHub would let the push through if the person can write, and
 *     the page says so rather than implying GitHub enforces it;
 *   * D8 -- the App asks for contents, pull requests, issues and checks (read);
 *     not workflows write, so a change under `.github/workflows/` cannot be
 *     pushed through SwarmCloud;
 *   * D6 -- verification reads only by default. The opt-in Push test, on a
 *     write grant only, creates and deletes one
 *     `swarmcloud/onboarding-check-<nonce>` branch (`access.verify` with
 *     `checks: ["push_test"]`). It is the one thing on this page that writes
 *     to a repository, so it asks first, names the branch pattern and the
 *     repository, and when the delete failed names the branch left behind so
 *     the person can delete it.
 *
 * AN OWNER ENABLED BY TOKEN (D5) says "via token": the person stored a
 * fallback token for it (Admin settings, or `sc setup token --owner`), and
 * SwarmCloud reaches it with that token, not the App.
 *
 * AN INSTALL SOMEONE ELSE APPROVES (§2.3 ORG_APPROVAL_PENDING). GitHub shows
 * a user token no pending install request, so Request install on an org
 * opens the App's install page -- where GitHub offers a non-owner its request
 * form -- and records the request with `POST .../install-request`. The row
 * then draws the §2.3 copy the server sent until GitHub lists the
 * installation (re-checked on every owners read and by the 15-minute sweep).
 *
 * CHOOSER A: owners on the left -- the installations, plus the person's orgs
 * with none -- and the selected owner's repositories on the right, ONE PAGE AT
 * A TIME and searched server-side, each Not chosen, Read or Write, with
 * whether the person can push shown BEFORE Write is offered. Not listed?
 * Type owner/repo; the server reads it once and answers a refusal with the
 * §2.3 copy, which is drawn word for word.
 *
 * VERIFY A: a grid, failures first, the fix inline. `unknown` is a measured
 * answer (the check could not be read), never a pass, and is drawn grey.
 *
 * CONNECTED IS NOT INSTALLED (#780, found live 2026-10-08). The owner
 * connected GitHub -- the App showed under Authorized GitHub Apps -- but never
 * installed it, and this page showed no orgs and no repositories with no word
 * why: GitHub lists an org to the App only where the App is installed. So
 * when no owner has an installation the page says so first, with Install
 * buttons; each owner row with none offers Install; and because installing
 * happens on GitHub's page, the owners are read again on Refresh and when the
 * window regains focus.
 */

type View = 'mine' | 'choose'

/**
 * CONNECTION AND CHOOSE REPOSITORIES ARE TWO TABS OF MINE (GITHUB-ACCESS-PAGE;
 * part of #2157, proposal E, config-e.html S2a-S2f). The page is the GitHub App
 * integration -- connect, disconnect, who you are on GitHub -- and the chooser
 * where repositories are added, and nothing else: "Repositories SwarmCloud may
 * reach as you" is Config › Repositories' (ReachableRepositories.tsx). Mine is
 * the Connection tab, at the bare path; Choose repositories is `?view=choose`.
 *
 * MEMBERS AND BROWSER LOGINS LEFT FOR THE PAGES WHOSE SUBJECT THEY ARE
 * (GA-MOVE-VIEWS, BGD 2026-10-24): Members is People and roles' Members'
 * GitHub tab (WorkspacePeople.tsx), Browser logins is Secrets' (WorkspaceSecrets.tsx).
 * Their old `?view=` here lands there (paths.ts `MOVED_VIEWS`), so S2 draws no
 * switch in the head.
 */
const GITHUB_TABS: readonly TabDef[] = [
  { key: 'mine', label: 'Connection' },
  { key: 'choose', label: 'Choose repositories' },
]

/**
 * WHERE A NEW REPOSITORY IS ADDED: Choose repositories, opened directly.
 * Config › Repositories' "Choose more on GitHub" ("Choose on GitHub" while nothing is chosen) links here. It rides on `view`
 * because `view` is the one query key App's `keptKeys` keeps on GitHub
 * access; an `?add=1` would be cut off the address before the page read it.
 */
export const ADD_REPOSITORY_ADDRESS = 'config/github?view=choose'

/** Config › Repositories, where the reach-as-you list is (CONFIG-NAV made the tab). */
const REPOSITORIES_ADDRESS = 'config/repositories'

/**
 * THE TAB IS IN THE ADDRESS (live QA RSA-28, 2026-10-11), so a reload or a
 * shared link returns to it: Choose repositories is `?view=choose`, Connection
 * the bare path. Any other value opens Connection.
 */
function viewFromUrl(): View {
  if (typeof window === 'undefined') return 'mine'
  return new URLSearchParams(window.location.search).get('view') === 'choose' ? 'choose' : 'mine'
}

function writeViewToUrl(v: View): void {
  const url = new URL(window.location.href)
  if (v !== 'mine') url.searchParams.set('view', v)
  else url.searchParams.delete('view')
  window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`)
}

/**
 * ASK FIRST, AND MEAN IT (live QA RSA-1 and RSA-27, 2026-10-11). Every action
 * here that takes access away -- Disconnect, removing an org, removing a grant,
 * a grant moved down from Write or to Not chosen -- and the push test, which
 * writes, waits for a yes in this banner. Removing a grant took one click: a
 * misclick on a write grant's row dropped it, and the repository's tasks were
 * refused at submission from then on.
 *
 * The banner takes focus to its SAFE button when it opens (focus stayed on
 * <body>, so a keyboard user did not know it was there), and Esc is Keep: the
 * same as the safe button, never the action.
 */
export function AskFirst({
  title,
  children,
  act,
  actKind = 'danger-filled',
  keep,
  busy = false,
  onAct,
  onKeep,
}: {
  title: string
  children: ReactNode
  act: string
  actKind?: 'danger-filled' | 'primary'
  keep: string
  busy?: boolean
  onAct: () => void
  onKeep: () => void
}) {
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    box.current?.querySelector<HTMLButtonElement>('button[data-safe]')?.focus()
  }, [])
  return (
    <div
      ref={box}
      className="ac-ask"
      data-asking={title}
      onKeyDown={(e) => {
        if (e.key === 'Escape' && !busy) {
          e.stopPropagation()
          onKeep()
        }
      }}
    >
      <Banner
        tone="warn"
        title={title}
        actions={
          <>
            <Button kind={actKind} size="sm" busy={busy} onClick={onAct}>
              {act}
            </Button>
            <Button size="sm" data-safe="" onClick={onKeep} disabled={busy}>
              {keep}
            </Button>
          </>
        }
      >
        {children}
      </Banner>
    </div>
  )
}

/**
 * FOCUS GOES BACK TO THE OPENER (QA1-FIX-01 config-05). The button that opens
 * an AskFirst is unmounted while it asks, so Esc or Keep left focus on <body>
 * and threw a keyboard user to the top of the page. The box this ref is put on
 * holds the opener; when the ask closes (Esc, Keep, or the action answered and
 * the opener is drawn again), its first button takes focus.
 */
function useFocusBack(asking: boolean) {
  const box = useRef<HTMLDivElement>(null)
  const was = useRef(asking)
  useEffect(() => {
    if (was.current && !asking) box.current?.querySelector<HTMLButtonElement>('button')?.focus()
    was.current = asking
  }, [asking])
  return box
}

/**
 * WHEN THE TOKEN EXPIRES, FORWARD. This read `timeAgo(access_expires_at)`,
 * and `timeAgo` measures the past: an instant eight hours AHEAD is a negative
 * age, clamped to zero, so a fresh token read "expires just now" (QA of
 * /access, 2026-10-08). An instant ahead is counted with `humaniseUntil`; one
 * behind says expired, and how long ago.
 */
export function tokenExpiry(iso: string, now: number): string {
  const t = new Date(iso).getTime()
  if (!Number.isFinite(t)) return 'expiry unreadable'
  return t > now ? `expires in ${humaniseUntil(t - now)}` : `expired ${timeAgo(t, now)}`
}

/**
 * HOW LONG A PASSING 409 IS GIVEN BEFORE ITS ONE RETRY. swarm-api answers 409
 * `conflict` while a refresh of the caller's GitHub token holds its lease
 * (access.py, "a refresh of your GitHub token is running"), and the first
 * reads after an Enable are the likeliest to meet it. A refusal also travels
 * as 409 (`access_refused`, `github_not_connected`) and is an answer, so it is
 * never retried. ONE retry, not a loop: a second 409 is shown as it is.
 */
export const SETTLE_RETRY_MS = 1200

export function passingConflict(e: ApiError): boolean {
  return e.httpStatus === 409 && e.code !== 'access_refused' && e.code !== 'github_not_connected'
}

/** Run `call`; on a passing 409, say so through `onWait`, wait, and run it once more. */
export async function onceMoreOnConflict<T>(call: () => Promise<Result<T>>, onWait: (waiting: boolean) => void): Promise<Result<T>> {
  const first = await call()
  if (first.status !== 'error' || !passingConflict(first.error)) return first
  onWait(true)
  await new Promise((r) => setTimeout(r, SETTLE_RETRY_MS))
  try {
    return await call()
  } finally {
    onWait(false)
  }
}

/** Said while the owners or one owner's repositories are read: GitHub can take several seconds to answer. */
export const ASKING_GITHUB = 'Asking GitHub for your repositories…'
export const STILL_SETTING_UP = 'Still setting up…'

function dataOf<T>(r: Result<T>): T | null {
  return r.status === 'ok' || r.status === 'stale' ? r.data : null
}

/** A refused write's GitHub page (`AccessRefused.detail.url`), when it named one. */
function refusalUrl(e: ApiError): string | null {
  const d = e.detail as { url?: unknown } | null | undefined
  return d && typeof d.url === 'string' && d.url.startsWith('https://github.com/') ? d.url : null
}

function isGitHubUrl(url: string | null | undefined): url is string {
  return typeof url === 'string' && url.startsWith('https://github.com/')
}

export function GitHubLink({ url, children }: { url: string; children: ReactNode }) {
  return (
    <a className="c-link" href={url} target="_blank" rel="noreferrer">
      {children}
    </a>
  )
}

/** A refusal with the server's §2.3 copy, its GitHub page, and an optional retry. */
export function AccessRefusal({ error, title, onRetry }: { error: ApiError; title: string; onRetry?: () => void }) {
  const url = refusalUrl(error)
  return (
    <Refusal
      error={error}
      title={title}
      actions={
        url !== null || onRetry !== undefined ? (
          <>
            {url !== null && <GitHubLink url={url}>Open on GitHub</GitHubLink>}
            {onRetry !== undefined && (
              <Button size="sm" onClick={onRetry}>
                Re-check
              </Button>
            )}
          </>
        ) : undefined
      }
    />
  )
}

// ---------------------------------------------------------------------------
// What SwarmCloud may do as you: the owner's decisions, stated (D6, D8, D9)
// ---------------------------------------------------------------------------

/** The owner's three rules: each one's headline, S2b's short line, and the sentences that say how it is held. */
const POLICY_RULES: ReadonlyArray<{ rule: string; head: string; line: string; body: ReactNode }> = [
  {
    rule: 'D9',
    head: 'Read means read, and SwarmCloud enforces it.',
    line: 'A read grant never pushes, whatever GitHub would allow.',
    body: (
      <>
        A repository granted Read is cloned and never pushed: submission refuses an agent that would push to it, and the
        worker refuses the push. GitHub itself would let the push through if your account can write there, so the line
        is held by SwarmCloud, not by GitHub.
      </>
    ),
  },
  {
    rule: 'D8',
    head: 'No workflow changes.',
    line: 'SwarmCloud never asks for the workflows permission.',
    body: (
      <>
        The SwarmCloud App asks GitHub for contents, pull requests, issues and checks (read). It does not ask for
        workflows write, so a change under <code>.github/workflows/</code> cannot be pushed through SwarmCloud.
      </>
    ),
  },
  {
    rule: 'D6',
    head: 'Verifying only reads.',
    line: 'Push test is opt-in, per repository.',
    body: (
      <>
        Clone reads the upload-pack advertisement, Push the receive-pack advertisement and your push permission, Pull
        request the installation&apos;s pull-request permission. Nothing is written to GitHub unless you ask: Push test,
        on a write grant, creates one branch named <code>swarmcloud/onboarding-check-&lt;nonce&gt;</code> and deletes it
        again.
      </>
    ),
  },
]

/**
 * ON A PHONE THE RULES OPEN FOLDED (visual QA V122): each is its headline, a
 * summary that opens its sentences, so the card is three lines rather than
 * the first screen of every visit.
 *
 * WIDER, THE RULES ARE S2B'S CHECKLIST (VQ-038, GITHUB-ACCESS-S2B): the
 * right-hand column beside the GitHub card, each rule a check, its headline
 * and one short line. Three bulleted paragraphs at full width were the page's
 * longest block; how each rule is held stays one fold away ("How"), so no
 * sentence leaves the page.
 */
export function AccessPolicy() {
  const phone = usePhoneTables()
  return (
    <Card className="ac-policy" title="What SwarmCloud may do as you" level={2}>
      {phone ? (
        <ul className="ac-rules is-folded">
          {POLICY_RULES.map((r) => (
            <li key={r.rule} data-rule={r.rule}>
              <details>
                <summary>
                  <b>{r.head}</b>
                </summary>
                <p>{r.body}</p>
              </details>
            </li>
          ))}
        </ul>
      ) : (
        <ul className="ac-rules is-check">
          {POLICY_RULES.map((r) => (
            <li key={r.rule} data-rule={r.rule}>
              <ToneMark tone="ok" />
              <span className="ac-rule-t">
                <b>{r.head}</b>
                <small>{r.line}</small>
                <details className="ac-rule-how">
                  <summary>How</summary>
                  <p>{r.body}</p>
                </details>
              </span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}

// ---------------------------------------------------------------------------
// The connection
// ---------------------------------------------------------------------------

function Connection({ connection, quota, onChange }: { connection: GitHubConnection | null; quota: GitHubQuota[]; onChange: () => void }) {
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const [said, setSaid] = useState<string | null>(null)
  const now = useNow(AGE_TICK_MS)
  const opener = useFocusBack(confirming)

  async function disconnect() {
    setBusy(true)
    setRefused(null)
    const res = await disconnectGitHub()
    setBusy(false)
    setConfirming(false)
    if (res.status === 'error') {
      setRefused(res.error)
      return
    }
    setSaid(res.status === 'ok' ? res.data.github : null)
    onChange()
  }

  const active = connection !== null && connection.state === 'active'
  // An expiry already past is not "renewed automatically" (QA1-FIX-01
  // config-01): the 15-minute sweep did not reach it, and forgeapp.py renews a
  // token on its next use, so that is what the card says, in warn tone.
  const expiresAt = connection?.access_expires_at ?? null
  const expiresMs = expiresAt === null ? NaN : new Date(expiresAt).getTime()
  const expired = Number.isFinite(expiresMs) && expiresMs <= now
  const tokenState = connection === null ? null : githubTokenState(connection.state, expiresAt, now)
  return (
    <Card className="ac-conn" title="GitHub" action={<Chip>acts as you</Chip>}>
      {said !== null && (
        <Banner tone="info" title="GitHub was disconnected" role="status">
          <p className="ur-small">{said}</p>
        </Banner>
      )}
      {connection === null || connection.state === 'revoked' ? (
        <>
          {/* Connect GitHub is the lead's one button (GitHubLead); the card says
              what it will show, as S2a draws it, rather than drawing it twice. */}
          <p className="ur-cmeta">
            <span>
              <ToneMark tone="unknown">not connected</ToneMark>
            </span>
          </p>
          <p className="ur-small">Once connected, this card shows your GitHub login, the token&apos;s renewal and Disconnect GitHub.</p>
        </>
      ) : (
        <>
          <p className="ur-cmeta">
            <span>
              {connection.forge_login !== null ? <b>@{connection.forge_login}</b> : <Dash why="The account was not read yet" />}
            </span>
            <span>{connection.method === 'app_user' ? 'GitHub App · user token' : (connection.method ?? 'connection')}</span>
            <span>
              <ToneMark tone={tokenState?.tone ?? 'bad'}>{tokenState?.word ?? 'unknown'}</ToneMark>
            </span>
          </p>
          <p className="ur-cmeta">
            <span>
              Token{' '}
              {connection.access_expires_at === null ? (
                <Dash why="No expiry recorded" />
              ) : expired ? (
                <ToneMark tone="warn">{tokenExpiry(connection.access_expires_at, now)}</ToneMark>
              ) : (
                <b>{tokenExpiry(connection.access_expires_at, now)}</b>
              )}
            </span>
            {/* An expired token's renewal is said once, in the state above. */}
            {!expired && <span>renewed automatically</span>}
            {connection.refreshed_at !== null && <span>last renewed {timeAgo(connection.refreshed_at)}</span>}
          </p>
          {!active && (
            <p className="ur-small ur-bad">
              This connection is {connection.state ?? 'not active'}
              {connection.failure !== null ? ` (${connection.failure})` : ''}, so SwarmCloud cannot act as you until you
              reconnect: Reconnect is above.{' '}
              {/* The address CONFIG-NAV's SECTIONS spell, linked at its own path rather than `/setup`'s redirect. */}
              <a href={addressToPath(WORKSPACE_SETUP_ADDRESS)}>Config › Workspace Settings › Workspace setup</a> carries the
              recovery steps.
            </p>
          )}
          {confirming ? (
            <AskFirst
              title={connection.forge_login !== null ? `Disconnect @${connection.forge_login}?` : 'Disconnect GitHub?'}
              act="Disconnect"
              keep="Keep connected"
              busy={busy}
              onAct={() => void disconnect()}
              onKeep={() => setConfirming(false)}
            >
              <p className="ur-small">
                SwarmCloud revokes its authorisation at GitHub, disables the token it stored for you and forgets every
                org and repository you granted. Agents you submit afterwards wait until you connect again.
              </p>
            </AskFirst>
          ) : (
            <div className="ur-gh-acts" ref={opener}>
              <Button kind="danger" size="sm" onClick={() => setConfirming(true)}>
                Disconnect GitHub
              </Button>
            </div>
          )}
        </>
      )}
      <GitHubQuotaLines quota={quota} />
      {refused !== null && <Refusal error={refused} title="GitHub was not disconnected" />}
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Owners: chooser A's left column
// ---------------------------------------------------------------------------

function ownerKind(o: AccessOwner): string {
  return o.owner_type === 'User' ? 'your account' : 'organisation'
}

function ownerWords(o: AccessOwner): string {
  const kind = ownerKind(o)
  if (o.method === 'pat') return `${kind} · via token`
  if (o.install_state === 'requested') return `${kind} · install requested`
  if (o.install_state !== 'installed') return `${kind} · not installed`
  return `${kind} · ${o.repository_selection === 'all' ? 'all repositories' : 'selected repositories'}`
}

/**
 * REQUEST INSTALL, from the chooser's owner card and from Where the App is
 * installed. GitHub decides on its own page whether this person installs or
 * requests; the page opens first, in the click, so no popup blocker stands
 * between them, and the request is recorded beside it. An owner already
 * enabled answers `recorded: false` and is drawn as it is on the re-read.
 */
function askToInstall(o: AccessOwner) {
  if (isGitHubUrl(o.install_url)) window.open(o.install_url, '_blank', 'noopener,noreferrer')
  return requestAccessInstall(o.owner)
}

/**
 * What an owner's line in the phone select says after its name: the card's
 * chip for an enabled owner ("2 chosen"), its install state for one that is
 * not, so the select answers what the card would before it is opened.
 */
export function ownerOption(o: AccessOwner, granted: number): string {
  let state: string
  if (o.enabled) state = `${granted} chosen`
  else if (o.method === 'pat') state = 'via token'
  else if (o.install_state === 'requested') state = 'install requested'
  else if (o.install_state === 'installed') state = 'installed, not enabled'
  else state = 'not installed'
  return `${o.owner} · ${state}${o.sso === 'required' ? ' · SSO not authorised' : ''}`
}

function Owners({
  owners,
  orgsListed,
  grants,
  selected,
  phone = false,
  onSelect,
  onEnabled,
  onChange,
}: {
  owners: AccessOwner[]
  orgsListed: boolean
  grants: AccessGrant[]
  selected: string | null
  /**
   * PHONE CHOOSER (GA-PHONE-CHOOSER, S2f; BGD 2026-10-24). Under 560px the
   * owner column is a select and only the selected owner's card is drawn: a
   * stack of every card put the repositories a screen or more below the top.
   * `selected` may then be an owner not enabled yet, whose card is where
   * Install and Enable are.
   */
  phone?: boolean
  onSelect: (owner: string) => void
  /** The POST answered: the owner is enabled, so its repositories may be read now and not before. */
  onEnabled: (owner: string) => void
  onChange: () => void
}) {
  const [busy, setBusy] = useState<string | null>(null)
  const [settling, setSettling] = useState(false)
  const [refused, setRefused] = useState<{ owner: string; error: ApiError } | null>(null)
  const [removing, setRemoving] = useState<string | null>(null)
  const [removed, setRemoved] = useState<AccessDisableResponse | null>(null)

  // The repositories are read only once the POST has answered (`onEnabled`):
  // reading them alongside it asked for an owner not enabled yet (QA,
  // 2026-10-08). A second click while one is pending is dropped.
  async function enable(owner: string) {
    if (busy !== null) return
    setBusy(owner)
    setRefused(null)
    const res = await onceMoreOnConflict(() => enableAccessOwner(owner), setSettling)
    setBusy(null)
    if (res.status === 'error') {
      setRefused({ owner, error: res.error })
      return
    }
    onEnabled(owner.toLowerCase())
    onChange()
  }

  async function disable(owner: string) {
    setBusy(owner)
    setRefused(null)
    const res = await disableAccessOwner(owner)
    setBusy(null)
    setRemoving(null)
    if (res.status === 'error') {
      setRefused({ owner, error: res.error })
      return
    }
    setRemoved(res.status === 'ok' ? res.data : null)
    onChange()
  }

  async function request(o: AccessOwner) {
    if (busy !== null) return
    setBusy(o.owner)
    setRefused(null)
    const res = await askToInstall(o)
    setBusy(null)
    if (res.status === 'error') {
      setRefused({ owner: o.owner, error: res.error })
      return
    }
    onChange()
  }

  /** Withdraw a request: the same DELETE as Remove, which for a requested owner deletes only the request. */
  async function withdraw(owner: string) {
    setBusy(owner)
    setRefused(null)
    const res = await disableAccessOwner(owner)
    setBusy(null)
    if (res.status === 'error') {
      setRefused({ owner, error: res.error })
      return
    }
    onChange()
  }

  const grantedTo = (key: string) => grants.filter((g) => g.owner.toLowerCase() === key).length
  const drawn = phone ? owners.filter((o) => o.owner.toLowerCase() === selected) : owners
  return (
    <div className="ac-owners">
      {phone && (
        <label className="c-field ac-owner-select">
          <span className="c-lbl">
            {owners.length === 1 ? 'Owner · the one your connection reaches' : `Owner · one of ${owners.length} your connection reaches`}
          </span>
          <select className="c-inp" value={selected ?? ''} onChange={(e) => onSelect(e.target.value)}>
            {owners.map((o) => {
              const key = o.owner.toLowerCase()
              return (
                <option key={key} value={key}>
                  {ownerOption(o, grantedTo(key))}
                </option>
              )
            })}
          </select>
        </label>
      )}
      <ul className="ac-owner-list" aria-label={phone ? 'The selected owner' : 'Owners your connection reaches'}>
        {drawn.map((o) => {
          const key = o.owner.toLowerCase()
          const granted = grantedTo(key)
          const isSel = selected === key
          return (
            <li key={key} className={`ac-owner${isSel ? ' is-selected' : ''}`} data-owner={o.owner}>
              <div className="ac-owner-h">
                {o.enabled && !phone ? (
                  <button type="button" className="ac-owner-pick" aria-pressed={isSel} onClick={() => onSelect(key)}>
                    <b>{o.owner}</b>
                  </button>
                ) : (
                  <b>{o.owner}</b>
                )}
                {o.enabled && <Chip>{granted} chosen</Chip>}
              </div>
              <small className="ur-mu">
                {ownerWords(o)}
                {o.sso === 'required' && ' · SSO not authorised'}
              </small>
              <div className="ac-owner-acts">
                {o.enabled ? (
                  removing === key ? (
                    <AskFirst
                      title={`Remove ${o.owner}?`}
                      act="Remove"
                      keep="Keep"
                      busy={busy === o.owner}
                      onAct={() => void disable(o.owner)}
                      onKeep={() => setRemoving(null)}
                    >
                      <p className="ur-small">
                        Deletes your {granted} grant{granted === 1 ? '' : 's'} under {o.owner} at once; agents naming its
                        repositories are refused from then on.
                      </p>
                    </AskFirst>
                  ) : (
                    // Its scope in its words (QA1-FIX-01 config-06): plain "Remove"
                    // did not say every grant under the owner goes with it.
                    <Button size="sm" kind="danger" onClick={() => setRemoving(key)}>
                      {granted === 0 ? 'Remove' : granted === 1 ? 'Remove its 1 grant' : `Remove all ${granted}`}
                    </Button>
                  )
                ) : o.install_state === 'requested' ? (
                  <>
                    <Button size="sm" disabled={busy !== null} onClick={onChange}>
                      Re-check
                    </Button>
                    <Button size="sm" kind="ghost" busy={busy === o.owner} disabled={busy !== null && busy !== o.owner} onClick={() => void withdraw(o.owner)}>
                      Withdraw request
                    </Button>
                  </>
                ) : o.install_state === 'installed' ? (
                  <Button
                    size="sm"
                    kind="primary"
                    busy={busy === o.owner ? (settling ? STILL_SETTING_UP : 'Enabling…') : false}
                    disabled={busy !== null && busy !== o.owner}
                    onClick={() => void enable(o.owner)}
                  >
                    Enable
                  </Button>
                ) : !isGitHubUrl(o.install_url) ? (
                  <Dash why="The App's install page is not configured on this API" />
                ) : o.owner_type === 'Organization' ? (
                  // ONE ACTION PER OWNER STATE, the same as Where the App is
                  // installed draws (QA1-FIX-01 config-04): an organisation is
                  // asked for, and askToInstall opens the install page too, where
                  // an owner of it installs instead.
                  <Button
                    size="sm"
                    kind="primary"
                    busy={busy === o.owner}
                    disabled={busy !== null && busy !== o.owner}
                    title={`Not an owner of ${o.owner}? GitHub lets you ask its owners to install the App`}
                    onClick={() => void request(o)}
                  >
                    Request install
                  </Button>
                ) : (
                  <ButtonLink size="sm" kind="primary" href={o.install_url} target="_blank" rel="noreferrer">
                    Install on {o.owner}
                  </ButtonLink>
                )}
              </div>
              {o.install_state === 'requested' && typeof o.copy === 'string' && (
                <div className="ob-issue" data-code={o.code ?? 'ORG_APPROVAL_PENDING'}>
                  <p className="ur-small">
                    <b>{o.code ?? 'ORG_APPROVAL_PENDING'}</b> {o.copy}
                  </p>
                  {typeof o.requested_at === 'string' && <small className="ur-mu">asked {timeAgo(o.requested_at)}</small>}
                </div>
              )}
              {refused !== null && refused.owner === o.owner && (
                <AccessRefusal error={refused.error} title={`${o.owner} was not changed`} onRetry={onChange} />
              )}
            </li>
          )
        })}
      </ul>
      {!orgsListed && <p className="ur-hint">GitHub did not list your orgs this time; installations are still listed.</p>}
      {removed !== null && (
        <Banner tone="info" title={`${removed.owner} was removed`} role="status">
          <p className="ur-small">
            {removed.grants_deleted} grant{removed.grants_deleted === 1 ? '' : 's'} deleted. GitHub still lets your token
            reach an installed org until an owner uninstalls the App or you disconnect.
            {removed.installation_settings_url !== null && (
              <>
                {' '}
                <GitHubLink url={removed.installation_settings_url}>The installation's settings</GitHubLink>
              </>
            )}
          </p>
        </Banner>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// One owner's repositories: chooser A's right column
// ---------------------------------------------------------------------------

export type Choice = 'none' | AccessMode

function pushCell(r: { can_push: boolean | null }): CapCell {
  if (r.can_push === true) return { state: 'ok', reason: 'GitHub lets you push here' }
  if (r.can_push === false) return { state: 'missing', reason: 'You can read, not push' }
  return { state: 'unknown', reason: 'GitHub did not say' }
}

function writeRefusedWhy(r: AccessRepository): string | undefined {
  if (r.archived) return 'Archived on GitHub: nothing can be pushed to it'
  if (r.can_push === false) return 'GitHub does not let you push here'
  return undefined
}

/** Rank of a choice by what it lets SwarmCloud do: a move down the rank takes access away. */
const CHOICE_RANK: Record<Choice, number> = { none: 0, read: 1, write: 2 }

/** Whether moving from `from` to `to` takes access away, and so asks first (RSA-1). */
export function takesAccessAway(from: Choice, to: Choice): boolean {
  return CHOICE_RANK[to] < CHOICE_RANK[from]
}

/** The ask-first banner for a move down: Remove, or Write to Read. */
export function LossAsk({ repository, from, to, busy, onAct, onKeep }: { repository: string; from: Choice; to: Choice; busy: boolean; onAct: () => void; onKeep: () => void }) {
  const removing = to === 'none'
  return (
    <AskFirst
      title={removing ? `Remove ${repository}?` : `Make ${repository} read only?`}
      act={removing ? 'Remove access' : 'Change to Read'}
      keep={`Keep ${from === 'write' ? 'Write' : 'Read'}`}
      busy={busy}
      onAct={onAct}
      onKeep={onKeep}
    >
      <p className="ur-small">
        {removing
          ? `SwarmCloud deletes your ${from} grant on ${repository} now, and an agent naming it is refused at submission until you choose it again.`
          : `SwarmCloud stops pushing to ${repository} and opening pull requests there as you; an agent that needs to write to it is refused at submission.`}
      </p>
    </AskFirst>
  )
}

/**
 * A grant drawn as a chooser row, for Chosen when the grant is not on the page
 * GitHub answered. The grant does not carry visibility, branch or
 * registration, so those read as not known rather than as a value.
 */
function grantAsRow(g: AccessGrant): AccessRepository {
  return {
    repository: g.repository,
    owner: g.owner,
    repo: g.repository.split('/')[1] ?? g.repository,
    repo_id: g.repo_id,
    visibility: null,
    archived: g.archived === true,
    default_branch: null,
    can_push: g.can_push,
    registered: false,
    granted: true,
    mode: g.mode,
  }
}

/**
 * CHOSEN IS EVERY GRANT UNDER THE OWNER, NOT THE CHOSEN ROWS OF ONE PAGE (live
 * QA RSA-29, 2026-10-11). It filtered the page GitHub answered, so with 101
 * repositories a grant on page 2 was invisible under Chosen while the footer
 * still read "Page 1 of 2". The overview already carries every grant, so
 * Chosen lists those -- each with the page's richer row when it is there --
 * and drops the pager, which pages GitHub's list, not the grants.
 */
export function RepoChooser({ owner, chosen = [], onChange }: { owner: string; chosen?: AccessGrant[]; onChange: () => void }) {
  const [page, setPage] = useState(1)
  const [typed, setTyped] = useState('')
  const [query, setQuery] = useState('')
  const [only, setOnly] = useState<'all' | 'chosen'>('all')
  const [nonce, setNonce] = useState(0)
  const [busy, setBusy] = useState<string | null>(null)
  const [refused, setRefused] = useState<{ repo: string; error: ApiError } | null>(null)
  /** A move that takes access away, waiting for a yes. Nothing is written before it. */
  const [asking, setAsking] = useState<{ repo: string; from: Choice; to: Choice } | null>(null)
  const [settling, setSettling] = useState(false)
  const read = useUrRead(() => onceMoreOnConflict(() => loadAccessRepositories(owner, page, query), setSettling), `${owner}|${page}|${query}|${nonce}`)
  // AN UNSERVED LIST (QA1-FIX-01 config-03). Search over a list this API does
  // not serve did nothing, and Chosen drew nothing though the owner's card
  // counted its grants. So Search is disabled with its reason, and Chosen is
  // drawn from the grants the page already holds, as an empty first page.
  const unserved = read.state.status === 'error' && notServed(read.state.error)
  // Its time is when the refusing read came back, held across renders: a
  // fresh Date.now() on each one called the list "just read" forever.
  const unservedAt = useMemo(() => Date.now(), [read.state])
  const state: typeof read.state =
    unserved && only === 'chosen'
      ? {
          status: 'ok',
          fetchedAt: unservedAt,
          data: { owner, repositories: [], page: 1, per_page: 100, max_pages: 1, next_page: null, capped: false, q: null, total_count: null, tenant_id: '' },
        }
      : read.state

  async function choose(r: AccessRepository, c: Choice) {
    setAsking(null)
    setBusy(r.repo_id)
    setRefused(null)
    const res = c === 'none' ? await revokeAccessGrant(r.repo_id) : await putAccessGrant(r.repo_id, r.repository, c)
    setBusy(null)
    if (res.status === 'error') {
      setRefused({ repo: r.repo_id, error: res.error })
      return
    }
    setNonce((n) => n + 1)
    onChange()
  }

  function search(e: FormEvent) {
    e.preventDefault()
    setPage(1)
    setQuery(typed.trim())
  }

  return (
    <div className="ac-repos" aria-label={`${owner}'s repositories`} role="region">
      <div className="ac-repos-h">
        <form className="ac-search" role="search" onSubmit={search}>
          <input
            type="search"
            className="ur-search"
            aria-label={`Search ${owner}'s repositories`}
            placeholder={`Search ${owner}`}
            value={typed}
            maxLength={100}
            disabled={unserved}
            onChange={(e) => setTyped(e.target.value)}
          />
          <Button size="sm" type="submit" disabled={unserved}>
            Search
          </Button>
        </form>
        <UrRadio<'all' | 'chosen'>
          label="Show"
          value={only}
          onChange={setOnly}
          options={[
            { key: 'all', label: 'All' },
            { key: 'chosen', label: 'Chosen' },
          ]}
        />
      </div>
      {/* One line, never two: under All the "Not served yet" box below says
          it, so this line is drawn under Chosen only, where no box is. */}
      {unserved && only === 'chosen' && <p className="ur-hint">Search needs the repository list, which this API does not serve yet; Chosen lists your grants.</p>}
      {settling && read.state.status !== 'loading' && (
        <p className="ur-reading-why" role="status">
          {STILL_SETTING_UP}
        </p>
      )}
      <UrRegion
        state={state}
        route="GET /v1/access/orgs/{owner}/repositories"
        what={`${owner}'s repositories`}
        onRetry={read.reload}
        plural
        lines={4}
        reading={settling ? STILL_SETTING_UP : ASKING_GITHUB}
      >
        {(p) => {
          const onPage = new Map(p.repositories.map((r) => [r.repo_id, r]))
          const needle = p.q?.toLowerCase() ?? null
          const shown =
            only === 'all'
              ? p.repositories
              : [...chosen]
                  .filter((g) => needle === null || g.repository.toLowerCase().includes(needle))
                  .sort((a, b) => a.repository.localeCompare(b.repository))
                  .map((g) => onPage.get(g.repo_id) ?? grantAsRow(g))
          const pages = p.total_count !== null ? Math.max(1, Math.ceil(p.total_count / p.per_page)) : null
          return (
            <>
              {shown.length === 0 ? (
                <EmptyState kind="empty" heading={p.q !== null ? `Nothing in ${owner} matches “${p.q}”` : only === 'chosen' ? `Nothing chosen under ${owner}` : 'No repositories on this page'}>
                  {p.q !== null ? 'The search is a part of the name, matched by SwarmCloud.' : 'The installation may cover selected repositories only; add more at GitHub.'}
                </EmptyState>
              ) : (
                <ul className="ac-rows" aria-label={`${owner}'s repositories, page ${p.page}`}>
                  {shown.map((r) => {
                    const choice: Choice = r.granted && r.mode !== null ? r.mode : 'none'
                    const why = writeRefusedWhy(r)
                    return (
                      <li key={r.repo_id} className="ac-row" data-repo={r.repository}>
                        <div className="ac-row-name">
                          <b>{r.repository}</b>
                          <small className="ur-mu">
                            {r.visibility ?? (onPage.has(r.repo_id) ? 'visibility unknown' : 'chosen')}
                            {r.default_branch !== null && ` · default branch ${r.default_branch}`}
                            {r.archived && ' · archived'}
                            {r.registered && ' · registered'}
                          </small>
                        </div>
                        <UrCap label="You can push" cell={pushCell(r)} word={r.can_push === true ? 'can push' : r.can_push === false ? 'read only' : 'push unknown'} />
                        <UrRadio<Choice>
                          label={`Access to ${r.repository}`}
                          value={choice}
                          disabled={busy === r.repo_id}
                          onChange={(c) => {
                            if (c === choice) return
                            if (takesAccessAway(choice, c)) setAsking({ repo: r.repo_id, from: choice, to: c })
                            else void choose(r, c)
                          }}
                          options={[
                            { key: 'none', label: 'Not chosen' },
                            { key: 'read', label: 'Read' },
                            { key: 'write', label: 'Write', disabled: why !== undefined && choice !== 'write', title: why },
                          ]}
                        />
                        {asking !== null && asking.repo === r.repo_id && (
                          <div className="ac-row-err">
                            <LossAsk
                              repository={r.repository}
                              from={asking.from}
                              to={asking.to}
                              busy={busy === r.repo_id}
                              onAct={() => void choose(r, asking.to)}
                              onKeep={() => setAsking(null)}
                            />
                          </div>
                        )}
                        {refused !== null && refused.repo === r.repo_id && (
                          <div className="ac-row-err">
                            <AccessRefusal error={refused.error} title={`${r.repository} was not changed`} />
                          </div>
                        )}
                      </li>
                    )
                  })}
                </ul>
              )}
              {only === 'chosen' ? (
                <div className="ac-pager">
                  <span className="ur-mu">
                    {shown.length} chosen under {owner}
                    {p.q !== null ? ` · matching “${p.q}”` : ''}
                  </span>
                </div>
              ) : (
              <div className="ac-pager">
                <span className="ur-mu">
                  Page {p.page}
                  {pages !== null ? ` of ${pages} · ${p.total_count} repositor${p.total_count === 1 ? 'y' : 'ies'} in the installation` : ''}
                  {p.q !== null ? ` · matching “${p.q}”` : ''}
                </span>
                <span className="ur-acts">
                  <Button size="sm" disabled={p.page <= 1} onClick={() => setPage((n) => Math.max(1, n - 1))}>
                    Previous
                  </Button>
                  <Button size="sm" disabled={p.next_page === null} onClick={() => p.next_page !== null && setPage(p.next_page)}>
                    Next page
                  </Button>
                </span>
              </div>
              )}
              {p.capped && (
                <p className="ur-hint">
                  SwarmCloud reads at most {p.max_pages * p.per_page} of {owner}'s repositories per list. Past that,
                  type the repository below.
                </p>
              )}
            </>
          )
        }}
      </UrRegion>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Not listed? Type owner/repo
// ---------------------------------------------------------------------------

const OWNER_REPO = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\/[A-Za-z0-9._-]{1,100}$/

export function TypedRepository({ tenantId, onChange }: { tenantId: string; onChange: () => void }) {
  const [value, setValue] = useState('')
  const [mode, setMode] = useState<AccessMode>('read')
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const [done, setDone] = useState<string | null>(null)
  const [settling, setSettling] = useState(false)
  const repo = value.trim().replace(/^https:\/\/github\.com\//, '').replace(/\.git$/, '')
  const valid = OWNER_REPO.test(repo)

  // The typed value is cleared only by a grant that landed: a refusal keeps it
  // to correct. A second press while one is pending is dropped.
  async function grant() {
    if (!valid || busy) return
    setBusy(true)
    setRefused(null)
    setDone(null)
    const repoId = await typedRepoId(tenantId, repo)
    const res = await onceMoreOnConflict(() => putAccessGrant(repoId, repo, mode), setSettling)
    setBusy(false)
    if (res.status === 'error') {
      setRefused(res.error)
      return
    }
    setDone(res.status === 'ok' ? `${res.data.grant.repository} granted ${res.data.grant.mode}${res.data.registered ? ', and registered for the workspace' : ''}.` : null)
    setValue('')
    onChange()
  }

  return (
    <div className="ac-typed">
      <form
        className="ac-typed-form"
        onSubmit={(e) => {
          e.preventDefault()
          void grant()
        }}
      >
        <label className="ur-subh" htmlFor="ac-typed-input">
          Not listed? Type owner/repo
        </label>
        <div className="ac-typed-row">
          <input
            id="ac-typed-input"
            className="ur-search"
            placeholder="owner/repo"
            value={value}
            maxLength={141}
            onChange={(e) => setValue(e.target.value)}
            aria-invalid={value.trim() !== '' && !valid ? true : undefined}
          />
          <UrRadio<AccessMode>
            label="Access for the typed repository"
            value={mode}
            onChange={setMode}
            options={[
              { key: 'read', label: 'Read' },
              { key: 'write', label: 'Write' },
            ]}
          />
          <Button size="sm" kind="primary" type="submit" busy={busy ? (settling ? STILL_SETTING_UP : 'Granting…') : false} disabled={!valid}>
            Grant
          </Button>
        </div>
      </form>
      {value.trim() !== '' && !valid && <p className="ur-hint is-warn">Type it as owner/repo, for example example-org/example-api.</p>}
      {refused !== null && <AccessRefusal error={refused} title={`${repo || 'The repository'} was not granted`} onRetry={() => void grant()} />}
      {done !== null && (
        <Banner tone="info" title="Granted" role="status">
          <p className="ur-small">{done}</p>
        </Banner>
      )}
      <p className="ur-hint">SwarmCloud reads it once, as you, and never reaches a repository the App's installation leaves out.</p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Mine: Access A
// ---------------------------------------------------------------------------

/**
 * Chooser A. Its owners read is a GitHub read made with the person's token --
 * one refresh per request (access.py) -- so it is mounted only for an active
 * connection, never fired to be refused.
 */
/** The least time between two focus re-reads: each owners read is one refresh of your token. */
const FOCUS_REREAD_MS = 15_000

/**
 * Re-run `reload` when the person comes back to this tab -- from GitHub's
 * install page, most often. Focus and visibility both fire on one return, so
 * a second within FOCUS_REREAD_MS is dropped.
 */
function useRereadOnFocus(reload: () => void) {
  const last = useRef(0)
  const latest = useRef(reload)
  latest.current = reload
  useEffect(() => {
    const back = () => {
      if (document.visibilityState === 'hidden') return
      const now = Date.now()
      if (now - last.current < FOCUS_REREAD_MS) return
      last.current = now
      latest.current()
    }
    window.addEventListener('focus', back)
    document.addEventListener('visibilitychange', back)
    return () => {
      window.removeEventListener('focus', back)
      document.removeEventListener('visibilitychange', back)
    }
  }, [])
}

/**
 * NOTHING INSTALLED YET. Connecting authorised the App to act as the person;
 * it lists nothing until it is installed on an account or an organisation.
 * Both buttons open the same install page: GitHub asks there which account,
 * and offers only the person's own account and the orgs they may install on
 * or ask for. Back from GitHub, the card's Refresh (or returning to the tab)
 * reads the owners again. The App is named by the name the API serves
 * (`app_name`, the deployment's GITHUB_APP_NAME), never a hard-coded one.
 */
export function NotInstalledNotice({
  login,
  installUrl,
  appName,
}: {
  login: string | null
  installUrl: string | null
  appName?: string | null
}) {
  const app = githubAppName(appName)
  return (
    <section className="ac-install" aria-label={`${app.charAt(0).toUpperCase()}${app.slice(1)} is not installed`}>
      <p className="ac-install-h">
        <b>
          {login !== null ? `You're connected as @${login}` : "You're connected"}, but {app} isn't installed anywhere yet.
        </b>{' '}
        Install it on the accounts and repositories you want SwarmCloud to use.
      </p>
      {isGitHubUrl(installUrl) ? (
        <div className="ur-gh-acts">
          <ButtonLink kind="primary" size="sm" href={installUrl} target="_blank" rel="noreferrer">
            {login !== null ? `Install on ${login}` : 'Install on your account'}
          </ButtonLink>
          <ButtonLink size="sm" href={installUrl} target="_blank" rel="noreferrer">
            Install on an organisation…
          </ButtonLink>
        </div>
      ) : (
        <p className="ur-small">
          <Dash why="The App's install page is not configured on this API" /> This API names no install page for the App; an
          operator sets its slug.
        </p>
      )}
      <p className="ur-hint">
        Organisations you don't see (for example your company's) appear here after the App is installed there; if you're
        not an owner, GitHub sends the owners a request.
      </p>
    </section>
  )
}

/**
 * THE PHONE CHOOSER'S OWNER SURVIVES A RELOAD in this tab's sessionStorage,
 * the way the tenant choice does (fetch.ts `TENANT_PREF`). Not the address:
 * App's `keptKeys` keeps only `view` on GitHub access, so an `?owner=` would
 * be cut off before the page read it, and `view=choose` stays the deep link.
 * Read and written at phone width only: desktop opens on the first enabled
 * owner, as it always has. Sign-out sweeps it with every `swarm.` session key
 * (signoutState.ts, where it is listed as per-user).
 */
export const CHOSEN_OWNER_KEY = 'swarm.access.owner'

function storedOwner(): string | null {
  try {
    return globalThis.sessionStorage?.getItem(CHOSEN_OWNER_KEY) ?? null
  } catch {
    return null
  }
}

function storeOwner(owner: string): void {
  try {
    globalThis.sessionStorage?.setItem(CHOSEN_OWNER_KEY, owner)
  } catch {
    /* storage refused: the pick holds until the page is left, as on desktop */
  }
}

/**
 * THE FOOT BAR (S2c): what this visit saved, and when. Choices save as they
 * are made, so the count is of the grants written here -- a Read, Write or
 * Not chosen in the list, or a typed owner/repo -- each counted once its
 * write answered. Nothing is pending, so there is no Save: the bar carries
 * Refresh, which left the card's head for it, and reads the owners and the
 * overview again.
 */
function SavedFoot({ saved, reading, onRefresh }: { saved: { n: number; at: number } | null; reading: boolean; onRefresh: () => void }) {
  const now = useNow(AGE_TICK_MS)
  return (
    <div className="ac-foot" role="group" aria-label="Changes on this page">
      {/* Polite, not role="status": the chooser's own read already speaks as the card's status. */}
      <span className="ur-mu ac-foot-n" aria-live="polite">
        {saved === null
          ? 'No changes saved yet · choices save as you make them'
          : `${saved.n} ${saved.n === 1 ? 'change' : 'changes'} saved · ${timeAgo(saved.at, now)}`}
      </span>
      <Button size="sm" kind="ghost" busy={reading} onClick={onRefresh}>
        Refresh
      </Button>
    </div>
  )
}

function Choose({ overview, reloadOverview, startOwner = null }: { overview: AccessOverview; reloadOverview: () => void; startOwner?: string | null }) {
  const owners = useUrRead(loadAccessOwners, 'access-owners')
  const phone = usePhoneTables()
  // An owner named by Where the App is installed's Choose opens first.
  const [picked, setPickedState] = useState<string | null>(() => startOwner ?? (phone ? storedOwner() : null))
  const [saved, setSaved] = useState<{ n: number; at: number } | null>(null)
  const noteSaved = () => setSaved((was) => ({ n: (was?.n ?? 0) + 1, at: Date.now() }))
  function setPicked(key: string) {
    setPickedState(key)
    if (phone) storeOwner(key)
  }
  // Owners whose POST /v1/access/orgs answered while GET /v1/access has not
  // been read again yet: enabled, so selectable at once.
  const [landed, setLanded] = useState<string[]>([])
  const listed = overview.orgs.map((o) => o.owner.toLowerCase())
  const enabled = [...listed, ...landed.filter((o) => !listed.includes(o))]
  // A fresh overview answers for itself, including an owner removed since.
  useEffect(() => setLanded([]), [overview])
  const selected = picked !== null && enabled.includes(picked) ? picked : (enabled[0] ?? null)
  const reloadAll = () => {
    reloadOverview()
    owners.reload()
  }
  useRereadOnFocus(reloadAll)
  const login = overview.connection?.forge_login ?? null
  return (
    <Card className="ac-choose" title="Choose repositories" id="ac-choose">
      <p className="ur-hint ac-authz">
        Connecting and installing are two steps. Connect lets SwarmCloud act as you (GitHub › Authorized GitHub Apps);
        installing it on an account or org lets it reach that owner&apos;s repositories (Installed GitHub Apps).
      </p>
      <UrRegion state={owners.state} route="GET /v1/access/orgs" what="The owners your connection reaches" onRetry={owners.reload} plural lines={3} reading={ASKING_GITHUB}>
        {(o) => {
          // On a phone the select may stand on an owner not enabled yet (its
          // card holds Install or Enable), and the repositories shown are that
          // owner's or none: never the first enabled owner's under another's card.
          const keys = o.owners.map((x) => x.owner.toLowerCase())
          const shown = phone ? (picked !== null && keys.includes(picked) ? picked : (selected ?? keys[0] ?? null)) : selected
          const reposOf = phone ? (shown !== null && enabled.includes(shown) ? shown : null) : selected
          const where = phone ? 'above' : 'on the left'
          return (
            <>
              {!o.owners.some((x) => x.install_state === 'installed') && (
                <NotInstalledNotice
                  login={login ?? o.owners.find((x) => x.owner_type === 'User')?.owner ?? null}
                  installUrl={o.install_url}
                  appName={o.app_name}
                />
              )}
              <div className="ac-chooser">
                <Owners
                  owners={o.owners}
                  orgsListed={o.orgs_listed}
                  grants={overview.grants}
                  selected={shown}
                  phone={phone}
                  onSelect={setPicked}
                  onEnabled={(key) => {
                    setLanded((was) => (was.includes(key) ? was : [...was, key]))
                    setPicked(key)
                  }}
                  onChange={reloadAll}
                />
                {reposOf !== null ? (
                  <RepoChooser
                    key={reposOf}
                    owner={reposOf}
                    chosen={overview.grants.filter((g) => g.owner.toLowerCase() === reposOf.toLowerCase())}
                    onChange={() => {
                      noteSaved()
                      reloadOverview()
                    }}
                  />
                ) : phone && enabled.length > 0 ? (
                  <EmptyState kind="empty" heading={`${o.owners.find((x) => x.owner.toLowerCase() === shown)?.owner ?? 'This owner'} is not enabled`}>
                    SwarmCloud lists its repositories once the App is installed there and you have enabled it; its card above
                    says which step is left. Pick an enabled owner to choose from theirs.
                  </EmptyState>
                ) : o.owners.some((x) => x.install_state === 'installed') ? (
                  <EmptyState kind="empty" heading="No owner enabled yet">
                    Enable an owner {where}: SwarmCloud lists its repositories once the App is installed there.
                  </EmptyState>
                ) : (
                  <EmptyState kind="empty" heading="Nothing to list until the App is installed">
                    Install {githubAppName(o.app_name)} on an owner {where}; its repositories are listed here once it is, and
                    you have enabled it.
                  </EmptyState>
                )}
              </div>
            </>
          )
        }}
      </UrRegion>
      <TypedRepository
        tenantId={overview.tenant_id}
        onChange={() => {
          noteSaved()
          reloadAll()
        }}
      />
      <SavedFoot saved={saved} reading={owners.state.status === 'loading'} onRefresh={reloadAll} />
    </Card>
  )
}

// ---------------------------------------------------------------------------
// GitHub quota (QUOTA-THROTTLE, #1483)
// ---------------------------------------------------------------------------

/**
 * One credential's GitHub rate limit as swarm-api last read it
 * (`GET /v1/access` `github_quota`, routes/access.py): who it is, the counts
 * and the reset. No token, no secret. `low` is the server's: under the
 * threshold at which background reads wait (`forge_quota_defer_percent`), so
 * the page never restates the percentage.
 */
export interface GitHubQuota {
  kind: 'connection' | 'tenant' | 'installation'
  name: string
  limit: number
  remaining: number
  reset_at: string | null
  resource: string
  observed_at: string
  low: boolean
}

/** Who a quota line is for, in the words a person reads. */
function quotaFor(q: GitHubQuota): string {
  if (q.kind === 'connection') return 'you via SwarmCloud'
  if (q.kind === 'tenant') return `the ${q.name} workspace token`
  return `the SwarmCloud App on ${q.name}`
}

function resetsAt(iso: string): string {
  const at = new Date(iso)
  const hh = String(at.getUTCHours()).padStart(2, '0')
  const mm = String(at.getUTCMinutes()).padStart(2, '0')
  return `${hh}:${mm} UTC`
}

/**
 * WHY THIS IS ON THE PAGE (#1483, 2026-10-17). A person's 5,000 calls an hour
 * were spent by background reads, and their push test then read as a missing
 * permission. One line per credential says how much is left and when it
 * resets; amber while it is low, which is when background reads wait and the
 * rest is kept for what the person does. A credential nothing has read yet is
 * not drawn: unknown is not full.
 */
function GitHubQuotaLines({ quota }: { quota: GitHubQuota[] }) {
  if (quota.length === 0) return null
  return (
    <ul className="ur-small ac-quota" aria-label="GitHub quota">
      {quota.map((q) => (
        <li key={`${q.kind}:${q.name}:${q.resource}`} data-low={q.low ? 'true' : undefined}>
          <ToneMark tone={q.low ? 'warn' : 'ok'} />
          <span>
            GitHub quota for {quotaFor(q)}: <b>{q.remaining.toLocaleString('en-US')}</b> /{' '}
            {q.limit.toLocaleString('en-US')}
            {q.reset_at !== null ? ` · resets ${resetsAt(q.reset_at)}` : ''}
            {q.low ? ' · background reads wait until then' : ''}
          </span>
        </li>
      ))}
    </ul>
  )
}

/**
 * A lead, on the info tint as S2 draws it: `.ac-lead`, the one Config lead
 * style (the tint in styles/config.css; its buttons' borders, which clear 3:1
 * on the tint, in styles.css after the button floor). CONFIG-LEAD-TINT;
 * measured in contrast.config-lead.test.tsx.
 *
 * S2B'S SHAPE (VQ-038 minor, GITHUB-ACCESS-S2B): the kicker over the title
 * over the sentence on the left, the one button at the right, which wraps
 * under the words when the row is too narrow for both.
 */
function Lead({ kicker, title, actions, children }: { kicker?: string; title: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="ac-lead" aria-label="Next step">
      <Card className="ac-lead-c">
        <div className="ac-lead-t">
          {kicker !== undefined && <b className="ac-lead-k">{kicker}</b>}
          <h2>{title}</h2>
          <p className="ur-small">{children}</p>
        </div>
        {actions !== undefined && <div className="ac-lead-a">{actions}</div>}
      </Card>
    </section>
  )
}

/**
 * A later step's action, as the lead titles it and its one button names its
 * page: the words the strip's next step reads as from GitHub access.
 */
const LATER: Readonly<Record<string, { title: string; button: string }>> = {
  secrets: { title: 'Set the secrets your repositories need', button: 'Next: Secrets' },
  people: { title: 'Invite the people who work here', button: 'Next: People and roles' },
}

/**
 * THE ONE NEXT ACTION THAT LEADS THE PAGE (proposal E: "one next-action
 * sentence and button leading each page").
 *
 * THE STEP IS THE STRIP'S (VQ-037, 2026-10-26). The lead built "Step 2 of 3 ·
 * in progress" from this page's own grant count while the strip right above
 * it, fed by the setup read (`useSetupSteps`), marked step 2 done and step 3
 * next, and the Choose tab's button said "Next: Repositories". Now the step,
 * its word and the button are read off the same steps the strip draws:
 *   * GitHub not connected or not active: step 1, from this page's own
 *     connection read, which is the fresher of the two for its own step;
 *   * the strip's Repositories chosen not done: step 2, "next" or "in
 *     progress" as the strip says it (no word while the read is unmeasured);
 *   * done: the strip's next step, by its number, titled by its action, with
 *     its page as the one button; with nothing next, setup is complete.
 * The page's count is only in the sentence, as what this read shows.
 */
function GitHubLead({ view, overview, open }: { view: 'mine' | 'choose'; overview: AccessOverview; open: (v: View) => void }) {
  const steps = useSetupSteps()
  const of = steps.length
  const c = overview.connection
  const chosen = overview.grants.length
  const login = c?.forge_login != null ? `@${c.forge_login}` : null
  const lead = (kicker: string, title: string, sentence: ReactNode, actions?: ReactNode) => (
    <Lead kicker={kicker} title={title} actions={actions}>
      {sentence}
    </Lead>
  )
  if (c === null || c.state === 'revoked') {
    return lead(
      `Step 1 of ${of} · start here.`,
      'Connect GitHub to start',
      'SwarmCloud lists the orgs and repositories you reach, and clones, pushes and opens pull requests as you. Choosing repositories waits for this one step.',
      <ConnectButton />,
    )
  }
  if (c.state !== 'active') {
    return lead(
      `Step 1 of ${of} · needs you.`,
      'Reconnect GitHub',
      `This connection is ${c.state ?? 'not active'}, so SwarmCloud cannot act as you until you reconnect.`,
      <ConnectButton label="Reconnect" />,
    )
  }
  const counted = `${chosen} ${chosen === 1 ? 'repository is' : 'repositories are'} chosen`
  const as = `GitHub is connected${login !== null ? ` as ${login}` : ''}`
  const chooseButton = (
    <Button kind="primary" size="sm" onClick={() => open('choose')}>
      Choose repositories
    </Button>
  )
  // config-08: a checklist step done on this page failed or went stale (the
  // strip's step 1 says which, e.g. "Enable orgs failed"), so this page is not
  // set up, whatever the grant count says.
  const github = steps.find((s) => s.key === 'github') ?? null
  if (github !== null && (github.state === 'failed' || github.state === 'stale')) {
    return lead(
      `Step ${steps.indexOf(github) + 1} of ${of} · needs you.`,
      github.line,
      `${as}, but Workspace setup's checklist says ${github.line}; its fix is on this page. Workspace setup names what to do.`,
      <ButtonLink kind="primary" size="sm" href={addressToPath(WORKSPACE_SETUP_ADDRESS)}>
        Open Workspace setup
      </ButtonLink>,
    )
  }
  const repos = steps.find((s) => s.key === 'repositories') ?? null
  if (repos === null || repos.state !== 'done') {
    const word = repos === null || repos.state === 'unknown' ? '' : repos.state === 'in_progress' ? ' · in progress' : ' · next'
    const kicker = `Step ${repos === null ? 2 : steps.indexOf(repos) + 1} of ${of}${word}.`
    if (view === 'choose') {
      return lead(
        kicker,
        'Choose the repositories SwarmCloud may reach',
        `${chosen === 0 ? 'Nothing is chosen yet.' : `${counted}.`} Choices save as you make them; Config › Repositories registers them and verifies each.`,
        chosen === 0 ? undefined : (
          <ButtonLink kind="primary" size="sm" href={addressToPath(REPOSITORIES_ADDRESS)}>
            Next: Repositories
          </ButtonLink>
        ),
      )
    }
    return lead(
      kicker,
      'Choose the repositories SwarmCloud may reach',
      chosen === 0 ? `${as}. Pick Read or Write for each repository.` : `${as} and ${counted}. Add more, or register them on Config › Repositories.`,
      chooseButton,
    )
  }
  const next = setupNext(steps)
  const later = next === null ? undefined : LATER[next.key]
  if (next === null || later === undefined) {
    const { done } = setupCount(steps)
    return lead(
      done === of ? `Setup complete · ${done} of ${of}.` : `Step ${steps.indexOf(repos) + 1} of ${of} · done.`,
      'GitHub access is set up',
      `${as} and ${counted}. Choose more at any time; Config › Repositories lists and verifies them.`,
      view === 'choose' ? (
        <ButtonLink kind="primary" size="sm" href={addressToPath(REPOSITORIES_ADDRESS)}>
          Open Repositories
        </ButtonLink>
      ) : (
        chooseButton
      ),
    )
  }
  return lead(
    `Step ${steps.indexOf(next) + 1} of ${of} · next.`,
    later.title,
    `${as} and ${counted}, so this page is done. Next: ${next.label}, ${next.line}.`,
    <ButtonLink kind="primary" size="sm" href={addressToPath(next.to)}>
      {later.button}
    </ButtonLink>,
  )
}

/** Opens a tab; Choose repositories may be opened on one owner. */
type Open = (v: View, owner?: string) => void

/**
 * WHAT COMES AFTER (S2a): with GitHub not connected, the setup steps after
 * this one, each with what it waits for, so the pages they live on read as
 * waiting rather than broken. The steps are the setup strip's own
 * (`useSetupSteps`): a personal workspace has no Invite people, so it is not
 * listed there. Nothing here is a button: each step's page says what to do
 * once its wait is over.
 */
const AFTER: Readonly<Record<string, { label: string; waits: string }>> = {
  repositories: { label: 'Choose repositories', waits: 'Waits for GitHub: there is nothing to list until you connect and install the App.' },
  secrets: { label: 'Set secrets', waits: 'Waits for repositories: what is needed depends on what you register.' },
  people: { label: 'Invite people', waits: 'Any time after GitHub.' },
}

function WhatComesAfter() {
  const steps = useSetupSteps()
  return (
    <Card className="ac-after is-dash" title="What comes after">
      <ol className="ac-steps">
        {steps.map((s, i) => {
          const after = AFTER[s.key]
          if (after === undefined) return null
          return (
            <li key={s.key} className="ac-step">
              <span className="ac-step-m" aria-hidden="true" />
              <span className="ac-step-t">
                <b>
                  {i + 1}. {after.label}
                </b>{' '}
                <small>{after.waits}</small>
              </span>
            </li>
          )
        })}
      </ol>
    </Card>
  )
}

/**
 * WHERE THE APP IS INSTALLED (S2b), on the Connection tab: every owner the
 * connection reaches, whether the App is installed there and how, and how
 * many repositories are chosen under it, from the same owners read Choose
 * repositories makes (`GET /v1/access/orgs`). That read refreshes the
 * person's token (access.py), so it is mounted only for an active connection,
 * and read again on return from GitHub's install page, as the chooser is.
 *
 * Each row's one action is the step it is missing: Choose for an owner with
 * the App (Choose repositories opens on it, where Enable is if it is not yet
 * enabled), Re-check for a request GitHub has not answered, Request install
 * for an organisation without the App -- GitHub's page and the record, as the
 * chooser's owner card does it -- and Install for the person's own account.
 * "Install on an organisation…" opens the App's install page, where GitHub
 * asks which account.
 */
function Installs({ grants, open }: { grants: AccessGrant[]; open: Open }) {
  const owners = useUrRead(loadAccessOwners, 'access-owners')
  useRereadOnFocus(owners.reload)
  const [busy, setBusy] = useState<string | null>(null)
  const [refused, setRefused] = useState<{ owner: string; error: ApiError } | null>(null)
  const installUrl = dataOf(owners.state)?.install_url ?? null

  async function request(o: AccessOwner) {
    if (busy !== null) return
    setBusy(o.owner)
    setRefused(null)
    const res = await askToInstall(o)
    setBusy(null)
    if (res.status === 'error') {
      setRefused({ owner: o.owner, error: res.error })
      return
    }
    owners.reload()
  }

  const chosenUnder = (o: AccessOwner) => grants.filter((g) => g.owner.toLowerCase() === o.owner.toLowerCase()).length
  return (
    <Card
      className="ac-installs"
      title="Where the App is installed"
      action={
        isGitHubUrl(installUrl) ? (
          // ANOTHER organisation (visual QA pass 2 config2-22): every owner
          // listed has its own row's button; this one is for an owner the list
          // does not name yet.
          <ButtonLink size="sm" href={installUrl} target="_blank" rel="noreferrer">
            Install on another organisation…
          </ButtonLink>
        ) : undefined
      }
    >
      <UrRegion state={owners.state} route="GET /v1/access/orgs" what="The owners your connection reaches" onRetry={owners.reload} plural lines={3} reading={ASKING_GITHUB}>
        {(d) =>
          d.owners.length === 0 ? (
            <EmptyState kind="empty" heading="No owner listed">
              GitHub listed no account or organisation for your connection. Install {githubAppName(d.app_name)} on one, then
              Refresh.
            </EmptyState>
          ) : (
            <table role="table" className="mcp-servers ac-installs-t" aria-label="Where the App is installed">
              <thead>
                <tr>
                  <th scope="col">Owner</th>
                  {/* "Installed", not "Install" (QA1-FIX-01 config-07): stacked at
                      390, the label sat above an Install button meaning the other thing. */}
                  <th scope="col">Installed</th>
                  <th scope="col">Chosen</th>
                  <th scope="col">
                    <span className="ac-vh">Action</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {d.owners.map((o) => {
                  const reaches = o.method === 'pat' || o.install_state === 'installed'
                  return (
                    <Fragment key={o.owner.toLowerCase()}>
                    <tr data-owner={o.owner}>
                      <th scope="row" data-label="Owner">
                        <b>{o.owner}</b> <small className="ur-mu ac-installs-k">{ownerKind(o)}</small>
                      </th>
                      <td data-label="Installed">
                        {o.method === 'pat' ? (
                          'via token'
                        ) : o.install_state === 'installed' ? (
                          o.repository_selection === 'all' ? 'all repositories' : 'selected repositories'
                        ) : (
                          <ToneMark tone="warn">{o.install_state === 'requested' ? 'install requested' : 'not installed'}</ToneMark>
                        )}
                        {o.sso === 'required' && <small className="ur-mu"> · SSO not authorised</small>}
                      </td>
                      <td data-label="Chosen">
                        {reaches ? (
                          chosenUnder(o)
                        ) : (
                          <>
                            <Dash why="Nothing to list until the App is installed" />{' '}
                            <small className="ur-mu">nothing to list until it is</small>
                          </>
                        )}
                      </td>
                      <td className="ac-installs-a">
                        {reaches ? (
                          <Button size="sm" onClick={() => open('choose', o.owner)}>
                            Choose
                          </Button>
                        ) : o.install_state === 'requested' ? (
                          <Button size="sm" disabled={busy !== null} onClick={owners.reload}>
                            Re-check
                          </Button>
                        ) : !isGitHubUrl(o.install_url) ? (
                          <Dash why="The App's install page is not configured on this API" />
                        ) : o.owner_type === 'Organization' ? (
                          <Button
                            size="sm"
                            busy={busy === o.owner}
                            disabled={busy !== null && busy !== o.owner}
                            title={`Not an owner of ${o.owner}? GitHub lets you ask its owners to install the App`}
                            onClick={() => void request(o)}
                          >
                            Request install
                          </Button>
                        ) : (
                          // Says whose (config2-22): a bare `Install` on the
                          // person's own row did not say it was their account.
                          <ButtonLink size="sm" href={o.install_url} target="_blank" rel="noreferrer">
                            Install on your account
                          </ButtonLink>
                        )}
                      </td>
                    </tr>
                    {/* A refusal is a full-width row under its owner (QA1-FIX-01
                        config-02): inside the Action cell it squeezed the Owner
                        column to a word per line. */}
                    {refused !== null && refused.owner === o.owner && (
                      <tr className="ac-installs-err" data-refused={o.owner}>
                        <td colSpan={4}>
                          <AccessRefusal error={refused.error} title={`${o.owner} was not changed`} onRetry={owners.reload} />
                        </td>
                      </tr>
                    )}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          )
        }
      </UrRegion>
    </Card>
  )
}

/** The Connection tab: the lead, then the connection (with the GitHub quota) and what comes after or where the App is installed beside the rules (S2a, S2b). */
function Mine({ overview, reloadOverview, open }: { overview: AccessOverview; reloadOverview: () => void; open: Open }) {
  // `github_quota` is read here rather than added to types.ts's
  // AccessOverview: older servers do not send it, and absent is no lines.
  const quota = (overview as AccessOverview & { github_quota?: GitHubQuota[] }).github_quota ?? []
  const c = overview.connection
  return (
    <>
      <GitHubLead view="mine" overview={overview} open={open} />
      {/* S2B'S TWO COLUMNS (VQ-038): the GitHub card, its quota lines inside
          it, and what comes after or where the App is installed on the left;
          the rules checklist on the right. One column on a phone. */}
      <div className="ac-body">
        <div className="ac-main">
          <Connection connection={c} quota={quota} onChange={reloadOverview} />
          {c === null || c.state === 'revoked' ? (
            <WhatComesAfter />
          ) : (
            c.state === 'active' && <Installs grants={overview.grants} open={open} />
          )}
        </div>
        <AccessPolicy />
      </div>
    </>
  )
}

/**
 * The Choose repositories tab (S2c, S2f). Its owners read is a GitHub read
 * made with the person's token, so with no active connection the chooser is
 * not mounted to be refused: the tab says to connect first, and opens
 * Connection.
 */
function ChooseTab({ overview, reloadOverview, open, startOwner }: { overview: AccessOverview; reloadOverview: () => void; open: Open; startOwner: string | null }) {
  const connected = overview.connection !== null && overview.connection.state === 'active'
  if (!connected) {
    return (
      <Lead
        title="Connect GitHub first"
        actions={
          <Button kind="primary" size="sm" onClick={() => open('mine')}>
            Open Connection
          </Button>
        }
      >
        There is nothing to choose from until GitHub is connected and the App is installed on an owner.
      </Lead>
    )
  }
  return (
    <>
      <GitHubLead view="choose" overview={overview} open={open} />
      <Choose overview={overview} reloadOverview={reloadOverview} startOwner={startOwner} />
    </>
  )
}

/**
 * THE LEDE SAYS WHAT THE TAB SHOWS (visual QA V232). Since GITHUB-ACCESS-PAGE
 * the reach-as-you list is on Config › Repositories, so the two tabs say what
 * they hold instead.
 */
function accessLede(view: View, tenant: string | null): string {
  const inTenant = tenant !== null ? ` in workspace ${tenant}` : ''
  if (view === 'choose') {
    return `The owners the GitHub App is installed on${inTenant}, and the repositories you choose from them, each Read or Write.`
  }
  return `How SwarmCloud acts as you on GitHub${inTenant}: your connection through the GitHub App, and what it may do.`
}

/** Config › GitHub access (Work › Access until CONFIG-NAV). */
export function AccessScreen() {
  const overview = useUrRead(loadAccess, 'access')
  const [view, setViewState] = useState<View>(viewFromUrl)
  // The owner Where the App is installed's Choose named, for the chooser to open on.
  const [startOwner, setStartOwner] = useState<string | null>(null)
  function setView(v: View) {
    setViewState(v)
    writeViewToUrl(v)
  }
  const open: Open = (v, owner) => {
    setStartOwner(owner !== undefined ? owner.toLowerCase() : null)
    setView(v)
  }
  const tenant = dataOf(overview.state)?.tenant_id ?? null
  return (
    <div className="ur-page ac-page">
      {/* Refresh alone: S2 draws the page's two tabs and no switch. Setup left
          the head with GITHUB-ACCESS-PAGE (the setup strip over every Config
          page links Workspace setup); Members and Browser logins left it with
          GA-MOVE-VIEWS for People and roles and Secrets. */}
      <PageHead title="GitHub access">
        <UrRefresh reads={[overview]} />
      </PageHead>
      {/* `cfg-parts` (config.css, imported with the setup strip) is the Config
          pages' tab strip layout: never shrunk, never a vertical scroller --
          the underline's -1px margin made a bare strip draw a scroll arrow. */}
      <Tabs tabs={GITHUB_TABS} current={view} label="GitHub access" className="cfg-parts" onSelect={(k) => open(k === 'choose' ? 'choose' : 'mine')} />
      <p className="ur-sub">{accessLede(view, tenant)}</p>
      <UrRegion state={overview.state} route="GET /v1/access" what="Your access" onRetry={overview.reload} lines={4}>
        {(o) =>
          view === 'choose' ? (
            <ChooseTab overview={o} reloadOverview={overview.reload} open={open} startOwner={startOwner} />
          ) : (
            <Mine overview={o} reloadOverview={overview.reload} open={open} />
          )
        }
      </UrRegion>
    </div>
  )
}
