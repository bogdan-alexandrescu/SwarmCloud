import { useEffect, useState, type ReactNode } from 'react'
import { authorizeGitHub, disconnectGitHub, enableAccessOwner, exchangeGitHub, loadOnboarding, loadRepositories, verifyAccessGrant, verifyGitToken } from './api'
import { Banner, Button, ButtonLink, Card, Chip, Dash, ToneMark } from './components'
import { errorHeading, type ApiError, type Result } from './fetch'
import { indexCredentialWords, type RepoRecord } from './RepositoriesData'
import { GT_PAGE, UrNavButton, UrRefresh, UrRegion, useUrRead, type UrRead } from './RepositoriesParts'
import { PageHead, useClaimPageAge } from './Shell'
import { prefillTask } from './submitPrefill'
import type {
  AccessVerifyResponse,
  AppInstalledEvidence,
  GitHubConnectedEvidence,
  GitHubDisconnectResponse,
  GitHubExchangeBody,
  GitHubExchangeResponse,
  GitHubRefusalDetail,
  OnboardingDoc,
  OnboardingIssue,
  OnboardingStep,
  OrgsEnabledEvidence,
} from './types'
import { timeAgo } from './types'
import './styles/repositories.css'

/**
 * CONNECT GITHUB (#780, lane OB3's console half; the owner pulled it forward
 * of OB8 on 2026-10-07 so a person can connect before the full checklist
 * exists). Two pieces:
 *
 *   * `GitHubSetupCard`, on Work › Repositories › Git tokens -- where the
 *     console shows git access today -- reads `GET /v1/onboarding` and guides
 *     a person through three steps (GT-GUIDE, 2026-10-12): Connect GitHub
 *     (`POST .../authorize`, then the browser goes to GitHub; ending one is
 *     `DELETE /v1/onboarding/github`, behind a confirm), Choose what it
 *     reaches, and Try it;
 *   * `GitHubCallbackScreen`, at `/onboarding/github/callback`, the App's
 *     registered callback URL: it posts GitHub's `state` and `code` (or
 *     `error`) to `POST .../exchange` ONCE and says what came of it.
 *
 * THE CODE AND THE STATE ARE NEVER KEPT, LOGGED OR DRAWN. They are read from
 * the address bar once, handed to the one exchange post, and the bar is
 * rewritten without them before anything else runs (`takeGitHubCallback`).
 * No React state, no ref and no element holds either, and the server's answers
 * carry neither (forgeapp.py: "NO ROUTE RETURNS A VALUE").
 */

/** Where the authorise URL may send the browser. The server mints it; this refuses anything else. */
const GITHUB_AUTHORIZE = 'https://github.com/'

/**
 * The browser's navigation, as one seam. jsdom implements no navigation, and
 * a test proving the Connect button goes to GitHub has to see where it went.
 */
export const browser = {
  assign(url: string): void {
    window.location.assign(url)
  },
}

/**
 * Start the authorisation: mint a single-use, ten-minute authorise URL for
 * this person and send the browser there. Answers only when it did NOT
 * navigate -- with the refusal to show.
 */
export async function startGitHubConnect(): Promise<ApiError> {
  const res = await authorizeGitHub('console')
  if (res.status === 'error') return res.error
  const url = res.status === 'ok' ? res.data?.authorize_url : undefined
  if (typeof url !== 'string' || !url.startsWith(GITHUB_AUTHORIZE)) {
    return {
      kind: 'server_error',
      httpStatus: null,
      code: null,
      message: 'The API answered without a GitHub sign-in page to open, so nothing was opened. Press Connect GitHub again.',
    }
  }
  browser.assign(url)
  // Navigation is under way; the page is about to be replaced. Nothing to show.
  return new Promise<ApiError>(() => {})
}

/** A refusal's §2.3 copy, when the server sent one (`forgeapp.AuthorisationRefused`). */
function refusalOf(e: ApiError): GitHubRefusalDetail | null {
  const d = e.detail as Partial<GitHubRefusalDetail> | null | undefined
  return d && typeof d.failure_code === 'string' && typeof d.recovery === 'string'
    ? { failure_code: d.failure_code, recovery: d.recovery }
    : null
}

/**
 * What a failed write says: the §2.3 recovery copy first, the server's own
 * sentence under it. Exported for Setup and Access (OB8), whose refusals
 * (`access.AccessRefused`) carry the same `failure_code` and `recovery`.
 */
