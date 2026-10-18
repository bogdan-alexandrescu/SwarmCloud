import { useEffect, useRef, useState } from 'react'
import { Banner, Button, Card, EmptyState } from './components'
import { USE_FIXTURES } from './api'
import { read, route, write, type ApiError, type Result } from './fetch'
import { UrRegion, useUrRead } from './RepositoriesParts'
import { humaniseUntil, timeAgo } from './types'
import { AGE_TICK_MS, useNow } from './useNow'
import './styles/onboarding.css'

/**
 * NAMED BROWSER LOGINS (LB-7; docs/design/live-browser.md §5.5.1, lane L12c).
 *
 * A person who signed in through the live browser of a task IN THEIR OWN
 * WORKSPACE (`u-<user>`) may keep that session as a named login, and a later
 * task in the same workspace opts in with `input.browser_login: "<name>"`.
 * Two things here, and nothing else:
 *
 *   * `SaveAsLoginOffer`, which the Browser tab (`LiveBrowser.tsx`) draws once
 *     the person has pressed Return control: a name, an expiry, and the
 *     owner's account warning;
 *   * `BrowserLoginsPane`, Access › Browser logins: the person's own logins,
 *     name, site, expiry and last use, each with a typed Revoke.
 *
 * NEVER IN A GROUP TENANT. Every worker of a group tenant such as `eng` runs as
 * one identity, so no grant could let one member's agents use a session and
 * refuse another's (§5.5.1, "In a group tenant: nothing is kept"). The offer
 * is not drawn there, and the pane reads nothing there. The server is what
 * enforces it -- it refuses a save outside a personal tenant, and the vault's
 * IAM is per person (lane L8b) -- so this is the console not offering what
 * would only be refused.
 *
 * NEVER A VALUE. The jar moves gate to gate and into Secret Manager; the
 * console never holds it. `BrowserLogin` names the four fields the list draws,
 * and the list draws them from that type alone, so a field the server sent
 * beyond them -- a value, a cookie -- has no way onto the page.
 *
 * THE ROUTES are lane L9b's (`swarm_api/routes/browser_logins.py`), which the
 * design does not spell out; they are named once, below, so the rebase onto
 * L9b changes one block if L9b chose others.
 */

const LIST = '/v1/browser-logins'
const SAVE = '/v1/browser-logins'
const REVOKE = '/v1/browser-logins/{name}'

/** §5.5.1: "an expiry (default 14 days, at most 30)". */
export const DEFAULT_EXPIRY_DAYS = 14
export const MAX_EXPIRY_DAYS = 30

/**
 * A name the vault can hold. It becomes the tail of the secret id
 * `swarm-tenant-u-<user>-browser-login-<name>` and the `input.browser_login`
 * a task names, so it is lower-case letters, digits and inner hyphens, at most
 * 40: Secret Manager ids allow more, and a tenant slug already spends part of
 * the 255. The server checks it again; this is the console not sending a name
 * it knows will be refused.
 */
const LOGIN_NAME = /^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$/

/** What the list draws. No value field, on purpose: see NEVER A VALUE above. */
export interface BrowserLogin {
  name: string
  /** The origin the session was signed in on, as the gate reported it. */
  site: string
  saved_at: string
  expires_at: string
  /** Null until a task has opted in to it. */
  last_used_at: string | null
}

export interface BrowserLoginList {
  tenant_id: string
  logins: BrowserLogin[]
}

/** A personal workspace's tenant id is `u-<slug>` (`swarm_common/identity.py`); a group's never is. */
export function isPersonalTenant(tenantId: string | null | undefined): boolean {
  return typeof tenantId === 'string' && tenantId.startsWith('u-')
}

/** Whether the Browser tab offers Save as login: a personal workspace's task, after Return control. */
export function offersSaveAsLogin(tenantId: string | null | undefined, controlReturned: boolean): boolean {
  return controlReturned && isPersonalTenant(tenantId)
}

