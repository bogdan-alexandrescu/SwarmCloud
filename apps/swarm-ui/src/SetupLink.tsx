/**
 * ADMIN › SETUP'S ONE-TIME LINK (lane DAYONE-SETUP-UI-5B; part of #2688;
 * docs/design/day-one-plan.md §5.2, §5.3; Day One audit finding 13).
 *
 * swarm-api's `POST /v1/admin/setup/links` mints `/admin/setup/link/<token>`
 * (`setup_links.LINK_FORM`) for the one person it names, and this page is
 * where that link lands. It is outside the admin gate: the person is a Claude
 * subscription's owner or a team's git admin, rarely a platform admin.
 *
 * ONE CLICK OPENS THE LINK. `POST /v1/setup/links/redeem` with the token in
 * the BODY, never in a URL. A `pool-account` link is consumed by that call and
 * answers its label and the first team's workspace; the page then runs the
 * same sign-in Capacity › Accounts runs (`beginAccountSignIn`, then
 * `finishAccountSignIn`, api.ts) in that workspace, by selecting it as the
 * tab's workspace (`chooseTenant`), which is how `X-Swarm-Tenant` is sent and
 * so where the account lands. A `team-git` or `addon-secret` link is refused
 * without its value and stays unused, so the page asks for the value and opens
 * the link again with it; swarm-api writes it straight into its one secret.
 *
 * NOT ON REDEEM BY ITSELF. Opening is a click, not a mount effect: a link is
 * single use, and a preview, a prefetch or a double effect must not spend it.
 *
 * A REDEEMED POOL LINK SURVIVES A RELOAD. The token is gone from the bar and
 * the link is spent, so the label and workspace it answered (neither a
 * secret) are kept in this tab's sessionStorage until the account is signed
 * in. The token itself is never stored.
 */
import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { USE_FIXTURES, beginAccountSignIn, finishAccountSignIn } from './api'
import { Banner, Button, Card } from './components'
import { chooseTenant, route, write, type ApiError, type Result } from './fetch'
import { PageHead } from './Shell'
import { pastedCodeHint, type AccountAuthorization } from './types'
import './styles/keys.css'

export const REDEEM_ROUTE = '/v1/setup/links/redeem'
/** This tab's redeemed pool link: `{label, tenant}`, never the token. */
export const PENDING_KEY = 'swarm.setup-link.pool'

interface PoolLink {
  label: string
  tenant: string
}
type Opened =
  | { kind: 'pool-account'; link: PoolLink }
  | { kind: 'team-git' | 'addon-secret'; label: string | null; secret: string | null }

/** `POST /v1/setup/links/redeem`, the token (and a form kind's value) in the body. */
export async function redeemSetupLink(token: string, value?: string): Promise<Result<unknown>> {
  if (USE_FIXTURES) {
    return { status: 'ok', fetchedAt: Date.now(), data: { kind: 'pool-account', label: 'claude-1', tenant: 'eng' } }
  }
  return write(route(REDEEM_ROUTE), 'POST', value === undefined ? { token } : { token, value })
}

function openedFrom(data: unknown): Opened | null {
  if (typeof data !== 'object' || data === null) return null
  const d = data as Record<string, unknown>
  const label = typeof d.label === 'string' ? d.label : null
  if (d.kind === 'pool-account') {
    if (label === null || typeof d.tenant !== 'string' || d.tenant === '') return null
    return { kind: 'pool-account', link: { label, tenant: d.tenant } }
  }
  if (d.kind === 'team-git' || d.kind === 'addon-secret') {
    return { kind: d.kind, label, secret: typeof d.secret === 'string' ? d.secret : null }
  }
  return null
}

/**
 * Why a link did not open, in one plain line, and what to ask the admin for.
 * swarm-api answers an unknown, used and expired token alike (one 404, so a
 * probe learns nothing), and a token too short or long to be one is a 422.
 */
