import { useEffect, useState, type ReactNode } from 'react'
import { authorizeGitHub, disconnectGitHub, exchangeGitHub, loadOnboarding } from './api'
import { Banner, Button, Card, Chip, Dash, ToneMark } from './components'
import { errorHeading, type ApiError, type Result } from './fetch'
import { GT_PAGE, UrNavButton, UrRefresh, UrRegion, useUrRead, type UrRead } from './RepositoriesParts'
import { PageHead, useClaimPageAge } from './Shell'
import type {
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
 *   * `GitHubConnectCard`, on Work › Repositories › Git tokens -- where the
 *     console shows git access today -- reads `GET /v1/onboarding` for the
 *     connection, starts one (`POST .../authorize`, then the browser goes to
 *     GitHub) and ends one (`DELETE /v1/onboarding/github`, behind a confirm);
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

/**
 * The accounts and orgs the connection reaches, from `orgs_enabled`. The
 * probe reads them after the token is stored, so straight after connecting
 * they may not be read yet -- said so, never drawn as "no orgs".
 */
function Owners({ doc }: { doc: OnboardingDoc }) {
  const step = stepOf(doc, 'orgs_enabled')
  const ev = (step?.evidence ?? {}) as Partial<OrgsEnabledEvidence>
  const owners = Array.isArray(ev.owners) ? ev.owners : []
  if (step === null || step.state === 'todo' || owners.length === 0) {
    return (
      <p className="ur-hint">
        Orgs <Dash why="Not read yet" /> SwarmCloud reads the accounts and orgs this connection reaches when it verifies
        the new token; they appear here once it has.
      </p>
    )
  }
  const issues: OnboardingIssue[] = Array.isArray(step.issues) ? step.issues : []
  return (
    <div className="ur-gh-orgs">
      <p className="ur-sub">Reaches</p>
      <ul aria-label="Accounts and orgs this connection reaches">
        {owners.map((o) => (
          <li key={o.owner}>
            <Chip tone={o.reach === 'reachable' ? undefined : 'bad'} title={o.owner_type === 'User' ? 'Your account' : 'An org'}>
              {o.owner}
              {o.reach !== 'reachable' ? ` · ${REACH_WORD[o.reach] ?? o.reach}` : ''}
            </Chip>
          </li>
        ))}
      </ul>
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
    </div>
  )
}

// ---------------------------------------------------------------------------
// The card, on Git tokens
// ---------------------------------------------------------------------------

/**
 * CONNECT GITHUB, the card (onboarding.md §4, Connect A: one button for the
 * App). It reads its own state from `GET /v1/onboarding` and reloads it after
 * a disconnect; `onChange` lets the page re-read its token records too.
 */
export function GitHubConnectCard({ onChange }: { onChange?: () => void }) {
  const doc = useUrRead(loadOnboarding, 'onboarding')
  return (
    <Card className="ur-gh" title="Connect GitHub" action={<Chip>acts as you</Chip>}>
      <UrRegion state={doc.state} route="GET /v1/onboarding" what="Your GitHub connection" onRetry={doc.reload} lines={2}>
        {(d) => <CardBody doc={d} read={doc} onChange={onChange} />}
      </UrRegion>
    </Card>
  )
}

function CardBody({ doc, read, onChange }: { doc: OnboardingDoc; read: UrRead<OnboardingDoc>; onChange?: () => void }) {
  const view = connectionOf(doc)
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const [done, setDone] = useState<GitHubDisconnectResponse | null>(null)

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
    setDone(res.status === 'ok' ? res.data : null)
    read.reload()
    onChange?.()
  }

  const who = (login: string | null) => (login === null ? <Dash why="The account was not read yet" /> : <b>@{login}</b>)

  return (
    <div className="ur-gh-body">
      {done !== null && (
        <Banner tone={done.github_revoked ? 'info' : 'warn'} title="GitHub was disconnected" role="status">
          <p className="ur-small">{done.github}</p>
        </Banner>
      )}
      {view.kind === 'app' ? (
        <>
          <p className="ur-cmeta">
            <span>Connected as {who(view.login)}</span>
            <span>through the SwarmCloud GitHub App</span>
            {view.failed !== null && <ToneMark tone="bad">access ended</ToneMark>}
          </p>
          {view.failed !== null && view.failed.copy !== null && <p className="ur-small ur-bad">{view.failed.copy}</p>}
          <Owners doc={doc} />
        </>
      ) : view.kind === 'own-token' ? (
        <p className="ur-sub">
          Your own stored token ({who(view.login)}) is what your tasks use. Connect GitHub to act through the SwarmCloud
          App instead: no token to paste, and it renews itself.
        </p>
      ) : view.kind === 'tenant' ? (
        <p className="ur-sub">
          Your tasks act through the tenant token ({who(view.login)}), not as you. Connect your GitHub account so
          SwarmCloud clones, pushes and opens pull requests as you.
        </p>
      ) : (
        <p className="ur-sub">
          No GitHub account is connected. Connect yours so SwarmCloud clones, pushes and opens pull requests as you.
        </p>
      )}

      {confirming ? (
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
            repositories you granted. Tasks you submit afterwards wait until you connect again.
          </p>
        </Banner>
      ) : (
        <div className="ur-gh-acts">
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
        </div>
      )}
      {refused !== null && <Refusal error={refused} title="GitHub was not disconnected" />}
      <p className="ur-hint">
        Checked {doc.derived_at ? timeAgo(doc.derived_at) : <Dash why="The read carried no time" />}. No token passes
        through this page: GitHub hands it to SwarmCloud, which stores it in Secret Manager.
      </p>
    </div>
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
      </div>
    </Card>
  )
}