/** The four fields and nothing else, whatever the row carried. */
function asLogin(raw: unknown): BrowserLogin {
  const r = (raw ?? {}) as Record<string, unknown>
  const s = (v: unknown) => (typeof v === 'string' ? v : '')
  return {
    name: s(r.name),
    site: s(r.site),
    saved_at: s(r.saved_at),
    expires_at: s(r.expires_at),
    last_used_at: typeof r.last_used_at === 'string' ? r.last_used_at : null,
  }
}

export async function loadBrowserLogins(): Promise<Result<BrowserLoginList>> {
  // The development fixture has no live browser and so no login to list.
  if (USE_FIXTURES) return { status: 'empty', fetchedAt: Date.now() }
  const r = await read<BrowserLoginList>(route(LIST), (d) => !Array.isArray(d?.logins) || d.logins.length === 0)
  if (r.status === 'ok' || r.status === 'stale') {
    return { ...r, data: { tenant_id: String(r.data.tenant_id ?? ''), logins: r.data.logins.map(asLogin) } }
  }
  return r
}

export async function saveBrowserLogin(taskId: string, name: string, expiresInDays: number): Promise<Result<unknown>> {
  return write(route(SAVE), 'POST', { task_id: taskId, name, expires_in_days: expiresInDays })
}

export async function revokeBrowserLogin(name: string): Promise<Result<unknown>> {
  return write(route(REVOKE, { name }), 'DELETE')
}

/** "expires in 11d 4h", or "expired 1d ago": an instant ahead counts forward (Access.tsx `tokenExpiry`). */
function expiryWords(iso: string, now: number): string {
  const t = new Date(iso).getTime()
  if (!Number.isFinite(t)) return 'expiry unreadable'
  return t > now ? `expires in ${humaniseUntil(t - now)}` : `expired ${timeAgo(t, now)}`
}

/** Past its expiry: a task that names it now fails before its agent runs (visual QA V234). */
function hasExpired(iso: string, now: number): boolean {
  const t = new Date(iso).getTime()
  return Number.isFinite(t) && t <= now
}

function lastUseWords(iso: string | null, now: number): string {
  return iso === null ? 'never used' : `used ${timeAgo(iso, now)}`
}

function Refused({ error, title }: { error: ApiError; title: string }) {
  return (
    <Banner tone="bad" title={title} role="alert">
      {error.message}
    </Banner>
  )
}

// ---------------------------------------------------------------------------
// Save as login, on the Browser tab
// ---------------------------------------------------------------------------

/**
 * Drawn by the Browser tab after Return control. Renders nothing unless the
 * task is in a personal workspace and the person has returned control, so the
 * tab mounts it unconditionally and passes what it knows.
 *
 * `site` is the top frame's origin from the console's origin strip (§3.4,
 * §5.7) -- the gate's report, not anything the page drew -- and it is shown
 * only in the warning. The server records the site from the gate's export, not
 * from this request, so the body carries the task, the name and the expiry.
 */
