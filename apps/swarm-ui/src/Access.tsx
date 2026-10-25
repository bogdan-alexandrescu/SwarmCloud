import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import {
  disableAccessOwner, disconnectGitHub, enableAccessOwner, githubAppName, loadAccess, loadAccessMembers, loadAccessOwners,
  loadAccessRepositories, putAccessGrant, requestAccessInstall, revokeAccessGrant, typedRepoId,
} from './api'
import { BrowserLoginsPane } from './BrowserLogins'
import { Banner, Button, ButtonLink, Card, Chip, Dash, EmptyState, Tabs, ToneMark, type TabDef } from './components'
import { useSetupSteps } from './ConfigSetup'
import type { ApiError, Result } from './fetch'
import { ConnectButton, Refusal } from './GitHubConnect'
import { addressToPath } from './paths'
import { UrCap, UrRadio, UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import type { CapCell } from './RepositoriesData'
import { FailedPanel, PageHead } from './Shell'
import { usePhoneTables } from './capacityPoll'
import type {
  AccessDisableResponse, AccessGrant, AccessMember, AccessMode, AccessOverview, AccessOwner, AccessRepository, GitHubConnection,
} from './types'
import { humaniseUntil, timeAgo } from './types'
import { AGE_TICK_MS, useNow } from './useNow'
import './styles/repositories.css'
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

type View = 'mine' | 'choose' | 'members' | 'logins'

/**
 * CONNECTION AND CHOOSE REPOSITORIES ARE TWO TABS OF MINE (GITHUB-ACCESS-PAGE;
 * part of #2157, proposal E, config-e.html S2a-S2f). The page is the GitHub App
 * integration -- connect, disconnect, who you are on GitHub -- and the chooser
 * where repositories are added, and nothing else: "Repositories SwarmCloud may
 * reach as you" is Config › Repositories' (ReachableRepositories.tsx). Mine is
 * the Connection tab, at the bare path; Choose repositories is `?view=choose`.
 * Members and Browser logins stay views of the page, on the switch beside.
 */
const GITHUB_TABS: readonly TabDef[] = [
  { key: 'mine', label: 'Connection' },
  { key: 'choose', label: 'Choose repositories' },
]

/**
 * WHERE A NEW REPOSITORY IS ADDED: Choose repositories, opened directly.
 * Config › Repositories' "Add new repository" links here. It rides on `view`
 * because `view` is the one query key App's `keptKeys` keeps on GitHub
 * access; an `?add=1` would be cut off the address before the page read it.
 */
export const ADD_REPOSITORY_ADDRESS = 'config/github?view=choose'

/** Config › Repositories, where the reach-as-you list is (CONFIG-NAV made the tab). */
const REPOSITORIES_ADDRESS = 'config/repositories'

/**
 * WHOSE ACCESS IS IN THE ADDRESS (live QA RSA-28, 2026-10-11). Mine / Members
 * was component state, so a reload or a shared link returned to Mine. It is
 * `?view=members` now; Mine is the bare path.
 *
 * BROWSER LOGINS (LB-7, docs/design/live-browser.md §5.5.1) are a third view,
 * `?view=logins`: the person's own saved live-browser sessions, by name, with
 * a typed Revoke (BrowserLogins.tsx). A view of Access rather than a nav tab,
 * because the design adds no tab (§6), so the issue forms' "Where" list
 * stays as it is.
 */
function viewFromUrl(): View {
  if (typeof window === 'undefined') return 'mine'
  const v = new URLSearchParams(window.location.search).get('view')
  return v === 'choose' || v === 'members' || v === 'logins' ? v : 'mine'
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

/** The owner's three rules: each one's headline, and the sentences under it. */
const POLICY_RULES: ReadonlyArray<{ rule: string; head: string; body: ReactNode }> = [
  {
    rule: 'D9',
    head: 'Read means read, and SwarmCloud enforces it.',
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
 * the first screen of every visit. Wider screens draw the whole list.
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
        <ul className="ac-rules">
          {POLICY_RULES.map((r) => (
            <li key={r.rule} data-rule={r.rule}>
              <b>{r.head}</b> {r.body}
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

function Connection({ connection, onChange }: { connection: GitHubConnection | null; onChange: () => void }) {
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const [said, setSaid] = useState<string | null>(null)
  const now = useNow(AGE_TICK_MS)

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
              <ToneMark tone={active ? 'ok' : 'bad'}>{active ? 'active' : (connection.state ?? 'unknown')}</ToneMark>
            </span>
          </p>
          <p className="ur-cmeta">
            <span>
              Token {connection.access_expires_at !== null ? <b>{tokenExpiry(connection.access_expires_at, now)}</b> : <Dash why="No expiry recorded" />}
            </span>
            <span>renewed automatically</span>
            {connection.refreshed_at !== null && <span>last renewed {timeAgo(connection.refreshed_at)}</span>}
          </p>
          {!active && (
            <p className="ur-small ur-bad">
              This connection is {connection.state ?? 'not active'}
              {connection.failure !== null ? ` (${connection.failure})` : ''}, so SwarmCloud cannot act as you until you
              reconnect: Reconnect is above. Config › Workspace Settings › Workspace setup carries the recovery steps.
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
            <div className="ur-gh-acts">
              <Button kind="danger" size="sm" onClick={() => setConfirming(true)}>
                Disconnect GitHub
              </Button>
            </div>
          )}
        </>
      )}
      {refused !== null && <Refusal error={refused} title="GitHub was not disconnected" />}
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Owners: chooser A's left column
// ---------------------------------------------------------------------------

function ownerWords(o: AccessOwner): string {
  const kind = o.owner_type === 'User' ? 'your account' : 'organisation'
  if (o.method === 'pat') return `${kind} · via token`
  if (o.install_state === 'requested') return `${kind} · install requested`
  if (o.install_state !== 'installed') return `${kind} · not installed`
  return `${kind} · ${o.repository_selection === 'all' ? 'all repositories' : 'selected repositories'}`
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

  // GitHub decides on its own page whether this person installs or requests;
  // the page opens first, in the click, so no popup blocker stands between
  // them, and the request is recorded beside it. An owner already enabled
  // answers `recorded: false` and is drawn as it is on the re-read.
  async function request(o: AccessOwner) {
    if (busy !== null) return
    if (isGitHubUrl(o.install_url)) window.open(o.install_url, '_blank', 'noopener,noreferrer')
    setBusy(o.owner)
    setRefused(null)
    const res = await requestAccessInstall(o.owner)
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
                    <Button size="sm" kind="ghost" onClick={() => setRemoving(key)}>
                      Remove
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
                ) : isGitHubUrl(o.install_url) ? (
                  <>
                    <ButtonLink size="sm" kind="primary" href={o.install_url} target="_blank" rel="noreferrer">
                      Install on {o.owner}
                    </ButtonLink>
                    {o.owner_type === 'Organization' && (
                      <Button
                        size="sm"
                        busy={busy === o.owner}
                        disabled={busy !== null && busy !== o.owner}
                        title={`Not an owner of ${o.owner}? GitHub lets you ask its owners to install the App`}
                        onClick={() => void request(o)}
                      >
                        Request install
                      </Button>
                    )}
                  </>
                ) : (
                  <Dash why="The App's install page is not configured on this API" />
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
            onChange={(e) => setTyped(e.target.value)}
          />
          <Button size="sm" type="submit">
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
      {settling && read.state.status !== 'loading' && (
        <p className="ur-reading-why" role="status">
          {STILL_SETTING_UP}
        </p>
      )}
      <UrRegion
        state={read.state}
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
    setDone(res.status === 'ok' ? `${res.data.grant.repository} granted ${res.data.grant.mode}${res.data.registered ? ', and registered for the tenant' : ''}.` : null)
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

function Choose({ overview, reloadOverview }: { overview: AccessOverview; reloadOverview: () => void }) {
  const owners = useUrRead(loadAccessOwners, 'access-owners')
  const phone = usePhoneTables()
  const [picked, setPickedState] = useState<string | null>(() => (phone ? storedOwner() : null))
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
    <Card
      className="ac-choose"
      title="Choose repositories"
      id="ac-choose"
      action={
        <Button size="sm" kind="ghost" busy={owners.state.status === 'loading'} onClick={reloadAll}>
          Refresh
        </Button>
      }
    >
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
                    onChange={reloadOverview}
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
      <TypedRepository tenantId={overview.tenant_id} onChange={reloadAll} />
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
    <ul className="ur-small" aria-label="GitHub quota">
      {quota.map((q) => (
        <li key={`${q.kind}:${q.name}:${q.resource}`} data-low={q.low ? 'true' : undefined}>
          <ToneMark tone={q.low ? 'warn' : 'ok'} />
          GitHub quota for {quotaFor(q)}: <b>{q.remaining.toLocaleString('en-US')}</b> /{' '}
          {q.limit.toLocaleString('en-US')}
          {q.reset_at !== null ? ` · resets ${resetsAt(q.reset_at)}` : ''}
          {q.low ? ' · background reads wait until then' : ''}
        </li>
      ))}
    </ul>
  )
}

/**
 * A lead, drawn on the card surface. S2 tints it with the info colour, but a
 * button on the info tint draws its border at 2.89:1 in dark, under the 3:1
 * the spacing sweep holds every control to (spacing.setup.test.tsx), and the
 * fix is a stylesheet rule outside this page's file -- asked in the PR.
 */
function Lead({ kicker, title, actions, children }: { kicker?: string; title: string; actions: ReactNode; children: ReactNode }) {
  return (
    <section className="ac-lead" aria-label="Next step">
      <Card title={title}>
        <p className="ur-small">
          {kicker !== undefined && <b className="ur-mu">{kicker}</b>} {children}
        </p>
        <div className="ur-gh-acts">{actions}</div>
      </Card>
    </section>
  )
}

/**
 * THE ONE NEXT ACTION THAT LEADS THE PAGE (proposal E: "one next-action
 * sentence and button leading each page"). It is read off this page's own
 * `GET /v1/access` -- the connection's state and how many repositories are
 * chosen -- so it never says a step is left that the read says is done. The
 * step's number is its place in the setup strip above (three steps in a
 * personal workspace, four in a group one).
 */
function GitHubLead({ view, overview, open }: { view: 'mine' | 'choose'; overview: AccessOverview; open: (v: View) => void }) {
  const steps = useSetupSteps().length
  const c = overview.connection
  const chosen = overview.grants.length
  const login = c?.forge_login != null ? `@${c.forge_login}` : null
  const lead = (kicker: string, title: string, sentence: ReactNode, actions: ReactNode) => (
    <Lead kicker={kicker} title={title} actions={actions}>
      {sentence}
    </Lead>
  )
  if (c === null || c.state === 'revoked') {
    return lead(
      `Step 1 of ${steps} · start here.`,
      'Connect GitHub to start',
      'SwarmCloud lists the orgs and repositories you reach, and clones, pushes and opens pull requests as you. Choosing repositories waits for this one step.',
      <ConnectButton />,
    )
  }
  if (c.state !== 'active') {
    return lead(
      `Step 1 of ${steps} · needs you.`,
      'Reconnect GitHub',
      `This connection is ${c.state ?? 'not active'}, so SwarmCloud cannot act as you until you reconnect.`,
      <ConnectButton label="Reconnect" />,
    )
  }
  const counted = `${chosen} ${chosen === 1 ? 'repository' : 'repositories'} chosen`
  const as = `GitHub is connected${login !== null ? ` as ${login}` : ''}.`
  if (view === 'choose') {
    return lead(
      `Step 2 of ${steps} · ${chosen === 0 ? 'next' : 'in progress'}.`,
      chosen === 0 ? 'Nothing chosen yet' : counted,
      'Choices save as you make them. Config › Repositories lists every repository SwarmCloud may reach as you, and verifies each.',
      <ButtonLink kind="primary" size="sm" href={addressToPath(REPOSITORIES_ADDRESS)}>
        Next: Repositories
      </ButtonLink>,
    )
  }
  return lead(
    `Step 2 of ${steps} · ${chosen === 0 ? 'next' : 'in progress'}.`,
    chosen === 0 ? 'Choose the repositories SwarmCloud may reach' : counted,
    chosen === 0 ? `${as} Pick Read or Write for each repository.` : `${as} Add more on Choose repositories; Config › Repositories lists and verifies them.`,
    <>
      <Button kind="primary" size="sm" onClick={() => open('choose')}>
        Choose repositories
      </Button>
      {chosen > 0 && (
        <ButtonLink size="sm" href={addressToPath(REPOSITORIES_ADDRESS)}>
          Repositories
        </ButtonLink>
      )}
    </>,
  )
}

/** The Connection tab: the lead, the connection, the GitHub quota and the rules (S2a, S2b). */
function Mine({ overview, reloadOverview, open }: { overview: AccessOverview; reloadOverview: () => void; open: (v: View) => void }) {
  // `github_quota` is read here rather than added to types.ts's
  // AccessOverview: older servers do not send it, and absent is no lines.
  const quota = (overview as AccessOverview & { github_quota?: GitHubQuota[] }).github_quota ?? []
  return (
    <>
      <GitHubLead view="mine" overview={overview} open={open} />
      <Connection connection={overview.connection} onChange={reloadOverview} />
      <GitHubQuotaLines quota={quota} />
      <AccessPolicy />
    </>
  )
}

/**
 * The Choose repositories tab (S2c, S2f). Its owners read is a GitHub read
 * made with the person's token, so with no active connection the chooser is
 * not mounted to be refused: the tab says to connect first, and opens
 * Connection.
 */
function ChooseTab({ overview, reloadOverview, open }: { overview: AccessOverview; reloadOverview: () => void; open: (v: View) => void }) {
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
      <Choose overview={overview} reloadOverview={reloadOverview} />
    </>
  )
}

// ---------------------------------------------------------------------------
// Members: Access B, for an admin
// ---------------------------------------------------------------------------

function MemberRow({ m }: { m: AccessMember }) {
  const c = m.connection
  const writes = m.grants.filter((g) => g.mode === 'write').length
  const state = c === null ? 'not started' : (c.state ?? 'unknown')
  return (
    <li className="ac-member" data-member={m.user}>
      <div className="ac-row-name">
        <b>{m.user}</b>
        <small className="ur-mu">{c?.forge_login != null ? `@${c.forge_login}` : 'no GitHub account'}</small>
      </div>
      <span className="ac-cell">
        <span className="ac-cell-l">Connection</span>
        <ToneMark tone={state === 'active' ? 'ok' : c === null ? 'unknown' : 'bad'}>{c?.failure ?? state}</ToneMark>
      </span>
      <span className="ac-cell">
        <span className="ac-cell-l">Owners</span>
        {m.orgs.length === 0 ? <Dash why="No owner enabled" /> : m.orgs.map((o) => o.owner).join(', ')}
      </span>
      <span className="ac-cell">
        <span className="ac-cell-l">Repositories</span>
        {m.grants.length} · {writes} write
      </span>
    </li>
  )
}

/**
 * A 403 on the members view is the admin gate, not a failure (visual QA V047).
 * The API refuses a non-admin with its generic `forbidden` code ("the
 * tenant's members view is for admins"), which the client classifies as an
 * unrecognised refusal and the region drew red, with a Try again that can
 * never succeed. On this one admin-only route a refusal means exactly that,
 * so it is drawn as the admin gate. A tenant or domain refusal keeps its own.
 */
function membersGated(state: Result<unknown>): ApiError | null {
  if (state.status !== 'error') return null
  const e = state.error
  if (e.kind === 'admin_required') return e
  if (e.kind === 'forbidden' && e.httpStatus === 403) return { ...e, kind: 'admin_required' }
  return null
}

function Members() {
  const members = useUrRead(loadAccessMembers, 'access-members')
  const gate = membersGated(members.state)
  return (
    <Card className="ac-members" title="Members">
      {gate !== null ? (
        <FailedPanel error={gate} onRetry={members.reload} />
      ) : (
        <UrRegion state={members.state} route="GET /v1/access/members" what="The tenant's members" onRetry={members.reload} plural lines={4}>
          {(d) =>
            d.members.length === 0 ? (
              <EmptyState kind="empty" heading="No member has started">
                Nobody in tenant {d.tenant_id} has connected GitHub or chosen a repository yet.
              </EmptyState>
            ) : (
              <>
                {/* The rows' column names (visual QA V230); a phone's records carry their own (`.ac-cell-l`). */}
                <div className="ac-members-h" aria-hidden="true">
                  <span>Member</span>
                  <span>Connection</span>
                  <span>Owners</span>
                  <span>Repositories</span>
                </div>
                <ul className="ac-grid" aria-label={`Members of ${d.tenant_id}`}>
                  {d.members.map((m) => (
                    <MemberRow key={m.user} m={m} />
                  ))}
                </ul>
              </>
            )
          }
        </UrRegion>
      )}
      <p className="ur-hint">
        Each member connects as themselves. An admin sees states and names, never a value, and cannot reconnect for
        someone else.
      </p>
    </Card>
  )
}

/**
 * THE LEDE SAYS WHAT THE VIEW SHOWS (visual QA V232). It said "the orgs and
 * repositories SwarmCloud may reach as you" on Members and Browser logins
 * too, which are about neither. Since GITHUB-ACCESS-PAGE that list is on
 * Config › Repositories, so Mine's two tabs say what they hold instead.
 */
function accessLede(view: View, tenant: string | null): string {
  const inTenant = tenant !== null ? ` in tenant ${tenant}` : ''
  if (view === 'members') {
    return `Every member${inTenant}: whether they connected GitHub, the owners they enabled and how many repositories they chose. States and names, never a value.`
  }
  if (view === 'logins') {
    return 'The browser sessions you saved from a live browser in your workspace, by name and site, with when each expires. An agent names one to start signed in.'
  }
  if (view === 'choose') {
    return `The owners the GitHub App is installed on${inTenant}, and the repositories you choose from them, each Read or Write.`
  }
  return `How SwarmCloud acts as you on GitHub${inTenant}: your connection through the GitHub App, and what it may do.`
}

/** Config › GitHub access (Work › Access until CONFIG-NAV). */
export function AccessScreen() {
  const overview = useUrRead(loadAccess, 'access')
  const [view, setViewState] = useState<View>(viewFromUrl)
  function setView(v: View) {
    setViewState(v)
    writeViewToUrl(v)
  }
  const tenant = dataOf(overview.state)?.tenant_id ?? null
  const mine = view === 'mine' || view === 'choose'
  return (
    <div className="ur-page ac-page">
      <PageHead title="GitHub access">
        <UrRefresh reads={[overview]} />
        {/* THE SWITCH IS ITS OWN GROUP (visual QA run 4, work-b-001, part of
            #1680), so at 390 it moves to a row whole. Setup left the head with
            GITHUB-ACCESS-PAGE: the setup strip over every Config page links
            Workspace setup, and S2 draws no second one. */}
        <span className="ur-acts">
          <UrRadio<View>
            label="Whose access"
            value={mine ? 'mine' : view}
            onChange={setView}
            options={[
              { key: 'mine', label: 'Mine' },
              { key: 'members', label: 'Members', title: 'Every member of the tenant: for an admin' },
              { key: 'logins', label: 'Browser logins', title: 'Sessions you saved from a live browser in your workspace' },
            ]}
          />
        </span>
      </PageHead>
      {/* `cfg-parts` (config.css, imported with the setup strip) is the Config
          pages' tab strip layout: never shrunk, never a vertical scroller --
          the underline's -1px margin made a bare strip draw a scroll arrow. */}
      {mine && (
        <Tabs tabs={GITHUB_TABS} current={view} label="GitHub access" className="cfg-parts" onSelect={(k) => setView(k === 'choose' ? 'choose' : 'mine')} />
      )}
      <p className="ur-sub">{accessLede(view, tenant)}</p>
      {view === 'members' ? (
        <Members />
      ) : view === 'logins' ? (
        <BrowserLoginsPane tenantId={tenant} />
      ) : (
        <UrRegion state={overview.state} route="GET /v1/access" what="Your access" onRetry={overview.reload} lines={4}>
          {(o) =>
            view === 'choose' ? (
              <ChooseTab overview={o} reloadOverview={overview.reload} open={setView} />
            ) : (
              <Mine overview={o} reloadOverview={overview.reload} open={setView} />
            )
          }
        </UrRegion>
      )}
    </div>
  )
}
