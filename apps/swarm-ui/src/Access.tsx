import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import {
  disableAccessOwner, disconnectGitHub, enableAccessOwner, loadAccess, loadAccessMembers, loadAccessOwners,
  loadAccessRepositories, putAccessGrant, revokeAccessGrant, typedRepoId, verifyAccessGrant,
} from './api'
import { Banner, Button, ButtonLink, Card, Chip, Dash, EmptyState, ToneMark } from './components'
import type { ApiError, Result } from './fetch'
import { ConnectButton, Refusal } from './GitHubConnect'
import { SETUP } from './Onboarding'
import { addressToPath } from './paths'
import { UrCap, UrRadio, UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import type { CapCell } from './RepositoriesData'
import { PageHead } from './Shell'
import type {
  AccessCheck, AccessCheckName, AccessDisableResponse, AccessGrant, AccessMember, AccessMode, AccessOverview, AccessOwner,
  AccessRepository, AccessVerifyFailure, GitHubConnection,
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
 *   * D6 -- verification reads only. The opt-in write test (create and delete
 *     `swarmcloud/onboarding-check-<nonce>`) is not served by OB4's verify
 *     (`access.py`: "D6's opt-in branch write test is not built here"), so its
 *     column says not offered yet and nothing on this page writes to GitHub.
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

type View = 'mine' | 'members'

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

const CHECKS: readonly { key: AccessCheckName; label: string }[] = [
  { key: 'clone', label: 'Clone' },
  { key: 'push', label: 'Push' },
  { key: 'pull_request', label: 'Pull request' },
]

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

function GitHubLink({ url, children }: { url: string; children: ReactNode }) {
  return (
    <a className="c-link" href={url} target="_blank" rel="noreferrer">
      {children}
    </a>
  )
}

/** A refusal with the server's §2.3 copy, its GitHub page, and an optional retry. */
function AccessRefusal({ error, title, onRetry }: { error: ApiError; title: string; onRetry?: () => void }) {
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

export function AccessPolicy() {
  return (
    <Card className="ac-policy" title="What SwarmCloud may do as you" level={2}>
      <ul className="ac-rules">
        <li data-rule="D9">
          <b>Read means read, and SwarmCloud enforces it.</b> A repository granted Read is cloned and never pushed:
          submission refuses a task that would push to it, and the worker refuses the push. GitHub itself would let
          the push through if your account can write there, so the line is held by SwarmCloud, not by GitHub.
        </li>
        <li data-rule="D8">
          <b>No workflow changes.</b> The SwarmCloud App asks GitHub for contents, pull requests, issues and checks
          (read). It does not ask for workflows write, so a change under <code>.github/workflows/</code> cannot be
          pushed through SwarmCloud.
        </li>
        <li data-rule="D6">
          <b>Verifying only reads.</b> Clone reads the upload-pack advertisement, Push the receive-pack advertisement
          and your push permission, Pull request the installation's pull-request permission. Nothing is written to
          GitHub. The opt-in write test is not offered yet.
        </li>
      </ul>
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
          <p className="ur-sub">
            No GitHub account is connected. Connect yours so SwarmCloud lists the orgs and repositories you reach, and
            clones, pushes and opens pull requests as you.
          </p>
          <div className="ur-gh-acts">
            <ConnectButton />
          </div>
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
              reconnect. Work › Setup carries the recovery steps.
            </p>
          )}
          {confirming ? (
            <Banner
              tone="warn"
              title={connection.forge_login !== null ? `Disconnect @${connection.forge_login}?` : 'Disconnect GitHub?'}
              actions={
                <>
                  <Button kind="danger-filled" size="sm" busy={busy} onClick={() => void disconnect()}>
                    Disconnect
                  </Button>
                  <Button size="sm" onClick={() => setConfirming(false)} disabled={busy}>
                    Keep connected
                  </Button>
                </>
              }
            >
              <p className="ur-small">
                SwarmCloud revokes its authorisation at GitHub, disables the token it stored for you and forgets every
                org and repository you granted. Tasks you submit afterwards wait until you connect again.
              </p>
            </Banner>
          ) : (
            <div className="ur-gh-acts">
              {!active && <ConnectButton label="Reconnect" />}
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
  if (o.install_state !== 'installed') return `${kind} · not installed`
  return `${kind} · ${o.repository_selection === 'all' ? 'all repositories' : 'selected repositories'}`
}

function Owners({
  owners,
  orgsListed,
  grants,
  selected,
  onSelect,
  onEnabled,
  onChange,
}: {
  owners: AccessOwner[]
  orgsListed: boolean
  grants: AccessGrant[]
  selected: string | null
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

  return (
    <div className="ac-owners">
      <ul className="ac-owner-list" aria-label="Owners your connection reaches">
        {owners.map((o) => {
          const key = o.owner.toLowerCase()
          const granted = grants.filter((g) => g.owner.toLowerCase() === key).length
          const isSel = selected === key
          return (
            <li key={key} className={`ac-owner${isSel ? ' is-selected' : ''}`} data-owner={o.owner}>
              <div className="ac-owner-h">
                {o.enabled ? (
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
                    <Banner
                      tone="warn"
                      title={`Remove ${o.owner}?`}
                      actions={
                        <>
                          <Button kind="danger-filled" size="sm" busy={busy === o.owner} onClick={() => void disable(o.owner)}>
                            Remove
                          </Button>
                          <Button size="sm" onClick={() => setRemoving(null)}>
                            Keep
                          </Button>
                        </>
                      }
                    >
                      <p className="ur-small">
                        Deletes your {granted} grant{granted === 1 ? '' : 's'} under {o.owner} at once; tasks naming its
                        repositories are refused from then on.
                      </p>
                    </Banner>
                  ) : (
                    <Button size="sm" kind="ghost" onClick={() => setRemoving(key)}>
                      Remove
                    </Button>
                  )
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
                  <ButtonLink size="sm" kind="primary" href={o.install_url} target="_blank" rel="noreferrer">
                    Install on {o.owner}
                  </ButtonLink>
                ) : (
                  <Dash why="The App's install page is not configured on this API" />
                )}
              </div>
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

type Choice = 'none' | AccessMode

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

export function RepoChooser({ owner, onChange }: { owner: string; onChange: () => void }) {
  const [page, setPage] = useState(1)
  const [typed, setTyped] = useState('')
  const [query, setQuery] = useState('')
  const [only, setOnly] = useState<'all' | 'chosen'>('all')
  const [nonce, setNonce] = useState(0)
  const [busy, setBusy] = useState<string | null>(null)
  const [refused, setRefused] = useState<{ repo: string; error: ApiError } | null>(null)
  const [settling, setSettling] = useState(false)
  const read = useUrRead(() => onceMoreOnConflict(() => loadAccessRepositories(owner, page, query), setSettling), `${owner}|${page}|${query}|${nonce}`)

  async function choose(r: AccessRepository, c: Choice) {
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
          const shown = p.repositories.filter((r) => only === 'all' || r.granted)
          const pages = p.total_count !== null ? Math.max(1, Math.ceil(p.total_count / p.per_page)) : null
          return (
            <>
              {shown.length === 0 ? (
                <EmptyState kind="empty" heading={p.q !== null ? `Nothing in ${owner} matches “${p.q}”` : only === 'chosen' ? 'None chosen on this page' : 'No repositories on this page'}>
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
                            {r.visibility ?? 'visibility unknown'}
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
                            if (c !== choice) void choose(r, c)
                          }}
                          options={[
                            { key: 'none', label: 'Not chosen' },
                            { key: 'read', label: 'Read' },
                            { key: 'write', label: 'Write', disabled: why !== undefined && choice !== 'write', title: why },
                          ]}
                        />
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
// The grants, verified: Verify A
// ---------------------------------------------------------------------------

/** 0 a failure, 1 not yet known, 2 passed: the grid's order (failures first). */
export function grantRank(g: AccessGrant): number {
  const needed: AccessCheckName[] = g.mode === 'write' ? ['clone', 'push', 'pull_request'] : ['clone']
  const states = needed.map((k) => g.checks[k]?.state)
  if (states.some((s) => s === 'missing')) return 0
  if (states.some((s) => s !== 'ok')) return 1
  return 2
}

function CheckCell({ grant, name, label }: { grant: AccessGrant; name: AccessCheckName; label: string }) {
  const c: AccessCheck | undefined = grant.checks[name]
  if (c?.state === 'not_required' || (c === undefined && grant.mode === 'read' && name !== 'clone')) {
    return <Dash why={`${label} is not needed for a read grant`} />
  }
  if (c === undefined) return <UrCap label={label} cell={{ state: 'unknown', reason: 'Not checked yet' }} word="not checked" />
  const state = c.state === 'ok' || c.state === 'missing' ? c.state : 'unknown'
  const reason = [c.code, c.checked_at !== null ? `checked ${timeAgo(c.checked_at)}` : null].filter(Boolean).join(' · ') || null
  return <UrCap label={label} cell={{ state, reason }} word={state} />
}

function Grants({ grants, onChange }: { grants: AccessGrant[]; onChange: () => void }) {
  const [busy, setBusy] = useState<string | null>(null)
  const [failures, setFailures] = useState<Record<string, AccessVerifyFailure[]>>({})
  const [refused, setRefused] = useState<{ repo: string; error: ApiError } | null>(null)
  const [all, setAll] = useState(false)
  const [filter, setFilter] = useState('')
  const sorted = [...grants]
    .filter((g) => filter.trim() === '' || g.repository.toLowerCase().includes(filter.trim().toLowerCase()))
    .sort((a, b) => grantRank(a) - grantRank(b) || a.repository.localeCompare(b.repository))

  async function verify(g: AccessGrant): Promise<void> {
    setBusy(g.repo_id)
    setRefused(null)
    const res = await verifyAccessGrant(g.repo_id)
    setBusy(null)
    if (res.status === 'error') {
      setRefused({ repo: g.repo_id, error: res.error })
      return
    }
    if (res.status === 'ok') setFailures((f) => ({ ...f, [g.repo_id]: res.data.failures }))
  }

  async function verifyAll() {
    setAll(true)
    for (const g of grants) await verify(g)
    setAll(false)
    onChange()
  }

  async function change(g: AccessGrant, mode: AccessMode | 'remove') {
    setBusy(g.repo_id)
    setRefused(null)
    const res = mode === 'remove' ? await revokeAccessGrant(g.repo_id) : await putAccessGrant(g.repo_id, g.repository, mode)
    setBusy(null)
    if (res.status === 'error') {
      setRefused({ repo: g.repo_id, error: res.error })
      return
    }
    onChange()
  }

  const newest = grants.map((g) => g.verified_at).filter((v): v is string => v !== null).sort().pop() ?? null

  return (
    <Card
      className="ac-grants"
      title="Repositories SwarmCloud may reach as you"
      action={
        <span className="ur-acts">
          <Button
            size="sm"
            busy={all}
            disabled={grants.length === 0}
            onClick={() => {
              void verifyAll()
            }}
          >
            Re-check all
          </Button>
        </span>
      }
      foot={<>Last verified {newest !== null ? timeAgo(newest) : <Dash why="No grant has been verified yet" />}. A task for a repository not on this list is refused at submission.</>}
    >
      {grants.length === 0 ? (
        <EmptyState kind="empty" heading="No repository chosen yet">
          Choose repositories from an enabled owner above, or type owner/repo.
        </EmptyState>
      ) : (
        <>
          {grants.length > 8 && (
            <input
              type="search"
              className="ur-search ac-filter"
              aria-label="Filter your repositories"
              placeholder="Filter"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
          )}
          <div className="ac-grid-h" aria-hidden="true">
            <span>Repository</span>
            <span>Access</span>
            {CHECKS.map((c) => (
              <span key={c.key}>{c.label}</span>
            ))}
            <span>Write test</span>
            <span />
          </div>
          <ul className="ac-grid" aria-label="Your granted repositories, failures first">
            {sorted.map((g) => {
              const rank = grantRank(g)
              const fails = failures[g.repo_id] ?? []
              return (
                <li key={g.repo_id} className={`ac-grant is-rank-${rank}`} data-repo={g.repository} data-rank={rank}>
                  <div className="ac-row-name">
                    <b>{g.repository}</b>
                    <small className="ur-mu">
                      {g.verified_at !== null ? `verified ${timeAgo(g.verified_at)}` : 'not verified yet'}
                      {g.archived === true && ' · archived'}
                    </small>
                  </div>
                  <UrRadio<AccessMode>
                    label={`Access to ${g.repository}`}
                    value={g.mode}
                    disabled={busy === g.repo_id}
                    onChange={(m) => {
                      if (m !== g.mode) void change(g, m)
                    }}
                    options={[
                      { key: 'read', label: 'Read' },
                      {
                        key: 'write',
                        label: 'Write',
                        disabled: g.mode !== 'write' && (g.can_push === false || g.archived === true),
                        title: g.archived === true ? 'Archived on GitHub' : g.can_push === false ? 'GitHub does not let you push here' : undefined,
                      },
                    ]}
                  />
                  {CHECKS.map((c) => (
                    <span key={c.key} className="ac-cell" data-check={c.key}>
                      <span className="ac-cell-l">{c.label}</span>
                      <CheckCell grant={g} name={c.key} label={c.label} />
                    </span>
                  ))}
                  <span className="ac-cell" data-check="write_test">
                    <span className="ac-cell-l">Write test</span>
                    <Dash why="The opt-in write test (D6) is not served by this API yet; verification only reads" />
                  </span>
                  <span className="ac-grant-acts">
                    <Button size="sm" busy={busy === g.repo_id} onClick={() => void verify(g).then(onChange)}>
                      Verify
                    </Button>
                    <Button size="sm" kind="ghost" disabled={busy === g.repo_id} onClick={() => void change(g, 'remove')}>
                      Remove
                    </Button>
                  </span>
                  {fails.length > 0 && (
                    <div className="ac-fails">
                      {fails.map((f, i) => (
                        <div key={`${f.check}:${f.code}:${i}`} className="ob-issue" data-code={f.code}>
                          <p className="ur-small">
                            <b className="ur-bad">{f.code}</b> {f.copy}
                          </p>
                          {typeof f.url === 'string' && f.url.startsWith('https://github.com/') && <GitHubLink url={f.url}>Open on GitHub</GitHubLink>}
                        </div>
                      ))}
                    </div>
                  )}
                  {refused !== null && refused.repo === g.repo_id && (
                    <div className="ac-fails">
                      <AccessRefusal error={refused.error} title={`${g.repository} was not changed`} onRetry={() => void verify(g)} />
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        </>
      )}
      <p className="ur-hint">
        Removing an org deletes its grants at once, and tasks naming its repositories are refused from then on. GitHub
        still lets your token reach an installed org until an org owner uninstalls the App or you disconnect.
      </p>
    </Card>
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
 * reads the owners again.
 */
export function NotInstalledNotice({ login, installUrl }: { login: string | null; installUrl: string | null }) {
  return (
    <section className="ac-install" aria-label="SwarmCloud Saga is not installed">
      <p className="ac-install-h">
        <b>
          {login !== null ? `You're connected as @${login}` : "You're connected"}, but SwarmCloud Saga isn't installed anywhere yet.
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

function Choose({ overview, reloadOverview }: { overview: AccessOverview; reloadOverview: () => void }) {
  const owners = useUrRead(loadAccessOwners, 'access-owners')
  const [picked, setPicked] = useState<string | null>(null)
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
        Authorized is not installed: Connect lets SwarmCloud act as you (GitHub › Authorized GitHub Apps); installing it on
        an account or org lets it reach that owner's repositories (Installed GitHub Apps).
      </p>
      <UrRegion state={owners.state} route="GET /v1/access/orgs" what="The owners your connection reaches" onRetry={owners.reload} plural lines={3} reading={ASKING_GITHUB}>
        {(o) => (
          <>
            {!o.owners.some((x) => x.install_state === 'installed') && (
              <NotInstalledNotice
                login={login ?? o.owners.find((x) => x.owner_type === 'User')?.owner ?? null}
                installUrl={o.install_url}
              />
            )}
            <div className="ac-chooser">
              <Owners
                owners={o.owners}
                orgsListed={o.orgs_listed}
                grants={overview.grants}
                selected={selected}
                onSelect={setPicked}
                onEnabled={(key) => {
                  setLanded((was) => (was.includes(key) ? was : [...was, key]))
                  setPicked(key)
                }}
                onChange={reloadAll}
              />
              {selected !== null ? (
                <RepoChooser key={selected} owner={selected} onChange={reloadOverview} />
              ) : o.owners.some((x) => x.install_state === 'installed') ? (
                <EmptyState kind="empty" heading="No owner enabled yet">
                  Enable an owner on the left: SwarmCloud lists its repositories once the App is installed there.
                </EmptyState>
              ) : (
                <EmptyState kind="empty" heading="Nothing to list until the App is installed">
                  Install SwarmCloud Saga on an owner on the left; its repositories are listed here once it is, and you have
                  enabled it.
                </EmptyState>
              )}
            </div>
          </>
        )}
      </UrRegion>
      <TypedRepository tenantId={overview.tenant_id} onChange={reloadAll} />
    </Card>
  )
}

function Mine({ overview, reloadOverview }: { overview: AccessOverview; reloadOverview: () => void }) {
  const connected = overview.connection !== null && overview.connection.state === 'active'
  return (
    <>
      <Connection connection={overview.connection} onChange={reloadOverview} />
      {connected && <Choose overview={overview} reloadOverview={reloadOverview} />}
      {(connected || overview.grants.length > 0) && <Grants grants={overview.grants} onChange={reloadOverview} />}
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

function Members() {
  const members = useUrRead(loadAccessMembers, 'access-members')
  return (
    <Card className="ac-members" title="Members">
      <UrRegion state={members.state} route="GET /v1/access/members" what="The tenant's members" onRetry={members.reload} plural lines={4}>
        {(d) =>
          d.members.length === 0 ? (
            <EmptyState kind="empty" heading="No member has started">
              Nobody in tenant {d.tenant_id} has connected GitHub or chosen a repository yet.
            </EmptyState>
          ) : (
            <ul className="ac-grid" aria-label={`Members of ${d.tenant_id}`}>
              {d.members.map((m) => (
                <MemberRow key={m.user} m={m} />
              ))}
            </ul>
          )
        }
      </UrRegion>
      <p className="ur-hint">
        Each member connects as themselves. An admin sees states and names, never a value, and cannot reconnect for
        someone else.
      </p>
    </Card>
  )
}

/** Work › Access. */
export function AccessScreen() {
  const overview = useUrRead(loadAccess, 'access')
  const [view, setView] = useState<View>('mine')
  const tenant = dataOf(overview.state)?.tenant_id ?? null
  return (
    <div className="ur-page ac-page">
      <PageHead title="Access">
        <UrRefresh reads={[overview]} />
        <UrRadio<View>
          label="Whose access"
          value={view}
          onChange={setView}
          options={[
            { key: 'mine', label: 'Mine' },
            { key: 'members', label: 'Members', title: 'Every member of the tenant: for an admin' },
          ]}
        />
        <span className="ur-acts">
          <ButtonLink size="sm" href={addressToPath(SETUP)}>
            Setup
          </ButtonLink>
        </span>
      </PageHead>
      <p className="ur-sub">
        The orgs and repositories SwarmCloud may reach as you{tenant !== null ? ` in tenant ${tenant}` : ''}, each Read or
        Write, and whether it can.
      </p>
      <AccessPolicy />
      {view === 'members' ? (
        <Members />
      ) : (
        <UrRegion state={overview.state} route="GET /v1/access" what="Your access" onRetry={overview.reload} lines={4}>
          {(o) => <Mine overview={o} reloadOverview={overview.reload} />}
        </UrRegion>
      )}
    </div>
  )
}