export function SaveAsLoginOffer({
  taskId,
  tenantId,
  site,
  controlReturned,
}: {
  taskId: string
  tenantId: string | null
  site: string | null
  controlReturned: boolean
}) {
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [days, setDays] = useState(String(DEFAULT_EXPIRY_DAYS))
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const [saved, setSaved] = useState<string | null>(null)
  if (!offersSaveAsLogin(tenantId, controlReturned)) return null

  const n = Number(days)
  const daysOk = /^\d+$/.test(days) && n >= 1 && n <= MAX_EXPIRY_DAYS
  const nameOk = LOGIN_NAME.test(name)
  const where = site ?? 'this site'

  async function save() {
    if (!nameOk || !daysOk || busy) return
    setBusy(true)
    setRefused(null)
    const res = await saveBrowserLogin(taskId, name, n)
    setBusy(false)
    if (res.status === 'error') {
      setRefused(res.error)
      return
    }
    setSaved(name)
    setOpen(false)
  }

  if (saved !== null) {
    return (
      <Banner tone="info" title={`Saved as ${saved}`} role="status">
        A task in your workspace uses it with <code>input.browser_login: "{saved}"</code>. Revoke it under Access › Browser
        logins.
      </Banner>
    )
  }

  if (!open) {
    return (
      <div className="lb-save-offer">
        <Button size="sm" onClick={() => setOpen(true)}>
          Save as login
        </Button>
        <span className="ur-hint">Keep this sign-in for later tasks in your workspace.</span>
      </div>
    )
  }

  return (
    <form
      className="lb-save-form"
      aria-label="Save as login"
      onSubmit={(e) => {
        e.preventDefault()
        void save()
      }}
      onKeyDown={(e) => {
        if (e.key === 'Escape' && !busy) {
          e.stopPropagation()
          setOpen(false)
        }
      }}
    >
      {/* The owner's answer (§5.5.1, "Whose account"): any account may be
          saved, at the person's judgement, and the dialog says what that
          lets their agents do. */}
      <Banner tone="warn" title="Every agent you run in your workspace can act as this account">
        Saving keeps the session signed in on {where}. Until the login expires or is revoked, any task in your workspace that
        names it starts signed in as this account, and can do there whatever the account can.
      </Banner>
      <label className="ur-subh" htmlFor="lb-save-name">
        Name
      </label>
      <input
        id="lb-save-name"
        className="ur-search"
        placeholder="github-qa"
        value={name}
        maxLength={40}
        autoComplete="off"
        onChange={(e) => setName(e.target.value)}
        aria-invalid={name !== '' && !nameOk ? true : undefined}
      />
      {name !== '' && !nameOk && <p className="ur-hint is-warn">Lower-case letters, digits and hyphens, at most 40.</p>}
      <label className="ur-subh" htmlFor="lb-save-days">
        Expires after (days, at most {MAX_EXPIRY_DAYS})
      </label>
      <input
        id="lb-save-days"
        className="ur-search"
        type="number"
        min={1}
        max={MAX_EXPIRY_DAYS}
        value={days}
        onChange={(e) => setDays(e.target.value)}
        aria-invalid={!daysOk ? true : undefined}
      />
      {!daysOk && <p className="ur-hint is-warn">Between 1 and {MAX_EXPIRY_DAYS} days.</p>}
      <span className="ur-acts">
        <Button size="sm" kind="primary" type="submit" busy={busy ? 'Saving…' : false} disabled={!nameOk || !daysOk}>
          Save login
        </Button>
        <Button size="sm" onClick={() => setOpen(false)} disabled={busy}>
          Not now
        </Button>
      </span>
      {refused !== null && <Refused error={refused} title={`${name} was not saved`} />}
    </form>
  )
}

// ---------------------------------------------------------------------------
// Access › Browser logins
// ---------------------------------------------------------------------------

/**
 * REVOKE IS TYPED. Revoke destroys every secret version (§5.5.1), so a task
 * that names the login fails before its agent runs from then on, and the only
 * way back is another sign-in. The person types the login's name; the button
 * stays off until it matches exactly. Esc is Keep, never the action, as on
 * the rest of Access (Access.tsx, "ASK FIRST, AND MEAN IT").
 */
function TypedRevoke({ login, onDone, onKeep }: { login: BrowserLogin; onDone: () => void; onKeep: () => void }) {
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const input = useRef<HTMLInputElement>(null)
  useEffect(() => {
    input.current?.focus()
  }, [])
  const matches = typed === login.name
  const id = `lb-revoke-${login.name}`

  async function revoke() {
    if (!matches || busy) return
    setBusy(true)
    setRefused(null)
    const res = await revokeBrowserLogin(login.name)
    setBusy(false)
    if (res.status === 'error') {
      setRefused(res.error)
      return
    }
    onDone()
  }

  return (
    <div
      className="ac-ask"
      role="group"
      aria-label={`Revoke ${login.name}`}
      onKeyDown={(e) => {
        if (e.key === 'Escape' && !busy) {
          e.stopPropagation()
          onKeep()
        }
      }}
    >
      <Banner
        tone="warn"
        title={`Revoke ${login.name}?`}
        actions={
          <>
            <Button kind="danger-filled" size="sm" busy={busy ? 'Revoking…' : false} disabled={!matches} onClick={() => void revoke()}>
              Revoke
            </Button>
            <Button size="sm" onClick={onKeep} disabled={busy}>
              Keep
            </Button>
          </>
        }
      >
        Every saved version is destroyed. A task that names {login.name} fails before its agent runs until you save a new
        one.
        {/* The typed name is inside the warning it confirms (visual QA V233): below it, it read as a separate form. */}
        <span className="lb-revoke-type">
          <label className="ur-subh" htmlFor={id}>
            Type {login.name} to revoke
          </label>
          <input
            id={id}
            ref={input}
            className="ur-search"
            value={typed}
            autoComplete="off"
            onChange={(e) => setTyped(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault()
                void revoke()
              }
            }}
          />
        </span>
      </Banner>
      {refused !== null && <Refused error={refused} title={`${login.name} was not revoked`} />}
    </div>
  )
}