export function Refusal({ error, title, actions }: { error: ApiError; title: string; actions?: ReactNode }) {
  const r = refusalOf(error)
  return (
    <Banner tone="bad" title={title} actions={actions} role="alert">
      {r !== null ? (
        <>
          <p className="ur-small">{r.recovery}</p>
          <p className="ur-hint">
            {error.message} <span className="ur-mu">({r.failure_code})</span>
          </p>
        </>
      ) : (
        <p className="ur-small">
          {errorHeading(error)}: {error.message}
        </p>
      )}
    </Banner>
  )
}

/** Connect GitHub (Connect A): one button for the App. Exported for Setup and Access (OB8). */
export function ConnectButton({ label = 'Connect GitHub' }: { label?: string }) {
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  return (
    <>
      <Button
        kind="primary"
        size="sm"
        busy={busy}
        onClick={() => {
          setBusy(true)
          setRefused(null)
          void startGitHubConnect().then((e) => {
            setBusy(false)
            setRefused(e)
          })
        }}
      >
        {label}
      </Button>
      {refused !== null && <Refusal error={refused} title="GitHub's sign-in page was not opened" />}
    </>
  )
}

// ---------------------------------------------------------------------------
// What the onboarding document says about the connection
// ---------------------------------------------------------------------------

function stepOf(doc: OnboardingDoc, name: OnboardingStep['step']): OnboardingStep | null {
  return doc.steps.find((s) => s.step === name) ?? null
}

/** The connection as the card draws it, from `github_connected`'s evidence. */
export type ConnectionView =
  /** The caller's own App connection: what Disconnect ends. */
  | { kind: 'app'; login: string | null; failed: OnboardingStep | null }
  /** The caller's own slot holds a stored token, not the App's. */
  | { kind: 'own-token'; login: string | null }
  /** Nothing of the caller's: tasks act through the tenant token. */
  | { kind: 'tenant'; login: string | null }
  | { kind: 'none' }

export function connectionOf(doc: OnboardingDoc): ConnectionView {
  const step = stepOf(doc, 'github_connected')
  const ev = (step?.evidence ?? {}) as Partial<GitHubConnectedEvidence>
  const login = typeof ev.forge_login === 'string' ? ev.forge_login : null
  if (ev.via === 'user' && ev.kind === 'app_user') return { kind: 'app', login, failed: step?.state === 'failed' ? step : null }
  if (ev.via === 'user') return { kind: 'own-token', login }
  if (ev.via === 'tenant') return { kind: 'tenant', login }
  return { kind: 'none' }
}

const REACH_WORD: Record<string, string> = {
  reachable: 'reachable',
  sso_required: 'SSO not authorised',
  classic_blocked: 'refuses classic tokens',
}

/** Where the orgs are enabled and the repositories chosen (OB4's routes; Onboarding.tsx `ACCESS`). */
export const ACCESS_PAGE = 'work/access'
const SUBMIT_TASK = 'work/new'

function str(v: unknown): string | null {
  return typeof v === 'string' && v !== '' ? v : null
}

/** The guided step (1-3) an onboarding step belongs to. */
const GUIDE_OF: Partial<Record<OnboardingStep['step'], 1 | 2 | 3>> = {
  github_connected: 1,
  app_installed: 1,
  orgs_enabled: 2,
  repos_chosen: 2,
  access_verified: 3,
}

/**
 * The orgs line. NEVER "they appear here once it has" (GT-GUIDE, owner
 * 2026-10-12): `orgs_enabled` completes only once the person ENABLES an owner
 * on Work › Access, and the old line waited on a verification that was never
 * going to tick it. It now says what the step waits for -- an earlier step,
 * the first read of the owners, or the person -- and where to act. `onEnabled`
 * draws an inline Enable per owner (`POST /v1/access/orgs`, Access's own
 * route) and re-reads the steps after it.
 */