export function linkRefusal(e: ApiError): { line: string; retry: boolean } {
  if (e.httpStatus === 404 || (e.httpStatus === 422 && valueKind(e) === null)) {
    return {
      line: 'This link has already been used, has expired (a link lasts 24 hours) or was never issued: ask the admin who sent it for a new link from Admin › Setup.',
      retry: false,
    }
  }
  if (e.httpStatus === 403 && e.code === 'forbidden') {
    return {
      line: 'This link was made for someone else and is still unused: sign in to the console as the person it names, or ask the admin for a link in your own name.',
      retry: false,
    }
  }
  return {
    line: `The link could not be opened (${e.message.replace(/[.\s]+$/, '')}); it is unused unless the platform said otherwise, so try again, and if it keeps failing ask the admin who sent it.`,
    retry: true,
  }
}

/** The form kind a 422 names when its link needs a value, or null. */
function valueKind(e: ApiError): 'team-git' | 'addon-secret' | null {
  if (e.httpStatus !== 422) return null
  const m = /^a (team-git|addon-secret) link is opened with its value/.exec(e.message)
  return m === null ? null : (m[1] as 'team-git' | 'addon-secret')
}

function readPending(): PoolLink | null {
  try {
    const raw = globalThis.sessionStorage?.getItem(PENDING_KEY) ?? null
    if (raw === null) return null
    const v = JSON.parse(raw) as Partial<PoolLink>
    return typeof v.label === 'string' && typeof v.tenant === 'string' ? { label: v.label, tenant: v.tenant } : null
  } catch {
    return null
  }
}

function writePending(link: PoolLink | null): void {
  try {
    if (link === null) globalThis.sessionStorage?.removeItem(PENDING_KEY)
    else globalThis.sessionStorage?.setItem(PENDING_KEY, JSON.stringify(link))
  } catch {
    /* refused: the sign-in still runs in this page; only a reload loses it */
  }
}

export function SetupLinkScreen({ token }: { token: string | null }) {
  const [pool, setPool] = useState<{ link: PoolLink; startNow: boolean } | null>(() => {
    if (token !== null) return null
    const pending = readPending()
    return pending === null ? null : { link: pending, startNow: false }
  })
  return (
    <div className="pk-page">
      <PageHead title="Your setup link" />
      {pool !== null ? (
        <PoolSignIn link={pool.link} startNow={pool.startNow} />
      ) : token === null ? (
        <Card title="Open your link">
          <p className="pk-lead">
            This page opens a one-time link an admin made for you in Admin &rsaquo; Setup. The link is read once, when
            you open it, and is not kept in the address: open the link from the admin&rsquo;s message again.
          </p>
        </Card>
      ) : token === '' ? (
        <Banner tone="bad" title="This is not a whole setup link." role="alert">
          Only part of it reached this page: open the link from the admin&rsquo;s message again, or ask them for a new
          one.
        </Banner>
      ) : (
        <OpenLink token={token} onPool={(link) => setPool({ link, startNow: true })} />
      )}
    </div>
  )
}

function OpenLink({ token, onPool }: { token: string; onPool: (link: PoolLink) => void }) {
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<{ line: string; retry: boolean } | null>(null)
  const [needs, setNeeds] = useState<'team-git' | 'addon-secret' | null>(null)
  const [written, setWritten] = useState<Opened | null>(null)

  async function open() {
    if (busy) return
    setBusy(true)
    setRefused(null)
    const r = await redeemSetupLink(token)
    setBusy(false)
    if (r.status === 'error') {
      const kind = valueKind(r.error)
      if (kind !== null) setNeeds(kind)
      else setRefused(linkRefusal(r.error))
      return
    }
    const opened = r.status === 'ok' || r.status === 'stale' ? openedFrom(r.data) : null
    if (opened === null) {
      setRefused({ line: 'The platform opened the link but did not say what it is for: ask the admin who sent it for a new link.', retry: false })
      return
    }
    if (opened.kind === 'pool-account') {
      // The link is spent from here: keep what it answered for a reload.
      writePending(opened.link)
      onPool(opened.link)
      return
    }
    setWritten(opened)
  }

  if (written !== null && written.kind !== 'pool-account') return <Written opened={written} />
  if (needs !== null) return <ValueForm token={token} kind={needs} onWritten={setWritten} />
  if (refused !== null && !refused.retry) {
    return <Banner tone="bad" title={refused.line} role="alert" />
  }
  return (
    <Card title="Open your setup link">
      <p className="pk-lead">
        An admin made this link for you in Admin &rsaquo; Setup. It works once: opening it signs your Claude
        subscription into the team&rsquo;s pool, or asks for the one value it was made for.
      </p>
      {refused !== null && <Banner tone="bad" title={refused.line} role="alert" />}
      <div className="pk-form-row">
        <Button kind="primary" busy={busy ? 'Opening…' : false} onClick={() => void open()}>
          Open the link
        </Button>
      </div>
    </Card>
  )
}