function LoginRow({ login, now, onChange }: { login: BrowserLogin; now: number; onChange: () => void }) {
  const [asking, setAsking] = useState(false)
  // KEEP GIVES FOCUS BACK TO THE ROW'S REVOKE (visual QA V233). The button is
  // off while the question is open, and the question took focus with it, so
  // closing it with Keep or Esc dropped focus to <body>: a keyboard user was
  // thrown back to the top of the page.
  const rowRef = useRef<HTMLLIElement>(null)
  const [refocus, setRefocus] = useState(false)
  useEffect(() => {
    if (!refocus || asking) return
    rowRef.current?.querySelector<HTMLButtonElement>('button[data-revoke]')?.focus()
    setRefocus(false)
  }, [refocus, asking])
  const expired = hasExpired(login.expires_at, now)
  return (
    <li ref={rowRef} className="ac-row" data-login={login.name}>
      <span className="ac-row-name">
        <b>{login.name}</b>
        <span className="ur-small">{login.site}</span>
      </span>
      <span className="ur-small">
        <span className={`lb-expiry${expired ? ' is-expired' : ''}`}>{expiryWords(login.expires_at, now)}</span> ·{' '}
        {lastUseWords(login.last_used_at, now)}
      </span>
      <Button size="sm" kind="danger" data-revoke="" aria-label={`Revoke ${login.name}`} disabled={asking} onClick={() => setAsking(true)}>
        Revoke
      </Button>
      {asking && (
        <span className="ac-row-err">
          <TypedRevoke
            login={login}
            onKeep={() => {
              setAsking(false)
              setRefocus(true)
            }}
            onDone={onChange}
          />
        </span>
      )}
    </li>
  )
}

/**
 * Access › Browser logins: the signed-in person's own logins in their
 * workspace. `tenantId` is the tenant the page is in; in a group tenant there
 * are none to read, and the pane says where they live instead.
 */
export function BrowserLoginsPane({ tenantId }: { tenantId: string | null }) {
  const group = tenantId !== null && !isPersonalTenant(tenantId)
  if (group) {
    return (
      <Card className="lb-logins" title="Browser logins">
        <EmptyState kind="empty" heading="Not kept in a group tenant">
          A browser login is kept only in your personal workspace. Tenant {tenantId} is a group: every member's agents share
          one identity there, so each browser step signs in fresh and nothing is saved.
        </EmptyState>
      </Card>
    )
  }
  return <PersonalLogins />
}

function PersonalLogins() {
  const logins = useUrRead(loadBrowserLogins, 'browser-logins')
  const now = useNow(AGE_TICK_MS)
  return (
    <Card className="lb-logins" title="Browser logins">
      <UrRegion
        state={logins.state}
        route={`GET ${LIST}`}
        what="Your browser logins"
        onRetry={logins.reload}
        plural
        empty={
          <EmptyState kind="empty" heading="No browser login saved">
            After you return control of a task's browser in your workspace, its Browser tab offers Save as login.
          </EmptyState>
        }
      >
        {(d) => (
          <ul className="ac-rows" aria-label="Browser logins">
            {d.logins.map((l) => (
              <LoginRow key={l.name} login={l} now={now} onChange={logins.reload} />
            ))}
          </ul>
        )}
      </UrRegion>
      <p className="ur-hint">
        Names and sites only: the session itself stays in Secret Manager, readable by your workspace's agents alone, and is
        never shown here.
      </p>
    </Card>
  )
}
