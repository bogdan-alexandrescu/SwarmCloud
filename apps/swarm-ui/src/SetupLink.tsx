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
 * EXCEPT IN THE INSTALLER'S OWN TAB (audit 4d, F5). When the link's owner is
 * the admin signed in on Admin › Setup, that page opens the link itself in a
 * tab named OWN_SIGN_IN_WINDOW: the name is set by the console's own click,
 * never by a mail preview or a prefetch, so that tab opens the link at once,
 * once, and clears the name.
 *
 * A WORKSPACE NOT READY YET (audit 4d, F6). swarm-api answers 409 and leaves
 * the link unused while its workspace is not `ready`; the page says so and
 * opens it again by itself every REDEEM_RETRY_MS. An account sign-in refused
 * with 403 just after (the membership answer swarm-api caches for 30 s) is
 * started again by itself a few times before it asks.
 *
 * A REDEEMED POOL LINK SURVIVES A RELOAD. The token is gone from the bar and
 * the link is spent, so the label and workspace it answered (neither a
 * secret) are kept in this tab's sessionStorage until the account is signed
 * in. The token itself is never stored.
 *
 * THE TEAMMATE'S PART, IN THREE LINES (the owner's pick for the Day One
 * console finish, 2026-11-02: the checklist beside a working panel). Beside
 * the link's own panel the page lists the person's whole part: open the link,
 * sign in their Claude account (or store the one value the link is for), and
 * that is all. Nothing is ticked by hand: each mark comes from an answer this
 * page was given. swarm-api answers a used, an expired and an unknown link
 * alike (one 404), so the first line says it no longer opens and never claims
 * which. The time left on an unused link is drawn only from an `expires_at`
 * the platform served with a refusal that kept it unused (the 409 or the
 * value form's 422); without one it reads "time left: not recorded", never a
 * guess. The styles are the checklist finish's (styles/console-finish.css).
 *
 * VOICE IT'S OPENAI API KEY (`voice-api-key`, the owner, 4 November 2026). A
 * platform admin mints it on Admin › Accounts for themself; it opens in their
 * own console session within ten minutes, once. Like the form kinds it is
 * refused without its value and stays unused, so the page asks for the key;
 * swarm-api forwards it once to the quota broker, which files it into the
 * Voice it pool, and the answer names the account, never the key.
 */
import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { USE_FIXTURES, beginAccountSignIn, finishAccountSignIn } from './api'
import { spanWords, type StepState } from './AdminSetupSteps'
import { Banner, Button, Card } from './components'
import { chooseTenant, route, write, type ApiError, type Result } from './fetch'
import { PageHead } from './Shell'
import { pastedCodeHint, type AccountAuthorization } from './types'
import './styles/keys.css'
import './styles/config.css'
import './styles/console-finish.css'

export const REDEEM_ROUTE = '/v1/setup/links/redeem'
/** This tab's redeemed pool link: `{label, tenant}`, never the token. */
export const PENDING_KEY = 'swarm.setup-link.pool'
/** The name of the tab Admin › Setup opens the installer's own link in: open it at once. */
export const OWN_SIGN_IN_WINDOW = 'swarm-own-claude-sign-in'
/** How often a link whose workspace is not ready yet (409) is opened again. */
export const REDEEM_RETRY_MS = 10_000
/** How often, and how many times, a sign-in refused while the workspace's membership settles (403) starts again. */
export const SIGN_IN_RETRY_MS = 10_000
export const SIGN_IN_RETRIES = 6

interface PoolLink {
  label: string
  tenant: string
}
/** The kinds opened with a value typed on this page. */
type FormKind = 'team-git' | 'addon-secret' | 'voice-api-key'
type Opened =
  | { kind: 'pool-account'; link: PoolLink }
  | { kind: 'team-git' | 'addon-secret'; label: string | null; secret: string | null }
  | { kind: 'voice-api-key'; label: string | null; account: string | null }

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
  if (d.kind === 'voice-api-key') {
    return { kind: d.kind, label, account: typeof d.account_id === 'string' && d.account_id !== '' ? d.account_id : null }
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
      line: 'This link has already been used, has expired (a link lasts 24 hours; a Voice it key link ten minutes) or was never issued: ask the admin who sent it for a new link from Admin › Setup, or make a new Voice it key link on Admin › Accounts.',
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
function valueKind(e: ApiError): FormKind | null {
  if (e.httpStatus !== 422) return null
  const m = /^a (team-git|addon-secret|voice-api-key) link is opened with its value/.exec(e.message)
  return m === null ? null : (m[1] as FormKind)
}

type LinkKind = 'pool-account' | FormKind

/**
 * What the page has learned about its link from the answers it was given,
 * for the three lines beside the panel. `unused` is only ever set by an
 * answer that said so (a 409 or a value form's 422); `invalid` is the one
 * 404 for a used, an expired or an unknown link.
 */
export interface LinkPart {
  link: 'unopened' | 'unused' | 'spent' | 'invalid' | 'not-yours'
  kind: LinkKind | null
  /** When the link stops opening, as served by the platform; null when it served none. */
  expiresAt: number | null
  /** The account is signed in, or the value is stored. */
  done: boolean
}

const UNOPENED: LinkPart = { link: 'unopened', kind: null, expiresAt: null, done: false }

/** The `expires_at` a refusal that kept the link unused carried in its detail, or null. */
function expiresFrom(e: ApiError): number | null {
  const d = e.detail
  if (typeof d !== 'object' || d === null) return null
  const v = (d as Record<string, unknown>).expires_at
  if (typeof v !== 'string') return null
  const t = Date.parse(v)
  return Number.isNaN(t) ? null : t
}

interface PartLine {
  title: string
  text: string
  state: StepState
  /** Who acts on this line now; null when nobody does. */
  now: string | null
}

/** The person's part in three lines, from what the page has learned; `nowMs` is the clock the time left is read against. */
export function partLines(p: LinkPart, nowMs: number): PartLine[] {
  const left = p.expiresAt === null ? null : p.expiresAt - nowMs
  const open: PartLine =
    p.link === 'spent'
      ? { title: 'Open your link', text: 'opened; the link is now used', state: 'done', now: null }
      : p.link === 'invalid'
        ? {
            title: 'Open your link',
            text: 'this link no longer opens: it was used, it expired (a link lasts 24 hours) or was never issued, and the platform does not say which',
            state: 'failed',
            now: 'The admin who sent it: a new link',
          }
        : p.link === 'not-yours'
          ? { title: 'Open your link', text: 'made for someone else, and still unused', state: 'failed', now: 'The person it names, or the admin' }
          : p.link === 'unused'
            ? {
                title: 'Open your link',
                text: `still unused · ${left === null ? 'time left: not recorded' : left > 0 ? `${spanWords(left / 1000)} left on the link` : 'its time has run out: ask the admin who sent it for a new one'}`,
                state: 'in_progress',
                now: 'You',
              }
            : { title: 'Open your link', text: 'one click; it works once, for you only, within 24 hours of being made', state: 'todo', now: 'You' }
  const formKind = p.kind === 'team-git' || p.kind === 'addon-secret' || p.kind === 'voice-api-key' ? p.kind : null
  const acting = p.link === 'spent' || (formKind !== null && p.link === 'unused')
  const second: PartLine =
    formKind !== null
      ? {
          title: VALUE_WORDS[formKind].title,
          text:
            formKind === 'voice-api-key'
              ? p.done
                ? 'in the Voice it pool; the key is never shown again'
                : 'from this page to the account pool, once'
              : p.done
                ? 'stored in its one secret; it is never shown again'
                : 'from this page straight into its one secret',
          state: p.done ? 'done' : acting ? 'in_progress' : 'todo',
          now: !p.done && acting ? 'You' : null,
        }
      : {
          title: 'Sign in your Claude account',
          text: p.done
            ? 'signed in; the account is in the workspace’s Claude pool'
            : acting
              ? 'on Claude’s own page, opened from here; paste the code Claude shows'
              : 'on Claude’s own page, opened from here',
          state: p.done ? 'done' : acting ? 'in_progress' : 'todo',
          now: !p.done && acting ? 'You' : null,
        }
  const last: PartLine = {
    title: 'That is all',
    text: 'your platform admin sees it on their page by itself; nothing to send back',
    state: p.done ? 'done' : 'todo',
    now: null,
  }
  return [open, second, last]
}

/** A line's mark: its number in a ring, ticked once done. */
function PartMark({ n, state }: { n: number; state: StepState }) {
  return (
    <svg className={`cfg-mark is-${state}`} viewBox="0 0 20 20" aria-hidden>
      <circle cx={10} cy={10} r={8.5} />
      {state === 'done' ? (
        <path d="m6 10.4 2.7 2.7L14.2 7.6" />
      ) : (
        <text x={10} y={14} textAnchor="middle" fontSize={11} stroke="none" fill="currentColor">
          {n}
        </text>
      )}
    </svg>
  )
}

/** The person's part, beside the link's panel: three lines, each with its mark and who acts on it now. */
export function YourPart({ part, nowMs }: { part: LinkPart; nowMs: number }) {
  const ls = partLines(part, nowMs)
  return (
    <nav className="cfc-list" aria-label="Your part">
      <p className="cfc-kicker">Your part of the setup · {ls.filter((l) => l.state === 'done').length} of 3 done</p>
      <ol>
        {ls.map((l, j) => (
          <li key={j} className={`cfc-item is-${l.state}`} data-part={j + 1} data-state={l.state}
            aria-current={l.state === 'in_progress' ? 'step' : undefined}>
            <div className="cfc-pick" style={{ cursor: 'default' }}>
              <PartMark n={j + 1} state={l.state} />
              <span className="cfc-item-main">
                <span className="cfc-item-h">
                  <b>{l.title}</b>
                </span>
                <span className="cfc-item-now">{l.text}</span>
                {l.now !== null && (
                  <span className="cfc-item-who">
                    <span className={`cfc-who ${l.now === 'You' ? 'is-you' : 'is-teammate'}`}>{l.now}</span>
                  </span>
                )}
              </span>
            </div>
          </li>
        ))}
      </ol>
    </nav>
  )
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
  const [auto] = useState(() => token !== null && token !== '' && globalThis.window?.name === OWN_SIGN_IN_WINDOW)
  // A pool link redeemed earlier in this tab is spent: the first line is done.
  const [part, setPart] = useState<LinkPart>(() =>
    pool === null ? UNOPENED : { link: 'spent', kind: 'pool-account', expiresAt: null, done: false },
  )
  const learn = (next: Partial<LinkPart>) => setPart((p) => ({ ...p, ...next }))
  const panel =
    pool !== null ? (
      <PoolSignIn link={pool.link} startNow={pool.startNow} onSignedIn={() => learn({ done: true })} />
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
      <OpenLink token={token} auto={auto} onPart={learn} onPool={(link) => setPool({ link, startNow: true })} />
    )
  return (
    <div className="pk-page cfc-page cfc-linkpage">
      <div className="cfc-grid">
        <YourPart part={part} nowMs={Date.now()} />
        {/* On a phone the person's part stays above the panel: the finish's own grid puts its panel first. */}
        <div className="cfc-right" style={{ order: 0 }}>
          <PageHead title="Your setup link" />
          {panel}
        </div>
      </div>
    </div>
  )
}

function OpenLink({ token, auto, onPart, onPool }: { token: string; auto: boolean; onPart: (p: Partial<LinkPart>) => void; onPool: (link: PoolLink) => void }) {
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<{ line: string; retry: boolean } | null>(null)
  const [needs, setNeeds] = useState<FormKind | null>(null)
  const [written, setWritten] = useState<Opened | null>(null)
  const [notReady, setNotReady] = useState<string | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const autoOpened = useRef(false)

  useEffect(
    () => () => {
      if (timer.current !== null) clearTimeout(timer.current)
    },
    [],
  )

  useEffect(() => {
    if (!auto || autoOpened.current) return
    autoOpened.current = true
    // Once: a reload of this tab is an ordinary visit again.
    if (globalThis.window !== undefined) window.name = ''
    void open()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [auto])

  async function open() {
    if (busy) return
    if (timer.current !== null) clearTimeout(timer.current)
    timer.current = null
    setBusy(true)
    setRefused(null)
    const r = await redeemSetupLink(token)
    setBusy(false)
    if (r.status === 'error') {
      if (r.error.httpStatus === 409) {
        // The workspace is not ready: swarm-api kept the link unused, so open it again shortly.
        setNotReady(r.error.message)
        onPart({ link: 'unused', kind: 'pool-account', expiresAt: expiresFrom(r.error) })
        timer.current = setTimeout(() => void open(), REDEEM_RETRY_MS)
        return
      }
      setNotReady(null)
      const kind = valueKind(r.error)
      if (kind !== null) {
        onPart({ link: 'unused', kind, expiresAt: expiresFrom(r.error) })
        setNeeds(kind)
      } else {
        const refusal = linkRefusal(r.error)
        if (!refusal.retry) onPart({ link: r.error.httpStatus === 403 ? 'not-yours' : 'invalid' })
        setRefused(refusal)
      }
      return
    }
    setNotReady(null)
    const opened = r.status === 'ok' || r.status === 'stale' ? openedFrom(r.data) : null
    if (opened === null) {
      setRefused({ line: 'The platform opened the link but did not say what it is for: ask the admin who sent it for a new link.', retry: false })
      return
    }
    if (opened.kind === 'pool-account') {
      // The link is spent from here: keep what it answered for a reload.
      writePending(opened.link)
      onPart({ link: 'spent', kind: 'pool-account' })
      onPool(opened.link)
      return
    }
    onPart({ link: 'spent', kind: opened.kind, done: true })
    setWritten(opened)
  }

  if (written !== null && written.kind !== 'pool-account') return <Written opened={written} />
  if (needs !== null) {
    return (
      <ValueForm token={token} kind={needs} onPart={onPart} onWritten={(o) => {
        onPart({ link: 'spent', kind: needs, done: true })
        setWritten(o)
      }} />
    )
  }
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
      {notReady !== null && (
        <Banner tone="info" title="Your workspace is still being set up." role="status">
          The link is unused, and this page opens it by itself as soon as the workspace is ready ({notReady.replace(/[.\s]+$/, '')}).
        </Banner>
      )}
      <div className="pk-form-row">
        <Button kind="primary" busy={busy ? 'Opening…' : notReady !== null ? 'Waiting for the workspace…' : false} onClick={() => void open()}>
          Open the link
        </Button>
      </div>
    </Card>
  )
}

const VALUE_WORDS: Record<FormKind, { title: string; field: string; lead: string; verb: string }> = {
  'team-git': {
    title: "Store the team's GitHub token",
    field: "The team's GitHub token",
    lead: 'The value goes straight from this page into its one secret in Secret Manager. It is never shown again, and the link is used only once it is stored.',
    verb: 'Store it',
  },
  'addon-secret': {
    title: "Store the add-on's secret",
    field: "The add-on's secret",
    lead: 'The value goes straight from this page into its one secret in Secret Manager. It is never shown again, and the link is used only once it is stored.',
    verb: 'Store it',
  },
  'voice-api-key': {
    title: "Add Voice it's OpenAI API key",
    field: 'OpenAI API key (it begins sk-)',
    lead: 'A developer API key from your OpenAI account, not a ChatGPT sign-in: only a key can open a Live session. It goes from this page to the account pool once, into a secret only the pool reads, and is never shown again. Sending it uses the link, so a key the pool refuses needs a new link from Admin › Accounts.',
    verb: 'Add the key',
  },
}

function ValueForm({ token, kind, onPart, onWritten }: {
  token: string
  kind: FormKind
  onPart: (p: Partial<LinkPart>) => void
  onWritten: (o: Opened) => void
}) {
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
      const refusal = linkRefusal(r.error)
      if (!refusal.retry) onPart({ link: r.error.httpStatus === 403 ? 'not-yours' : 'invalid' })
      setRefused(refusal.line)
      return
    }
    setValue('')
    const opened = r.status === 'ok' || r.status === 'stale' ? openedFrom(r.data) : null
    onWritten(opened ?? (kind === 'voice-api-key' ? { kind, label: null, account: null } : { kind, label: null, secret: null }))
  }

  return (
    <Card title={words.title}>
      <form className="pk-form" autoComplete="off" onSubmit={(e) => void send(e)}>
        <p className="pk-lead">{words.lead}</p>
        <label htmlFor={`${id}-v`}>{words.field}</label>
        <input id={`${id}-v`} className="c-inp" type="password" autoComplete="off" spellCheck={false} value={value}
          onChange={(e) => setValue(e.target.value)} />
        {refused !== null && <Banner tone="bad" title={refused} role="alert" />}
        <div className="pk-form-row">
          <Button kind="primary" type="submit" busy={busy ? 'Storing…' : false} disabled={value.trim() === ''}>
            {words.verb}
          </Button>
        </div>
      </form>
    </Card>
  )
}