const VALUE_WORDS: Record<'team-git' | 'addon-secret', { title: string; field: string }> = {
  'team-git': { title: "Store the team's GitHub token", field: "The team's GitHub token" },
  'addon-secret': { title: "Store the add-on's secret", field: "The add-on's secret" },
}

function ValueForm({ token, kind, onWritten }: { token: string; kind: 'team-git' | 'addon-secret'; onWritten: (o: Opened) => void }) {
  const id = useId()
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<string | null>(null)
  const words = VALUE_WORDS[kind]

  async function send(e: FormEvent) {
    e.preventDefault()
    if (busy || value.trim() === '') return
    setBusy(true)
    setRefused(null)
    const r = await redeemSetupLink(token, value.trim())
    setBusy(false)
    if (r.status === 'error') {
      setRefused(linkRefusal(r.error).line)
      return
    }
    setValue('')
    const opened = r.status === 'ok' || r.status === 'stale' ? openedFrom(r.data) : null
    onWritten(opened ?? { kind, label: null, secret: null })
  }

  return (
    <Card title={words.title}>
      <form className="pk-form" autoComplete="off" onSubmit={(e) => void send(e)}>
        <p className="pk-lead">
          The value goes straight from this page into its one secret in Secret Manager. It is never shown again, and
          the link is used only once it is stored.
        </p>
        <label htmlFor={`${id}-v`}>{words.field}</label>
        <input id={`${id}-v`} className="c-inp" type="password" autoComplete="off" spellCheck={false} value={value}
          onChange={(e) => setValue(e.target.value)} />
        {refused !== null && <Banner tone="bad" title={refused} role="alert" />}
        <div className="pk-form-row">
          <Button kind="primary" type="submit" busy={busy ? 'Storing…' : false} disabled={value.trim() === ''}>
            Store it
          </Button>
        </div>
      </form>
    </Card>
  )
}

function Written({ opened }: { opened: Opened }) {
  const where = opened.kind !== 'pool-account' && opened.secret !== null ? ` in ${opened.secret}` : ''
  return (
    <Banner tone="info" title={`Stored${where}.`} role="status">
      This link is now used. Admin &rsaquo; Setup sees the value at its next read; you can close this page.
    </Banner>
  )
}

type SignIn =
  | { kind: 'idle' }
  | { kind: 'starting' }
  | { kind: 'start_failed'; error: ApiError }
  | { kind: 'open'; auth: AccountAuthorization; code: string; sending: boolean; failed: ApiError | null }
  | { kind: 'done' }

/**
 * The account sign-in Capacity › Accounts runs, in the link's workspace:
 * authorize, open the provider's page, paste the code it shows, exchange.
 */