function Owners({ doc, onEnabled }: { doc: OnboardingDoc; onEnabled?: () => void }) {
  const step = stepOf(doc, 'orgs_enabled')
  const ev = (step?.evidence ?? {}) as Partial<OrgsEnabledEvidence> & { enabled?: unknown; waiting_for?: unknown }
  const owners = Array.isArray(ev.owners) ? ev.owners : []
  const waiting = str(ev.waiting_for)
  const enabled = Array.isArray(ev.enabled) ? (ev.enabled as unknown[]).filter((o): o is string => typeof o === 'string') : null
  const [busy, setBusy] = useState<string | null>(null)
  const [refused, setRefused] = useState<ApiError | null>(null)

  async function enable(owner: string) {
    setBusy(owner)
    setRefused(null)
    const res = await enableAccessOwner(owner)
    setBusy(null)
    if (res.status === 'error') {
      setRefused(res.error)
      return
    }
    onEnabled?.()
  }

  if (waiting !== null && GUIDE_OF[waiting as OnboardingStep['step']] === 1) {
    return <p className="ur-hint">Orgs: after GitHub is connected and the App installed, enable the ones SwarmCloud may work in on Work › Access.</p>
  }
  if (step === null || owners.length === 0) {
    return (
      <p className="ur-hint">
        Orgs <Dash why="Not read yet" /> SwarmCloud has not read the accounts and orgs this connection reaches yet; then enable the ones SwarmCloud may work in on Work › Access.
      </p>
    )
  }
  const issues: OnboardingIssue[] = Array.isArray(step.issues) ? step.issues : []
  const isOn = (o: string) => enabled !== null && enabled.some((e) => e.toLowerCase() === o.toLowerCase())
  return (
    <div className="ur-gh-orgs">
      <p className="ur-sub">Reaches</p>
      <ul aria-label="Accounts and orgs this connection reaches">
        {owners.map((o) => (
          <li key={o.owner}>
            <Chip tone={o.reach === 'reachable' ? undefined : 'bad'} title={o.owner_type === 'User' ? 'Your account' : 'An org'}>
              {o.owner}
              {o.reach !== 'reachable' ? ` · ${REACH_WORD[o.reach] ?? o.reach}` : isOn(o.owner) ? ' · enabled' : ''}
            </Chip>
            {onEnabled !== undefined && enabled !== null && o.reach === 'reachable' && !isOn(o.owner) && (
              <Button size="sm" kind="ghost" busy={busy === o.owner} disabled={busy !== null} aria-label={`Enable ${o.owner}`} onClick={() => void enable(o.owner)}>
                Enable
              </Button>
            )}
          </li>
        ))}
      </ul>
      {step.state !== 'done' && (
        <p className="ur-small">
          Enable the accounts and orgs SwarmCloud may work in{onEnabled !== undefined && enabled !== null ? ' here, or' : ''} on Work › Access, where you also choose their repositories.
        </p>
      )}
      {ev.sso_hidden_orgs !== undefined && ev.sso_hidden_orgs > 0 && (
        <p className="ur-hint">
          GitHub hid {ev.sso_hidden_orgs} more org{ev.sso_hidden_orgs === 1 ? '' : 's'} behind single sign-on.
        </p>
      )}
      {issues.map((i) => (
        <p key={`${i.code}:${i.owner ?? ''}`} className="ur-small ur-bad">
          {i.copy}
        </p>
      ))}
      {refused !== null && <Refusal error={refused} title="The org was not enabled" />}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Set up GitHub access: the guided steps, on Git tokens
// ---------------------------------------------------------------------------

type GuideState = 'done' | 'todo' | 'failed' | 'waiting'

const GUIDE_WORD: Readonly<Record<GuideState, string>> = { done: 'done', todo: 'to do', failed: 'needs you', waiting: 'waiting' }

/**
 * One guided step's state, from the onboarding steps it groups. DERIVED, NEVER
 * SET: done only when the server derived every one of them done. A step this
 * server does not serve is left out rather than counted either way; one
 * waiting on an EARLIER guided step is `waiting`, and offers nothing.
 */
function guideState(doc: OnboardingDoc, names: OnboardingStep['step'][]): { state: GuideState; after: 1 | 2 | null } {
  const steps = names.map((n) => stepOf(doc, n)).filter((s): s is OnboardingStep => s !== null)
  const own = GUIDE_OF[names[0]!]!
  for (const s of steps) {
    const on = GUIDE_OF[str(s.evidence?.waiting_for) as OnboardingStep['step']]
    if (s.state !== 'done' && on !== undefined && on < own) return { state: 'waiting', after: on as 1 | 2 }
  }
  if (steps.length > 0 && steps.every((s) => s.state === 'done')) return { state: 'done', after: null }
  if (steps.some((s) => s.state === 'failed')) return { state: 'failed', after: null }
  return { state: 'todo', after: null }
}

/** A grant or registration the steps name: `repos_chosen`'s evidence rows. */
interface ChosenRepo {
  repo_id: string
  repository: string
  owner: string
}

function chosenRepos(doc: OnboardingDoc): ChosenRepo[] {
  const rows = stepOf(doc, 'repos_chosen')?.evidence?.repositories
  if (!Array.isArray(rows)) return []
  const out: ChosenRepo[] = []
  for (const r of rows as unknown[]) {
    if (typeof r !== 'object' || r === null) continue
    const row = r as Record<string, unknown>
    const id = str(row.repo_id)
    const name = str(row.repository)
    if (id === null || name === null) continue
    out.push({ repo_id: id, repository: name, owner: str(row.owner) ?? name.split('/')[0]! })
  }
  return out
}

/**
 * The harmless prompt "Run a test task" hands Submit: it reads and reports.
 * With the default strategy (`collect`), nothing is pushed either way.
 */
export const TEST_TASK_PROMPT =
  'This is a SwarmCloud test task. List the top-level files of this repository and say in three sentences what the project does. Do not change, commit or push anything.'

/** Only an `owner/repo` this page can name as a github.com clone URL. */
const OWNER_REPO = /^[A-Za-z0-9-]{1,39}\/[A-Za-z0-9._-]{1,100}$/

function GuideRow({ n, title, state, line, actions, children }: {
  n: 1 | 2 | 3
  title: string
  state: GuideState
  line?: ReactNode
  actions?: ReactNode
  children?: ReactNode
}) {
  return (
    <li className="ur-step" data-n={n} data-state={state}>
      <span className="ur-step-mark" aria-hidden="true">
        {state === 'done' ? '✓' : n}
      </span>
      <div className="ur-step-main">
        <div className="ur-step-h">
          <h3>{title}</h3>
          <span className="ur-step-word">{GUIDE_WORD[state]}</span>
        </div>
        {line !== undefined && line !== null && <p className="ur-step-line">{line}</p>}
        {children}
      </div>
      {actions !== undefined && actions !== null && <div className="ur-gh-acts ur-step-acts">{actions}</div>}
    </li>
  )
}

/**
 * SET UP GITHUB ACCESS (lane GT-GUIDE, the owner's GUIDED STEPS design,
 * 2026-10-12). A new person lands on Git tokens straight after their workspace
 * is approved; the page used to open on token cards and a terminal command.
 * It now opens on three steps, each ticked by `GET /v1/onboarding` and never
 * by this page:
 *
 *   1. Connect GitHub          -- `github_connected` + `app_installed`
 *   2. Choose what it reaches  -- `orgs_enabled` + `repos_chosen`
 *   3. Try it                  -- `access_verified`
 *
 * A step that is not done says exactly what to do next and offers it here or
 * links to where it is done (Work › Access). Try it acts through the routes
 * that already exist -- a grant's verify, or the token's -- and hands Submit
 * a harmless task. No token value passes through any of it.
 */
export function GitHubSetupCard({ read, go, onChange }: { read: UrRead<OnboardingDoc>; go: (to: string) => void; onChange?: () => void }) {
  const done = read.state.status === 'ok' || read.state.status === 'stale' ? countDone(read.state.data) : null
  return (
    <Card className="ur-gh ur-steps" title="Set up GitHub access" action={done === null ? undefined : <span className="ur-steps-count">{`${done} of 3 done`}</span>}>
      <UrRegion state={read.state} route="GET /v1/onboarding" what="Your GitHub setup" onRetry={read.reload} lines={3}>
        {(d) => (
          <>
            <ol className="ur-steps-list">
              <ConnectStep doc={d} read={read} onChange={onChange} />
              <ReachStep doc={d} read={read} go={go} />
              <TryStep doc={d} read={read} go={go} />
            </ol>
            <p className="ur-hint">
              Checked {d.derived_at ? timeAgo(d.derived_at) : <Dash why="The read carried no time" />}. No token passes
              through this page: GitHub hands it to SwarmCloud, which stores it in Secret Manager.
            </p>
          </>
        )}
      </UrRegion>
    </Card>
  )
}

function countDone(doc: OnboardingDoc): number {
  return [guideState(doc, ['github_connected', 'app_installed']), guideState(doc, ['orgs_enabled', 'repos_chosen']), guideState(doc, ['access_verified'])].filter(
    (g) => g.state === 'done',
  ).length
}

function ConnectStep({ doc, read, onChange }: { doc: OnboardingDoc; read: UrRead<OnboardingDoc>; onChange?: () => void }) {
  const view = connectionOf(doc)
  const g = guideState(doc, ['github_connected', 'app_installed'])
  const installed = stepOf(doc, 'app_installed')
  const inst = (installed?.evidence ?? {}) as Partial<AppInstalledEvidence> & { waiting_for?: unknown }
  const installUrl = str(inst.install_url)
  const needsInstall = installed !== null && installed.state !== 'done' && str(inst.waiting_for) === null && inst.needed !== false
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const [gone, setGone] = useState<GitHubDisconnectResponse | null>(null)

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
    setGone(res.status === 'ok' ? res.data : null)
    read.reload()
    onChange?.()
  }

  const who = (login: string | null) => (login === null ? <Dash why="The account was not read yet" /> : <b>@{login}</b>)

  const line =
    view.kind === 'app' ? (
      <>
        Connected as {who(view.login)} through the SwarmCloud GitHub App ·{' '}
        {view.failed !== null ? <ToneMark tone="bad">access ended</ToneMark> : 'renews automatically'}
      </>
    ) : view.kind === 'own-token' ? (
      <>
        Your own stored token ({who(view.login)}) is what your agents use. Connect GitHub to act through the SwarmCloud App
        instead: no token to paste, and it renews itself.
      </>
    ) : view.kind === 'tenant' ? (
      <>
        Your agents act through the tenant token ({who(view.login)}), not as you. Connect your GitHub account so SwarmCloud
        clones, pushes and opens pull requests as you.
      </>
    ) : (
      <>No GitHub account is connected. Connect yours so SwarmCloud clones, pushes and opens pull requests as you.</>
    )

  const actions = confirming ? null : (
    <>
      {needsInstall && installUrl !== null && installUrl.startsWith(GITHUB_AUTHORIZE) && (
        <ButtonLink kind="primary" size="sm" href={installUrl} target="_blank" rel="noreferrer">
          Install the App
        </ButtonLink>
      )}
      {needsInstall && (
        <Button size="sm" kind="ghost" onClick={read.reload}>
          Re-check
        </Button>
      )}
      {view.kind === 'app' ? (
        <>
          {view.failed !== null && <ConnectButton label="Reconnect" />}
          <Button kind="danger" size="sm" onClick={() => setConfirming(true)}>
            Disconnect
          </Button>
        </>
      ) : (
        <ConnectButton />
      )}
    </>
  )

  return (
    <GuideRow n={1} title="Connect GitHub" state={g.state} line={line} actions={actions}>
      {gone !== null && (
        <Banner tone={gone.github_revoked ? 'info' : 'warn'} title="GitHub was disconnected" role="status">
          <p className="ur-small">{gone.github}</p>
        </Banner>
      )}
      {view.kind === 'app' && view.failed !== null && view.failed.copy !== null && <p className="ur-small ur-bad">{view.failed.copy}</p>}
      {needsInstall && (
        <p className="ur-small">
          Install the SwarmCloud App on your account or an org you work in: connecting lets it act as you, installing lets
          it reach that owner's repositories. Then press Re-check.
        </p>
      )}
      {confirming && (
        <Banner
          tone="warn"
          title={view.kind === 'app' && view.login !== null ? `Disconnect @${view.login}?` : 'Disconnect GitHub?'}
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
            SwarmCloud revokes its authorisation at GitHub, disables the token it stored for you and forgets the
            repositories you granted. Agents you submit afterwards wait until you connect again.
          </p>
        </Banner>
      )}
      {refused !== null && <Refusal error={refused} title="GitHub was not disconnected" />}
    </GuideRow>
  )
}

function ReachStep({ doc, read, go }: { doc: OnboardingDoc; read: UrRead<OnboardingDoc>; go: (to: string) => void }) {
  const g = guideState(doc, ['orgs_enabled', 'repos_chosen'])
  if (g.state === 'waiting') {
    return <GuideRow n={2} title="Choose what it reaches" state="waiting" line={`After step ${g.after}: SwarmCloud reaches nothing until GitHub is connected.`} />
  }
  const repos = chosenRepos(doc)
  const orgs = stepOf(doc, 'orgs_enabled')
  const chosen = stepOf(doc, 'repos_chosen')
  const enabledRaw = (orgs?.evidence as { enabled?: unknown } | undefined)?.enabled
  const enabled = Array.isArray(enabledRaw) ? enabledRaw.filter((o): o is string => typeof o === 'string') : null
  // Each owner it may work in, with the repositories chosen under it.
  const owners = enabled ?? Array.from(new Set(repos.map((r) => r.owner)))
  const reach = owners
    .map((o) => {
      const n = repos.filter((r) => r.owner.toLowerCase() === o.toLowerCase()).length
      return `${o} (${n} repo${n === 1 ? '' : 's'})`
    })
    .join(' · ')
  const line =
    g.state === 'done'
      ? reach === ''
        ? `${repos.length} repositor${repos.length === 1 ? 'y' : 'ies'} chosen`
        : reach
      : orgs?.state !== 'done'
        ? 'Enable the accounts and orgs SwarmCloud may work in, then choose their repositories.'
        : repos.length === 0
          ? `${reach === '' ? 'An org is enabled' : `Enabled: ${reach}`}. Now choose the repositories SwarmCloud may work in, on Work › Access.`
          : reach
  const issues: OnboardingIssue[] = chosen !== null && Array.isArray(chosen.issues) ? chosen.issues : []
  return (
    <GuideRow
      n={2}
      title="Choose what it reaches"
      state={g.state}
      line={line}
      actions={
        <UrNavButton to={ACCESS_PAGE} go={go} size="sm" kind={g.state === 'done' ? 'secondary' : 'primary'}>
          Manage in Access
        </UrNavButton>
      }
    >
      {g.state !== 'done' && <Owners doc={doc} onEnabled={read.reload} />}
      {g.state === 'done' && <Owners doc={doc} />}
      {issues.map((i) => (
        <p key={`${i.code}:${i.repository ?? i.owner ?? ''}`} className="ur-small ur-bad">
          {i.copy}
        </p>
      ))}
    </GuideRow>
  )
}

/** What one Check access answered, as the step draws it. */
type CheckView = { kind: 'grant'; repository: string; data: AccessVerifyResponse } | { kind: 'token' } | { kind: 'refused'; error: ApiError }

/**
 * WHAT INDEXING USES, UNDER TRY IT (lane INDEX-CRED, owner report
 * 2026-10-13: "it says all tests pass for /repositories/tokens but it can't
 * index the repo in my personal workspace?"). Try it checks the access a
 * person's own tasks use; an index run is the tenant's and clones with the
 * credential `GET /v1/repositories` names (`index_credential`), so this says
 * which one, and the chosen repository's last index result with its reason
 * (`index.last_failure`) -- a passing check beside a failing index is
 * explained, never left to look like the same thing.
 */
function IndexingNote({ repo, registered }: { repo: ChosenRepo | null; registered: RepoRecord[] | null }) {
  if (repo === null || registered === null) return null
  const record = registered.find((r) => r.repo_id === repo.repo_id) ?? null
  const words = indexCredentialWords(record?.index_credential ?? registered[0]?.index_credential ?? null)
  const failure = record?.index.last_failure ?? null
  return (
    <div className="ur-small ur-indexing" data-testid="indexing-note">
      <p>
        Indexing uses: {words === null ? <Dash why="Not served: no repository is registered for indexing" /> : <b className={words.startsWith('none') ? 'ur-bad' : undefined}>{words}</b>}
        {words !== null && '. Try it checks the access your own agents use; indexing clones with this.'}
      </p>
      <p>
        {record === null ? (
          <>{repo.repository} is not registered for indexing.</>
        ) : failure !== null ? (
          <span className="ur-bad" data-class={failure.kind}>
            Last index of {repo.repository} failed{failure.at !== null && <> {timeAgo(failure.at)}</>}: {failure.cause}
            {record.index.backoff !== null && <> Polling stopped; Index now retries.</>}
          </span>
        ) : record.index.last_indexed_at !== null ? (
          <>
            Last index of {repo.repository}: <b>indexed {timeAgo(record.index.last_indexed_at)}</b>.
          </>
        ) : (
          <>{repo.repository} has not been indexed yet.</>
        )}
      </p>
    </div>
  )
}

function TryStep({ doc, read, go }: { doc: OnboardingDoc; read: UrRead<OnboardingDoc>; go: (to: string) => void }) {
  const g = guideState(doc, ['access_verified'])
  const repos = chosenRepos(doc)
  const [pick, setPick] = useState('')
  const [busy, setBusy] = useState(false)
  const [check, setCheck] = useState<CheckView | null>(null)
  // What indexing uses and how each repository last indexed (INDEX-CRED).
  const indexed = useUrRead(loadRepositories, 'try-it:repositories')
  const registered = indexed.state.status === 'ok' || indexed.state.status === 'stale' ? indexed.state.data : indexed.state.status === 'empty' ? [] : null
  if (g.state === 'waiting') {
    return (
      <GuideRow
        n={3}
        title="Try it"
        state="waiting"
        line={`After step ${g.after}: there is no repository to try yet.`}
      />
    )
  }
  const step = stepOf(doc, 'access_verified')
  const ev = (step?.evidence ?? {}) as Record<string, unknown>
  const repo = repos.find((r) => r.repo_id === pick) ?? repos[0] ?? null
  // An App connection's repositories are its grants, verified one by one
  // (`POST /v1/access/grants/{repo_id}/verify`); a stored token is re-probed
  // whole (`POST /v1/git-tokens/{id}/verify`). Both routes already exist.
  const viaGrants = ev.source === 'grants' || connectionOf(doc).kind === 'app'
  const tokenId = str(ev.token_id) ?? str(stepOf(doc, 'github_connected')?.evidence?.token_id)

  async function checkAccess() {
    if (repo === null) return
    setBusy(true)
    setCheck(null)
    if (viaGrants) {
      const res = await verifyAccessGrant(repo.repo_id)
      setBusy(false)
      if (res.status === 'error') setCheck({ kind: 'refused', error: res.error })
      else if (res.status === 'ok' && res.data !== null) setCheck({ kind: 'grant', repository: repo.repository, data: res.data })
    } else if (tokenId !== null) {
      const res = await verifyGitToken(tokenId)
      setBusy(false)
      setCheck(res.status === 'error' ? { kind: 'refused', error: res.error } : { kind: 'token' })
    } else {
      setBusy(false)
    }
    read.reload()
  }

  function runTestTask() {
    if (repo === null || !OWNER_REPO.test(repo.repository)) return
    prefillTask({ profile: 'claude-code', prompt: TEST_TASK_PROMPT, repositoryUrl: `https://github.com/${repo.repository}.git` })
    go(SUBMIT_TASK)
  }

  const rows = Array.isArray(ev.repositories) ? (ev.repositories as Record<string, unknown>[]) : []
  const count = (r: string) => rows.filter((x) => x?.result === r).length
  const line =
    repos.length === 0
      ? 'Choose a repository in step 2 first.'
      : g.state === 'done'
        ? 'Every chosen repository passed its clone, push and pull request checks.'
        : rows.length > 0
          ? `${count('passed')} passed${count('failed') > 0 ? ` · ${count('failed')} failed` : ''}${count('pending') > 0 ? ` · ${count('pending')} not checked yet` : ''}. Check a repository, or run a harmless test agent in it.`
          : 'Check a repository, or run a harmless test agent in it.'

  return (
    <GuideRow
      n={3}
      title="Try it"
      state={g.state}
      line={line}
      actions={
        repos.length === 0 ? null : (
          <>
            <select className="ur-select" aria-label="Repository to check" value={repo?.repo_id ?? ''} onChange={(e) => setPick(e.target.value)}>
              {repos.map((r) => (
                <option key={r.repo_id} value={r.repo_id}>
                  {r.repository}
                </option>
              ))}
            </select>
            <Button size="sm" kind={g.state === 'done' ? 'secondary' : 'primary'} busy={busy} disabled={!viaGrants && tokenId === null} onClick={() => void checkAccess()}>
              Check access
            </Button>
            <Button size="sm" disabled={repo === null || !OWNER_REPO.test(repo.repository)} onClick={runTestTask}>
              Run a test agent
            </Button>
          </>
        )
      }
    >
      {check?.kind === 'grant' &&
        (check.data.passed ? (
          <p className="ur-small" role="status">
            {check.repository}: clone, push and pull request checks passed.
          </p>
        ) : (
          <div role="status">
            {(check.data.failures ?? []).map((f) => (
              <p key={`${f.check}:${f.code}`} className="ur-small ur-bad">
                {f.copy}
              </p>
            ))}
            {(check.data.failures ?? []).length === 0 && <p className="ur-small ur-bad">{check.repository}: a check did not pass.</p>}
          </div>
        ))}
      {check?.kind === 'token' && (
        <p className="ur-small" role="status">
          SwarmCloud re-checked your token; the step above shows what it found.
        </p>
      )}
      {check?.kind === 'refused' && <Refusal error={check.error} title="Access was not checked" />}
      <IndexingNote repo={repo} registered={registered} />
      {step !== null &&
        g.state === 'failed' &&
        (Array.isArray(step.issues) ? step.issues : []).map((i) => (
          <p key={`${i.code}:${i.repository ?? ''}`} className="ur-small ur-bad">
            {i.copy}
          </p>
        ))}
    </GuideRow>
  )
}

// ---------------------------------------------------------------------------
// The callback page, /onboarding/github/callback
// ---------------------------------------------------------------------------

/** What the address bar carried, reduced to the one exchange it starts. */
type Callback = { kind: 'missing' } | { kind: 'exchange'; answer: Promise<Result<GitHubExchangeResponse>> }

/** GitHub's `error` as the exchange accepts it (`ExchangeBody.error`, `^[a-z_]{1,64}$`). */
const GITHUB_ERROR = /^[a-z_]{1,64}$/

/**
 * ONE EXCHANGE PER PAGE LOAD. The state is single use -- the server spends it
 * before it trades the code -- so a second post of the same pair is refused
 * `AUTHORISATION_EXPIRED` and would overwrite a success with a failure. React
 * strict mode runs every effect twice in development, and a remount reads a
 * bar this module has already scrubbed, so the post is held here, at module
 * level, not in a component: every mount after the first awaits the same
 * answer.
 */
let taken: Callback | null = null

/**
 * Read `code`, `state` and `error` off the address bar ONCE, rewrite the bar
 * without them, and start the exchange. The values live only in the closure
 * of that one post.
 */
export function takeGitHubCallback(): Callback {
  if (taken !== null) return taken
  const q = new URLSearchParams(window.location.search)
  const state = q.get('state')
  const code = q.get('code')
  const error = q.get('error')
  // Before anything else runs: the bar is what a person copies, the history
  // entry is what Back restores, and neither may keep a code.
  window.history.replaceState(window.history.state, '', window.location.pathname)
  if (state === null || state === '') {
    taken = { kind: 'missing' }
    return taken
  }
  // GitHub sends `error` (and an `error_description` that is not forwarded)
  // instead of a code when the person cancelled or the App refused. A code
  // GitHub did not send is posted as nothing, and the server says so.
  const body: GitHubExchangeBody =
    code !== null && code !== ''
      ? { state, code }
      : error !== null && error !== ''
        ? { state, error: GITHUB_ERROR.test(error) ? error : 'error' }
        : { state }
  taken = { kind: 'exchange', answer: exchangeGitHub(body) }
  return taken
}

type CallbackView =
  | { kind: 'posting' }
  | { kind: 'missing' }
  | { kind: 'ok'; data: GitHubExchangeResponse | null }
  | { kind: 'error'; error: ApiError }

/**
 * THE CALLBACK PAGE. GitHub sends the browser here with `?code=&state=` (or
 * `?error=&state=`) after the person authorises the App; the page posts them
 * once and answers "Connected as @<login>" with the orgs the connection
 * reaches, or the refusal with Try again, which starts a fresh authorisation.
 */
export function GitHubCallbackScreen({ go }: { go: (to: string) => void }) {
  // The page prints its own state, not a read age (a POST is not a read).
  useClaimPageAge(true)
  const [view, setView] = useState<CallbackView>({ kind: 'posting' })

  useEffect(() => {
    const cb = takeGitHubCallback()
    if (cb.kind === 'missing') {
      setView({ kind: 'missing' })
      return
    }
    let live = true
    void cb.answer.then((res) => {
      if (!live) return
      if (res.status === 'error') setView({ kind: 'error', error: res.error })
      else setView({ kind: 'ok', data: res.status === 'ok' ? res.data : null })
    })
    return () => {
      live = false
    }
  }, [])

  return (
    <div className="ur-page ur-gh-page">
      <PageHead title="Connect GitHub" />
      {view.kind === 'posting' ? (
        <Card className="ur-gh">
          <p className="ur-sub" role="status">
            Finishing the connection with GitHub…
          </p>
        </Card>
      ) : view.kind === 'ok' ? (
        <Connected data={view.data} go={go} />
      ) : (
        <Card className="ur-gh">
          {view.kind === 'missing' ? (
            <Banner tone="warn" title="Nothing to finish here" role="status">
              <p className="ur-small">
                This page finishes a connection GitHub sends you back from, and this visit carried none. Press Connect
                GitHub to start one.
              </p>
            </Banner>
          ) : (
            <Refusal error={view.error} title="GitHub was not connected" />
          )}
          <div className="ur-gh-acts">
            <ConnectButton label={view.kind === 'missing' ? 'Connect GitHub' : 'Try again'} />
            <UrNavButton to={GT_PAGE} go={go} size="sm">
              Back to Git tokens
            </UrNavButton>
          </div>
        </Card>
      )}
    </div>
  )
}

function Connected({ data, go }: { data: GitHubExchangeResponse | null; go: (to: string) => void }) {
  const doc = useUrRead(loadOnboarding, 'onboarding')
  const login = data?.connection?.forge_login ?? null
  return (
    <Card className="ur-gh" title={login !== null ? `Connected as @${login}` : 'Connected'} action={<UrRefresh reads={[doc]} />}>
      <p className="ur-sub" role="status">
        SwarmCloud now acts as {login !== null ? <b>@{login}</b> : 'your GitHub account'} through the SwarmCloud GitHub
        App. The token went from GitHub to Secret Manager; it is never shown here.
      </p>
      <UrRegion state={doc.state} route="GET /v1/onboarding" what="The orgs this connection reaches" onRetry={doc.reload} lines={1} plural>
        {(d) => <Owners doc={d} />}
      </UrRegion>
      <div className="ur-gh-acts">
        <UrNavButton to={GT_PAGE} go={go} kind="primary" size="sm">
          Back to Git tokens
        </UrNavButton>
        <UrNavButton to={ACCESS_PAGE} go={go} size="sm">
          Choose orgs in Access
        </UrNavButton>
      </div>
    </Card>
  )
}