function Written({ opened }: { opened: Opened }) {
  if (opened.kind === 'voice-api-key') {
    return (
      <Banner tone="info" title={`The key is in the Voice it pool${opened.account !== null ? ` as ${opened.account}` : ''}.`} role="status">
        This link is now used. Admin &rsaquo; Accounts shows the account in the Voice it pool; its usage windows read
        &ldquo;not measured&rdquo;, because OpenAI gives a key none. You can close this page.
      </Banner>
    )
  }
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
function PoolSignIn({ link, startNow, onSignedIn }: { link: PoolLink; startNow: boolean; onSignedIn: () => void }) {
  const id = useId()
  const [at, setAt] = useState<SignIn>({ kind: 'idle' })
  const started = useRef(false)
  const retries = useRef(0)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(
    () => () => {
      if (timer.current !== null) clearTimeout(timer.current)
    },
    [],
  )

  async function start(again: boolean = false) {
    if (timer.current !== null) clearTimeout(timer.current)
    timer.current = null
    if (!again) retries.current = 0
    setAt({ kind: 'starting' })
    // THE WORKSPACE IS THE LINK'S, sent as `X-Swarm-Tenant` by every request
    // this tab makes (fetch.ts `chooseTenant`): the sign-in files the account
    // into the workspace the header names.
    chooseTenant(link.tenant)
    const r = await beginAccountSignIn({ label: link.label, lend_to: [] })
    if (r.status === 'ok' || r.status === 'stale') setAt({ kind: 'open', auth: r.data, code: '', sending: false, failed: null })
    else if (r.status === 'error') {
      // A workspace just made ready: swarm-api may still hold "not a member" for a few seconds.
      if (r.error.httpStatus === 403 && retries.current < SIGN_IN_RETRIES) {
        retries.current += 1
        timer.current = setTimeout(() => void start(true), SIGN_IN_RETRY_MS)
        setAt({ kind: 'starting' })
        return
      }
      setAt({ kind: 'start_failed', error: r.error })
    }
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
      onSignedIn()
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
        {at.kind === 'starting' && retries.current > 0 && (
          <p className="pk-lead" role="status">
            Your workspace is finishing its setup; the sign-in starts again by itself in a few seconds.
          </p>
        )}
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