function PoolSignIn({ link, startNow }: { link: PoolLink; startNow: boolean }) {
  const id = useId()
  const [at, setAt] = useState<SignIn>({ kind: 'idle' })
  const started = useRef(false)

  async function start() {
    setAt({ kind: 'starting' })
    // THE WORKSPACE IS THE LINK'S, sent as `X-Swarm-Tenant` by every request
    // this tab makes (fetch.ts `chooseTenant`): the sign-in files the account
    // into the workspace the header names.
    chooseTenant(link.tenant)
    const r = await beginAccountSignIn({ label: link.label, lend_to: [] })
    if (r.status === 'ok' || r.status === 'stale') setAt({ kind: 'open', auth: r.data, code: '', sending: false, failed: null })
    else if (r.status === 'error') setAt({ kind: 'start_failed', error: r.error })
  }

  useEffect(() => {
    if (!startNow || started.current) return
    started.current = true
    void start()
    // Once, on the click that opened the link.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [startNow])

  async function finish(e: FormEvent) {
    e.preventDefault()
    if (at.kind !== 'open' || at.sending || at.code.trim() === '') return
    const open = at
    setAt({ ...open, sending: true, failed: null })
    chooseTenant(link.tenant)
    const r = await finishAccountSignIn({ state: open.auth.state, code: open.code.trim() })
    if (r.status === 'ok' || r.status === 'stale') {
      writePending(null)
      setAt({ kind: 'done' })
    } else if (r.status === 'error') {
      setAt({ ...open, sending: false, failed: r.error })
    }
  }

  const facts = (
    <ul className="ctl-facts">
      <li className="ctl-fact">
        <b>account</b>
        <span className="mono">{link.label}</span>
      </li>
      <li className="ctl-fact">
        <b>workspace</b>
        <span className="mono">{link.tenant}</span>
      </li>
    </ul>
  )

  if (at.kind === 'done') {
    return (
      <Card title="Your Claude account is signed in">
        {facts}
        <Banner tone="info" title={`${link.label} is in the ${link.tenant} workspace's Claude pool.`} role="status">
          Admin &rsaquo; Setup sees it at its next read; you can close this page.
        </Banner>
      </Card>
    )
  }

  if (at.kind === 'idle' || at.kind === 'starting' || at.kind === 'start_failed') {
    return (
      <Card title="Sign in your Claude account">
        {facts}
        <p className="pk-lead">
          Your link is open. Sign in with the Claude subscription the admin asked you for; the account lands in the{' '}
          {link.tenant} workspace&rsquo;s pool under the name {link.label}.
        </p>
        {at.kind === 'start_failed' && (
          <Banner tone="bad" title={`The sign-in did not start: ${at.error.message}`} role="alert">
            Your link is already used, and this page keeps what it opened: start the sign-in again.
          </Banner>
        )}
        <div className="pk-form-row">
          <Button kind="primary" busy={at.kind === 'starting' ? 'Starting…' : false} onClick={() => void start()}>
            {at.kind === 'start_failed' ? 'Start the sign-in again' : 'Start the sign-in'}
          </Button>
        </div>
      </Card>
    )
  }

  const hint = pastedCodeHint(at.code)
  return (
    <Card title="Sign in your Claude account">
      {facts}
      <form className="pk-form" autoComplete="off" onSubmit={(e) => void finish(e)}>
        <p className="pk-lead">
          1. Open Claude&rsquo;s sign-in page and sign in with the subscription the admin asked you for.{' '}
          <a href={at.auth.authorize_url} target="_blank" rel="noopener noreferrer">
            Open the Claude sign-in page
          </a>
        </p>
        <p className="pk-lead">2. Claude then shows a code. Paste it here.</p>
        <label htmlFor={`${id}-code`}>The code Claude shows</label>
        <input id={`${id}-code`} className="c-inp" autoComplete="off" spellCheck={false} value={at.code}
          onChange={(e) => setAt({ ...at, code: e.target.value })} />
        {hint !== null && <p className="ur-small">{hint}</p>}
        {at.failed !== null && (
          <Banner tone="bad" title={`The sign-in did not finish: ${at.failed.message}`} role="alert"
            actions={<Button size="sm" onClick={() => void start()}>Start over</Button>}>
            Paste the code again, or start over for a fresh sign-in page.
          </Banner>
        )}
        <div className="pk-form-row">
          <Button kind="primary" type="submit" busy={at.sending ? 'Signing in…' : false} disabled={at.code.trim() === ''}>
            Finish the sign-in
          </Button>
        </div>
      </form>
    </Card>
  )
}
