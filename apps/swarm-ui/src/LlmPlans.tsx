/**
 * CAPACITY › LLM PROVIDERS › PLANS (docs/design/llm-providers.md §6; the
 * owner's ask of 11 November 2026, the cards direction in place of the table).
 *
 * CARDS, GROUPED BY VENDOR. A card is a plan and leads with what the owner
 * asked to see at a glance: the quota LEFT in the 5-hour and 7-day windows
 * (100 minus the measured share used) as its largest figures, each with a
 * bar and its next reset (local time, the day when not today, and how long
 * until it). Then its state, live holders over max agents, the Voice it mark
 * and its lends as one line. ONE Manage control per card (the whole card
 * opens it too) and Resume only while paused; lends are edited in Manage.
 * Every change happens in a side sheet on the right: Add plan, Manage
 * (every plan control) and Lend, with Remove behind Manage.
 *
 * READABLE TYPE: nothing on a card is drawn below the body step; the name is
 * at the lead step and the quota figures at the figure step
 * (styles/llm-providers.css, the `.lpc-` rules).
 *
 * READS AND WRITES ARE api.ts's PLAN CALLS, nothing else: the account list
 * (`loadAccountsBoard`), `setAccountState`, `setAccountMaxAgents`,
 * `setAccountName`, `setAccountLending` (each lend with its agents cap),
 * `setAccountPerWorkspaceMax`, `setAccountModelClasses`,
 * `refreshAccount`, `removeAccount`, the sign-in pair and `mintSetupLink`,
 * and the Voice it pool route (`setAccountPools`, which still lives beside
 * the old board's Pools control in Accounts.tsx). A write that succeeds
 * re-reads the list rather than patching a copy; a write that fails says why
 * in its sheet and leaves the row as the platform last said it was.
 *
 * A LEND LINE is the borrower's live holders over its lend's agents cap
 * (§4.6), the holders counted by swarm-api on the owner's rows only; a lend
 * carried over from before lend caps reads `cap not set`, and a row whose
 * broker serves no `lends` draws no figure. A spend cap is a key's, not a
 * plan's (a plan's price is flat).
 *
 * WHAT IS NOT DRAWN, because no route serves it yet: adding a ChatGPT plan
 * from the browser (its sign-in is the plugin's `sc account add --provider
 * openai`). It is a question in the lane's PR, not a dead control.
 *
 * No explanatory prose on the surface: every definition sits behind an (i).
 */
import { useEffect, useRef, useState, type ReactNode } from 'react'
import {
  beginAccountSignIn,
  finishAccountSignIn,
  loadAccountsBoard,
  mintSetupLink,
  refreshAccount,
  removeAccount,
  setAccountLending,
  setAccountMaxAgents,
  setAccountModelClasses,
  setAccountName,
  setAccountPerWorkspaceMax,
  setAccountState,
  type AccountsBoard,
  type ServedAccount,
} from './api'
import { accountName } from './accountName'
import { MODEL_CLASSES, classesToSave, servedClasses, setAccountPools } from './Accounts'
import { Info } from './AccountsVerdict'
import { inVoiceItPool, poolsOf } from './AccountsTable'
import { DEFAULT_MAX_AGENTS, blocksOf, inTime, isWindowBlock, type PoolAccount } from './accountsPool'
import { MAX_AGENTS_LIMIT } from './AccountSheet'
import { ACCOUNTS_POLL_MS, capacityPoll, paneHref, useLinkedParam } from './capacityPoll'
import { Button } from './components'
import type { ApiError, Result } from './fetch'
import type { TopicId } from './help'
import { Screen } from './Shell'
import { FIVE_HOUR, SEVEN_DAY, humaniseUntil, isProjected, readingOf } from './types'
import { AGE_TICK_MS, useNow } from './useNow'
import './styles/llm-providers.css'

/** Plans' own address; `?account=<id>` on it opens that plan's Manage sheet. */
export const PLANS_ADDRESS = 'capacity/llm-providers/plans'

type Plan = ServedAccount & PoolAccount

/** The vendors in the table's order; a provider outside them groups under its own id, after them. */
const VENDORS: readonly { provider: string; label: string; plan: string }[] = [
  { provider: 'anthropic', label: 'Anthropic', plan: 'Claude' },
  { provider: 'openai', label: 'OpenAI', plan: 'ChatGPT' },
]

/** A plan is a subscription sign-in; an `-api` provider is a developer key, API Keys' page. */
export function isPlan(a: { provider: string }): boolean {
  return !a.provider.endsWith('-api')
}

function vendorOf(provider: string) {
  return VENDORS.find((v) => v.provider === provider) ?? null
}

/** The plans grouped by vendor: the known vendors first, in order, then any other provider by name. */
export function byVendor(plans: readonly Plan[]): { provider: string; rows: Plan[] }[] {
  const known = VENDORS.map((v) => v.provider)
  const others = [...new Set(plans.map((p) => p.provider).filter((p) => !known.includes(p)))].sort()
  return [...known, ...others]
    .map((provider) => ({ provider, rows: plans.filter((p) => p.provider === provider) }))
    .filter((g) => g.rows.length > 0)
}

function maxOf(a: Plan): number {
  return typeof a.max_agents === 'number' ? a.max_agents : DEFAULT_MAX_AGENTS
}

/** The address with `?account=` set or cleared, followed the way the router follows a pane link. */
function goAccount(id: string | null): void {
  const hash = paneHref(PLANS_ADDRESS, { account: id })
  const oldURL = window.location.href
  window.history.pushState(window.history.state, '', hash)
  window.dispatchEvent(new HashChangeEvent('hashchange', { oldURL, newURL: window.location.href }))
}

/* ---------------- the cells ---------------- */

const STATE_WORD: Readonly<Record<string, string>> = {
  AVAILABLE: 'Available',
  PAUSED: 'Paused',
  DRAINING: 'Draining',
  REAUTH_REQUIRED: 'Sign-in needed',
}

const STATE_TIP: Readonly<Record<string, string>> = {
  AVAILABLE: 'Takes new agents',
  PAUSED: 'Holders keep it; nothing new is assigned',
  DRAINING: 'Nothing new; holders move off at their next turn',
  REAUTH_REQUIRED: 'Its sign-in no longer refreshes; sign it in again under the same label',
}

/** The state: a dot alone when it takes agents, the word where it asks for something. */
function StateMark({ a }: { a: Plan }) {
  const word = STATE_WORD[a.state]
  const tone = a.state === 'AVAILABLE' ? 'is-ok' : a.state === 'REAUTH_REQUIRED' ? 'is-bad' : `is-${String(a.state).toLowerCase()}`
  const tip = [STATE_TIP[a.state], a.reason].filter(Boolean).join(' · ')
  return (
    <span className={`llp-st ${tone}`} title={tip || undefined}>
      <i aria-hidden="true" />
      {word === undefined ? <span className="mono">{a.state}</span> : a.state === 'AVAILABLE' ? <span className="llp-vh">{word}</span> : word}
    </span>
  )
}

/** Used over cap as a figure and a hairline bar; at the cap the bar turns red. */
function Over({ used, cap, title }: { used: number; cap: number; title?: string }) {
  const pct = cap > 0 ? Math.min(100, (used / cap) * 100) : 0
  const tone = used >= cap ? 'is-full' : pct >= 80 ? 'is-near' : ''
  return (
    <span className={`llp-over ${tone}`} title={title}>
      <span className="llp-over-n">
        {used}
        <span className="llp-dim"> / {cap}</span>
      </span>
      <span className="llp-bar" aria-hidden="true">
        <b style={{ width: `${pct}%` }} />
      </span>
    </span>
  )
}

/** A lend's agents cap, or null for one carried over that no lender has saved. */
function lendCap(a: Plan, t: string): number | null {
  const c = a.lends?.[t]
  return c !== undefined && c.cap_set !== false && Number.isFinite(c.max_agents) ? c.max_agents : null
}

/** A borrower's live holders over its lend's cap; nothing when the broker serves no `lends`. */
function LendUse({ a, t }: { a: Plan; t: string }) {
  if (a.lends === undefined) return null
  const held = a.lend_holders?.[t]
  const n = typeof held === 'number' ? held : null
  const cap = lendCap(a, t)
  if (cap === null)
    return (
      <span className="llp-over-n">
        {n ?? '—'}{' '}
        <span className="ctl-mark is-partial" title="Carried over from before lend caps; bounded by the plan's own max agents until a lender saves one">
          cap not set
        </span>
      </span>
    )
  if (n === null)
    return (
      <span className="llp-over-n llp-dim" title={`${t}: live holders not read`}>
        — / {cap}
      </span>
    )
  return <Over used={n} cap={cap} title={`${t}: agents now over the lend's cap`} />
}

/* ---------------- the card's quota ---------------- */

const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

/** `14:20` in the reader's own zone, with the day (`Thu 09:00`) when it is not today. */
export function localAt(at: number, now: number): string {
  const d = new Date(at)
  const n = new Date(now)
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  const today = d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate()
  return today ? hm : `${DAYS[d.getDay()]} ${hm}`
}

/**
 * One window's quota LEFT (100 minus the measured share used), the card's
 * largest figure, and its next reset. The marks keep their meaning: a
 * measured figure plainly; a stale one, or one past its reset, as projected
 * (`~`), never as current; no reading as "unknown", never 0% or 100%.
 */
function Quota({ a, k, name, now }: { a: Plan; k: string; name: string; now: number }) {
  const r = readingOf(a, k)
  const measured = r.kind === 'live' || r.kind === 'stale'
  // A window past its reset has refilled: projected full until the next reading.
  const left = measured ? Math.round(100 - r.pct) : r.kind === 'reset' ? 100 : null
  const projected = isProjected(r)
  const resets = measured ? Date.parse(r.resetsAt) : NaN
  const upcoming = Number.isFinite(resets) && resets > now
  const blocks = blocksOf(a).some((b) => isWindowBlock(b) && b.window === k)
  const tone = left === null ? ' is-off' : blocks ? ' is-bad' : projected ? ' is-projected' : ''
  const tip =
    left === null
      ? `${name}: no reading`
      : `${name}: ${projected ? 'projected ' : ''}${left}% left` +
        (r.kind === 'reset' ? `, reset ${localAt(Date.parse(r.resetsAt), now)}, nothing read since` : '') +
        (r.kind === 'stale' ? ', from an old reading' : '')
  return (
    <div className={`lpc-q${tone}`} data-window={k} title={tip}>
      <span className="lpc-q-l">{name}</span>
      <span className="lpc-q-v">
        {left === null ? (
          <span className="lpc-q-unk">unknown</span>
        ) : (
          <>
            <span className="lpc-q-n">
              {projected ? '~' : ''}
              {left}%
            </span>{' '}
            left
          </>
        )}
      </span>
      <span className="lpc-qbar" aria-hidden="true">
        {left !== null && <b style={{ width: `${Math.max(0, Math.min(100, left))}%` }} />}
      </span>
      <span className="lpc-q-at" data-reset="">
        <span className="lpc-q-ic" aria-hidden="true">
          ↻
        </span>
        {upcoming ? (
          <span title={`${name}: resets ${localAt(resets, now)}, ${inTime(resets, now)}`}>
            <span className="lpc-q-time">{localAt(resets, now)}</span> <span className="lpc-q-in">in {humaniseUntil(resets - now).replace(' ', '')}</span>
          </span>
        ) : (
          <span className="lpc-q-time" title={`${name}: next reset not served`}>
            unknown
          </span>
        )}
      </span>
    </div>
  )
}

/** The lends as one line: each borrower with its live holders over its lend's cap. */
function LendsLine({ a }: { a: Plan }) {
  if (a.lend_to.length === 0) return null
  return (
    <p className="lpc-lends" title="Lent to: live holders / the lend's agents cap">
      <span className="lpc-lends-l">Lent</span>{' '}
      {a.lend_to.map((t, i) => {
        const held = a.lends === undefined ? undefined : a.lend_holders?.[t]
        const n = typeof held === 'number' ? held : null
        const cap = lendCap(a, t)
        const over = n !== null && cap !== null && n >= cap
        return (
          <span key={t} className="lpc-lend" data-lend={t}>
            {i > 0 && ' '}
            <span className="lpc-mono">{t}</span>
            {a.lends !== undefined && (
              <span className={`lpc-num${over ? ' is-bad' : ''}`}>
                {' '}
                {n ?? '—'}
                {cap === null ? (
                  <span className="lpc-dim" title="Carried over from before lend caps; bounded by the plan's own max agents until a lender saves one">
                    {' '}
                    cap not set
                  </span>
                ) : (
                  `/${cap}`
                )}
              </span>
            )}
          </span>
        )
      })}
    </p>
  )
}

function Failure({ error }: { error: ApiError | null }) {
  if (error === null) return null
  return (
    <p className="llp-err" role="alert">
      {error.message}
    </p>
  )
}

/* ---------------- the cards ---------------- */

/**
 * One plan as a card: its name, its quota left in the 5-hour and 7-day
 * windows with each next reset, then its state, live holders over max
 * agents, Voice it and its lends. ONE control opens Manage (the whole card
 * does too); Resume only while paused. Lends are edited in Manage.
 */
function Card({
  a,
  tenant,
  sel,
  now,
  open,
  onResume,
  error,
}: {
  a: Plan
  tenant: string | null
  sel: boolean
  now: number
  open: (s: Sheet) => void
  onResume: (a: Plan) => void
  error: ApiError | null
}) {
  const owned = tenant === null || a.owner_tenant === tenant
  const v = vendorOf(a.provider)
  const tier = a.organisation?.plan ? `${v?.plan ?? a.provider} ${a.organisation.plan}` : null
  const classes = servedClasses(a)
  const max = maxOf(a)
  const full = a.assigned >= max
  const manage = () => open({ kind: 'manage', id: a.account_id })
  return (
    <article
      className={`lpc-card is-${String(a.state).toLowerCase()}${sel ? ' is-sel' : ''}${owned ? ' is-owned' : ''}`}
      aria-label={a.label}
      data-plan={a.account_id}
      // The whole card opens Manage; its one Manage button is the keyboard's way in.
      onClick={owned ? (e) => !(e.target as HTMLElement).closest('button, a, input, select') && manage() : undefined}
    >
      <div className="lpc-head">
        <span className="lpc-name">
          <span className="lpc-title" title={accountName(a)}>
            {accountName(a)}
          </span>
          <span className="lpc-sub">
            {accountName(a) !== a.label && (
              <span className="lpc-mono llp-label" title={a.label}>
                {a.label}
              </span>
            )}
            {tier !== null && <span title={classes.length > 0 ? `Serves ${classes.join(' · ')}` : undefined}>{tier}</span>}
            {!owned && (
              <span className="lpc-mono" title="Lent to this workspace by">
                {a.owner_tenant}
              </span>
            )}
          </span>
        </span>
        <StateMark a={a} />
      </div>
      <div className="lpc-quota">
        <Quota a={a} k={FIVE_HOUR} name="5-hour" now={now} />
        <Quota a={a} k={SEVEN_DAY} name="7-day" now={now} />
      </div>
      <div className="lpc-facts">
        <span className={`lpc-num lpc-agents${full ? ' is-bad' : ''}`} title="Live holders over max agents, counted from the grant (LEASED), not from RUNNING">
          {a.assigned}/{max} agents
        </span>
        {inVoiceItPool(a) && (
          <span className="lpc-voice" title="Shared with Voice it">
            Voice it
          </span>
        )}
      </div>
      <LendsLine a={a} />
      {owned && (
        <div className="lpc-foot">
          {a.state === 'PAUSED' && (
            <Button onClick={() => onResume(a)}>
              Resume
            </Button>
          )}
          <Button aria-label={`Manage ${a.label}`} onClick={manage}>
            Manage
          </Button>
        </div>
      )}
      <Failure error={error} />
    </article>
  )
}

function Cards({
  board,
  sel,
  open,
  onResume,
  rowError,
}: {
  board: AccountsBoard
  sel: string | null
  open: (s: Sheet) => void
  onResume: (a: Plan) => void
  rowError: { id: string; error: ApiError } | null
}) {
  const now = useNow(AGE_TICK_MS)
  const tenant = board.page.tenant_id
  const plans = (board.page.accounts as Plan[]).filter(isPlan)
  return (
    <div className="lpc-groups" aria-label="Plans" role="region">
      {byVendor(plans).map((g) => (
        <section key={g.provider} className="lpc-group" aria-label={vendorOf(g.provider)?.label ?? g.provider}>
          <h2 className="lpc-group-h">
            {vendorOf(g.provider)?.label ?? <span className="lpc-mono">{g.provider}</span>} <span className="lpc-dim">{g.rows.length}</span>
          </h2>
          <div className="lpc-grid">
            {g.rows.map((a) => (
              <Card
                key={a.account_id}
                a={a}
                tenant={tenant}
                sel={sel === a.account_id}
                now={now}
                open={open}
                onResume={onResume}
                error={rowError?.id === a.account_id ? rowError.error : null}
              />
            ))}
          </div>
        </section>
      ))}
    </div>
  )
}

/* ---------------- the side sheet ---------------- */

type Sheet = { kind: 'add' } | { kind: 'manage'; id: string } | { kind: 'lend'; id: string; to?: string } | { kind: 'remove'; id: string }

function SheetFrame({
  title,
  sub,
  onClose,
  onBack,
  children,
  foot,
}: {
  title: string
  sub?: ReactNode
  onClose: () => void
  onBack?: () => void
  children: ReactNode
  foot?: ReactNode
}) {
  const head = useRef<HTMLHeadingElement>(null)
  useEffect(() => {
    head.current?.focus()
  }, [title])
  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', k)
    return () => window.removeEventListener('keydown', k)
  }, [onClose])
  return (
    <aside className="llp-sheet" role="dialog" aria-modal="false" aria-label={title}>
      <header className="llp-sheet-head">
        {onBack && (
          <Button size="sm" kind="ghost" onClick={onBack}>
            Back
          </Button>
        )}
        <div className="llp-sheet-t">
          <h2 ref={head} tabIndex={-1}>
            {title}
          </h2>
          {sub && <div className="llp-sheet-sub">{sub}</div>}
        </div>
        <Button size="sm" kind="ghost" className="llp-x" aria-label="Close" onClick={onClose}>
          ✕
        </Button>
      </header>
      <div className="llp-sheet-body">{children}</div>
      {foot && <footer className="llp-sheet-foot">{foot}</footer>}
    </aside>
  )
}

function Section({ title, info, aside, children }: { title: string; info?: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="llp-ss" aria-label={title}>
      <h3>
        {title}
        {info && <Info about={title} tip={info} />}
        {aside && <span className="llp-ss-aside">{aside}</span>}
      </h3>
      {children}
    </section>
  )
}

function Field({ label, info, children }: { label: string; info?: string; children: ReactNode }) {
  return (
    <div className="llp-f">
      <span className="llp-f-l">
        {label}
        {info && <Info about={label} tip={info} />}
      </span>
      {children}
    </div>
  )
}

function Radios<T extends string>({ label, value, options, onPick }: { label: string; value: T; options: { key: T; label: string }[]; onPick: (v: T) => void }) {
  return (
    <div className="c-seg llp-seg" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button key={o.key} type="button" role="radio" aria-checked={value === o.key} onClick={() => onPick(o.key)}>
          {o.label}
        </button>
      ))}
    </div>
  )
}

function Switch({ on, onFlip, label }: { on: boolean; onFlip: () => void; label: string }) {
  return (
    <button type="button" role="switch" aria-checked={on} className={`llp-sw${on ? ' is-on' : ''}`} onClick={onFlip}>
      <i aria-hidden="true" />
      <span>{label}</span>
    </button>
  )
}

function failed(r: Result<unknown>): ApiError | null {
  return r.status === 'error' ? r.error : null
}

function sameList(a: readonly string[], b: readonly string[]): boolean {
  return a.join(' ') === b.join(' ')
}

function Manage({
  a,
  owned,
  close,
  go,
  reread,
}: {
  a: Plan
  owned: boolean
  close: () => void
  go: (s: Sheet) => void
  reread: () => Promise<void>
}) {
  const v = vendorOf(a.provider)
  const classesServed = servedClasses(a)
  const voiceServed = inVoiceItPool(a)
  const [max, setMax] = useState(String(maxOf(a)))
  const [classes, setClasses] = useState<string[]>(classesServed.length === 0 ? [...MODEL_CLASSES] : classesServed)
  const [name, setName] = useState(a.display_name ?? '')
  const [voice, setVoice] = useState(voiceServed)
  const [typed, setTyped] = useState('')
  const [asking, setAsking] = useState<'PAUSED' | 'DRAINING' | null>(null)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const pwServed = Object.entries(a.per_workspace_max ?? {}).map(([tenant, n]) => ({ tenant, max: String(n) }))
  const [pw, setPw] = useState(pwServed)
  // Only the owner and its borrowers may carry a cap: the broker refuses any other workspace.
  const pwFree = [a.owner_tenant, ...a.lend_to].filter((t) => !pw.some((p) => p.tenant === t))

  const hasClasses = a.provider === 'anthropic'
  const maxN = Number(max)
  const maxDirty = max !== String(maxOf(a))
  const maxOk = Number.isInteger(maxN) && maxN >= 1 && maxN <= MAX_AGENTS_LIMIT
  const classesDirty = hasClasses && !sameList(classesToSave(classes), classesToSave(classesServed))
  const nameDirty = name.trim() !== (a.display_name ?? '')
  const voiceDirty = voice !== voiceServed
  const pwKey = (rows: { tenant: string; max: string }[]) => JSON.stringify([...rows].sort((x, y) => x.tenant.localeCompare(y.tenant)))
  const pwDirty = pwKey(pw) !== pwKey(pwServed)
  const pwOk = pw.every((p) => {
    const n = Number(p.max)
    return Number.isInteger(n) && n >= 1 && n <= MAX_AGENTS_LIMIT
  })
  const dirty = maxDirty || classesDirty || nameDirty || voiceDirty || pwDirty
  const ready =
    dirty && (!maxDirty || maxOk) && (!pwDirty || pwOk) && (!voiceDirty || typed === a.label) && (!hasClasses || classes.length > 0)

  const run = async (calls: (() => Promise<Result<unknown>>)[], after?: (results: Result<unknown>[]) => void) => {
    setBusy(true)
    setError(null)
    setNote(null)
    const results: Result<unknown>[] = []
    let refused: ApiError | null = null
    for (const call of calls) {
      const r = await call()
      results.push(r)
      refused = failed(r)
      if (refused !== null) break
    }
    if (results.some((r) => failed(r) === null)) await reread()
    setBusy(false)
    setError(refused)
    if (refused === null) after?.(results)
  }

  /** Each changed field to its own route, in this order, stopping at the first refusal. */
  const save = () => {
    const calls: (() => Promise<Result<unknown>>)[] = []
    if (maxDirty) calls.push(() => setAccountMaxAgents(a.account_id, maxN))
    if (pwDirty) calls.push(() => setAccountPerWorkspaceMax(a.account_id, Object.fromEntries(pw.map((p) => [p.tenant, Number(p.max)]))))
    if (classesDirty) calls.push(() => setAccountModelClasses(a.account_id, classesToSave(classes)))
    if (voiceDirty) {
      const pools = poolsOf(a) ?? ['workspace']
      const next = voice ? [...pools, 'voice-it'] : pools.filter((p) => p !== 'voice-it')
      calls.push(() => setAccountPools(a.account_id, next, typed))
    }
    if (nameDirty) calls.push(() => setAccountName(a.account_id, name.trim() === '' ? null : name.trim()))
    void run(calls, () => setTyped(''))
  }

  const move = (to: 'AVAILABLE' | 'PAUSED' | 'DRAINING') =>
    void run([() => setAccountState(a.account_id, to, to === 'AVAILABLE' ? '' : reason.trim())], () => {
      setAsking(null)
      setReason('')
    })

  const refresh = () =>
    void run([() => refreshAccount(a.account_id)], (rs) => {
      const r = rs[0]
      if (r?.status === 'ok') {
        const out = (r.data as { refresh: { refreshed: boolean; reason: string } }).refresh
        setNote(out.refreshed ? 'Refreshed' : `Not refreshed · ${out.reason}`)
      }
    })

  return (
    <SheetFrame
      title={accountName(a)}
      sub={
        <>
          <span className="mono">{a.label}</span>
          {a.organisation?.plan && <span>{`${v?.plan ?? a.provider} ${a.organisation.plan}`}</span>}
          <StateMark a={a} />
        </>
      }
      onClose={close}
      foot={
        owned && (
          <>
            <Button kind="primary" disabled={!ready} busy={busy && dirty ? 'Saving' : false} onClick={save}>
              Save
            </Button>
            <Button kind="ghost" onClick={close}>
              Cancel
            </Button>
            <span className="llp-grow" />
            <Button kind="danger" onClick={() => go({ kind: 'remove', id: a.account_id })}>
              Remove plan
            </Button>
          </>
        )
      }
    >
      <Failure error={error} />
      {note !== null && (
        <p className="llp-note" role="status">
          {note}
        </p>
      )}
      {owned && (
        <>
          <Section title="Leasing">
            <div className="llp-btnrow">
              {a.state === 'AVAILABLE' && asking === null && (
                <>
                  <Button title={STATE_TIP.PAUSED} onClick={() => setAsking('PAUSED')}>
                    Pause
                  </Button>
                  <Button title={STATE_TIP.DRAINING} onClick={() => setAsking('DRAINING')}>
                    Drain
                  </Button>
                </>
              )}
              {(a.state === 'PAUSED' || a.state === 'DRAINING') && (
                <Button disabled={busy} onClick={() => move('AVAILABLE')}>
                  Resume
                </Button>
              )}
              {asking === null && (
                <Button disabled={busy} title="Read the windows now" onClick={refresh}>
                  Refresh
                </Button>
              )}
            </div>
            {asking !== null && (
              <div className="llp-ask">
                <Field label="Reason" info="Shown on the plan, for whoever puts it back.">
                  <input className="llp-in" aria-label="Reason" value={reason} onChange={(e) => setReason(e.target.value)} />
                </Field>
                <div className="llp-btnrow">
                  <Button kind="primary" disabled={reason.trim() === '' || busy} onClick={() => move(asking)}>
                    {asking === 'PAUSED' ? 'Pause plan' : 'Drain plan'}
                  </Button>
                  <Button kind="ghost" onClick={() => setAsking(null)}>
                    Cancel
                  </Button>
                </div>
              </div>
            )}
          </Section>
          <Section title="Limits">
            <Field label="Max agents" info={`Live holders at once, everyone together; 1 to ${MAX_AGENTS_LIMIT}.`}>
              <input
                className="llp-in llp-in-n"
                type="number"
                min={1}
                max={MAX_AGENTS_LIMIT}
                aria-label="Max agents"
                value={max}
                onChange={(e) => setMax(e.target.value)}
              />
            </Field>
            <Field label="Per workspace" info="At most this many of the agents at once may be one workspace's, the owner's included.">
              {pw.length > 0 && (
                <table className="llp-mini" aria-label="Per workspace">
                  <tbody>
                    {pw.map((p, i) => (
                      <tr key={i}>
                        <td className="mono">
                          {pwServed.some((q) => q.tenant === p.tenant) ? (
                            p.tenant
                          ) : (
                            <select
                              className="llp-in"
                              aria-label={`Workspace of cap ${i + 1}`}
                              value={p.tenant}
                              onChange={(e) => setPw(pw.map((q, j) => (j === i ? { ...q, tenant: e.target.value } : q)))}
                            >
                              {[p.tenant, ...pwFree].map((t) => (
                                <option key={t}>{t}</option>
                              ))}
                            </select>
                          )}
                        </td>
                        <td>
                          <input
                            className="llp-in llp-in-n"
                            type="number"
                            min={1}
                            max={MAX_AGENTS_LIMIT}
                            aria-label={`${p.tenant} max agents`}
                            value={p.max}
                            onChange={(e) => setPw(pw.map((q, j) => (j === i ? { ...q, max: e.target.value } : q)))}
                          />
                        </td>
                        <td>
                          <Button size="sm" kind="ghost" aria-label={`Remove cap for ${p.tenant}`} onClick={() => setPw(pw.filter((_, j) => j !== i))}>
                            Remove
                          </Button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              {pwFree.length > 0 && (
                <span>
                  <Button size="sm" onClick={() => setPw([...pw, { tenant: pwFree[0]!, max: '1' }])}>
                    Add workspace cap
                  </Button>
                </span>
              )}
            </Field>
            {hasClasses && (
              <Field label="Model classes" info="A step of a class no plan serves waits parked, holding nothing.">
                <div className="llp-checks">
                  {MODEL_CLASSES.map((c) => (
                    <button
                      key={c}
                      type="button"
                      className="c-chip is-pick"
                      aria-pressed={classes.includes(c)}
                      onClick={() => setClasses(classes.includes(c) ? classes.filter((x) => x !== c) : [...classes, c])}
                    >
                      {c}
                    </button>
                  ))}
                </div>
              </Field>
            )}
          </Section>
          <Section
            title="Lends"
            aside={
              <Button size="sm" onClick={() => go({ kind: 'lend', id: a.account_id })}>
                Lend
              </Button>
            }
          >
            {a.lend_to.length > 0 && (
              <ul className="llp-list">
                {a.lend_to.map((t) => (
                  <li key={t} className="llp-lent">
                    <span className="mono">{t}</span>
                    <LendUse a={a} t={t} />
                    <Button size="sm" kind="ghost" aria-label={`Edit lend to ${t}`} onClick={() => go({ kind: 'lend', id: a.account_id, to: t })}>
                      Edit
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </Section>
          <Section title="Voice it" info="Serves Voice it as its first tier. Platform admins only.">
            <Switch on={voice} onFlip={() => setVoice(!voice)} label="Share with Voice it" />
            {voiceDirty && (
              <Field label={`Type ${a.label} to confirm`}>
                <input
                  className="llp-in mono"
                  aria-label={`Type ${a.label} to confirm`}
                  autoComplete="off"
                  spellCheck={false}
                  value={typed}
                  onChange={(e) => setTyped(e.target.value)}
                />
              </Field>
            )}
          </Section>
          <Section title="Name" info="Display text only; the label stays the plan's id.">
            <input className="llp-in" aria-label="Display name" placeholder={a.label} value={name} onChange={(e) => setName(e.target.value)} />
          </Section>
        </>
      )}
    </SheetFrame>
  )
}

function LendSheet({
  a,
  board,
  edit,
  close,
  back,
  reread,
}: {
  a: Plan
  board: AccountsBoard
  edit?: string
  close: () => void
  back: () => void
  reread: () => Promise<void>
}) {
  const free = (board.tenants ?? []).map((t) => t.tenant_id).filter((t) => t !== a.owner_tenant && !a.lend_to.includes(t))
  const top = maxOf(a)
  const editing = edit !== undefined && a.lend_to.includes(edit) ? edit : null
  const [to, setTo] = useState(editing ?? (board.tenants === null ? '' : (free[0] ?? '')))
  const [agents, setAgents] = useState(String((editing !== null ? lendCap(a, editing) : null) ?? Math.min(2, top)))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)

  const send = async (lendTo: string[] | Record<string, number>) => {
    setBusy(true)
    setError(null)
    const r = await setAccountLending(a.account_id, lendTo)
    const refused = failed(r)
    if (refused === null) await reread()
    setBusy(false)
    setError(refused)
    if (refused === null) setTo('')
  }
  const target = to.trim()
  const n = Number(agents)
  const lent = target !== '' && a.lend_to.includes(target)
  const ok = target !== '' && target !== a.owner_tenant && Number.isInteger(n) && n >= 1 && n <= top
  // Every lend with its cap: one carried over keeps the plan's own max agents, the limit it already has.
  const withCap = () => ({ ...Object.fromEntries(a.lend_to.map((t) => [t, lendCap(a, t) ?? top])), [target]: n })
  const pick = (t: string) => {
    setTo(t)
    const cap = a.lend_to.includes(t) ? lendCap(a, t) : null
    if (cap !== null) setAgents(String(cap))
  }

  return (
    <SheetFrame
      title={`Lend ${accountName(a)}`}
      sub={
        <>
          <span className="mono">{a.label}</span>
          <span>
            {a.assigned} / {top} agents
          </span>
        </>
      }
      onBack={back}
      onClose={close}
    >
      <Failure error={error} />
      <div className="llp-f-row">
        <Field label="Workspace" info="Its agents may take this plan after their own.">
          {board.tenants === null ? (
            <input className="llp-in mono" aria-label="Workspace" autoComplete="off" spellCheck={false} value={to} onChange={(e) => pick(e.target.value)} />
          ) : (
            <select className="llp-in" aria-label="Workspace" value={to} onChange={(e) => pick(e.target.value)}>
              <option value="">—</option>
              {[...a.lend_to, ...free].map((t) => (
                <option key={t}>{t}</option>
              ))}
            </select>
          )}
        </Field>
        <Field label="Agents at once" info={`Required, 1 to ${top}: within the plan's own max agents. Enforced where the hold is granted.`}>
          <input
            className="llp-in llp-in-n"
            type="number"
            min={1}
            max={top}
            aria-label="Agents at once"
            value={agents}
            onChange={(e) => setAgents(e.target.value)}
          />
        </Field>
      </div>
      <div className="llp-btnrow">
        <Button kind="primary" disabled={!ok || busy} onClick={() => void send(withCap())}>
          {lent ? 'Save cap' : 'Lend'}
        </Button>
        <Button kind="ghost" onClick={back}>
          Cancel
        </Button>
      </div>
      {a.lend_to.length > 0 && (
        <Section title="Lent now" info="Ending a lend or narrowing its cap closes nothing: the workspace's holders run on; its next grant over the cap is refused.">
          <table className="llp-mini">
            <tbody>
              {a.lend_to.map((t) => (
                <tr key={t}>
                  <td className="mono">{t}</td>
                  <td data-l="Agents">
                    <LendUse a={a} t={t} />
                  </td>
                  <td>
                    <Button size="sm" kind="ghost" aria-label={`End lend to ${t}`} disabled={busy} onClick={() => void send(a.lend_to.filter((x) => x !== t))}>
                      End
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      )}
    </SheetFrame>
  )
}

function RemoveSheet({ a, close, back, done }: { a: Plan; close: () => void; back: () => void; done: () => Promise<void> }) {
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const remove = async () => {
    setBusy(true)
    setError(null)
    const refused = failed(await removeAccount(a.account_id))
    if (refused === null) {
      await done()
      return
    }
    setBusy(false)
    setError(refused)
  }
  return (
    <SheetFrame title={`Remove plan ${accountName(a)}`} onBack={back} onClose={close}>
      <Failure error={error} />
      <p className="llp-del-line">
        {a.assigned === 1 ? '1 holder moves at its next turn.' : `${a.assigned} holders move at their next turn.`}
        <Info about="Remove" tip="Live holds close now. The plan's secret is kept, so a plan removed by mistake can be signed in again." />
      </p>
      {a.lend_to.length > 0 && (
        <dl className="llp-kv">
          <dt>Lent to</dt>
          <dd className="mono">{a.lend_to.join(', ')}</dd>
        </dl>
      )}
      <Field label={`Type ${a.label} to remove`}>
        <input
          className="llp-in mono"
          aria-label={`Type ${a.label} to remove`}
          autoComplete="off"
          spellCheck={false}
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
        />
      </Field>
      <div className="llp-btnrow">
        <Button kind="danger-filled" disabled={typed !== a.label} busy={busy ? 'Removing' : false} onClick={() => void remove()}>
          Remove plan
        </Button>
        <Button kind="ghost" onClick={back}>
          Cancel
        </Button>
      </div>
    </SheetFrame>
  )
}

/** Opens the vendor's sign-in in a new tab, from the press itself so no blocker stops it. */
function openSignIn(url: string): void {
  window.open(url, '_blank', 'noopener')
}

function AddPlan({ close, done }: { close: () => void; done: () => Promise<void> }) {
  const [how, setHow] = useState<'here' | 'link'>('here')
  const [label, setLabel] = useState('')
  const [max, setMax] = useState(String(DEFAULT_MAX_AGENTS))
  const [email, setEmail] = useState('')
  const [code, setCode] = useState('')
  const [auth, setAuth] = useState<{ url: string; state: string } | null>(null)
  const [link, setLink] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)

  const clean = label.trim()
  const maxN = Number(max)
  const maxOk = Number.isInteger(maxN) && maxN >= 1 && maxN <= MAX_AGENTS_LIMIT

  const start = async () => {
    setBusy(true)
    setError(null)
    const r = await beginAccountSignIn({ label: clean, lend_to: [] })
    setBusy(false)
    if (r.status === 'ok') setAuth({ url: r.data.authorize_url, state: r.data.state })
    else setError(failed(r))
  }

  const finish = async () => {
    if (auth === null) return
    setBusy(true)
    setError(null)
    const r = await finishAccountSignIn({ state: auth.state, code: code.trim() })
    if (r.status !== 'ok') {
      setBusy(false)
      setError(failed(r))
      return
    }
    if (maxN !== DEFAULT_MAX_AGENTS) {
      const refused = failed(await setAccountMaxAgents(r.data.account.account_id, maxN))
      if (refused !== null) {
        setBusy(false)
        setError({ ...refused, message: `Plan ${clean} added; max agents not set: ${refused.message}` })
        return
      }
    }
    await done()
  }

  const mint = async () => {
    setBusy(true)
    setError(null)
    const r = await mintSetupLink({ kind: 'pool-account', label: clean, owner: email.trim() })
    setBusy(false)
    if (r.status === 'ok') setLink(r.data.url ?? `${window.location.origin}${r.data.path}`)
    else setError(failed(r))
  }

  return (
    <SheetFrame title="Add plan" sub={<span>Claude</span>} onClose={close}>
      <Failure error={error} />
      <Field label="Signed in by">
        <Radios
          label="Signed in by"
          value={how}
          onPick={(h) => {
            setHow(h)
            setError(null)
          }}
          options={[
            { key: 'here', label: 'Me, here' },
            { key: 'link', label: "The plan's owner" },
          ]}
        />
      </Field>
      <Field label="Label" info="The plan's secret is named after it, so it is fixed once added; the display name can change later.">
        <input
          className="llp-in mono"
          aria-label="Label"
          autoComplete="off"
          spellCheck={false}
          disabled={auth !== null || link !== null}
          value={label}
          onChange={(e) => setLabel(e.target.value)}
        />
      </Field>
      {how === 'here' ? (
        <>
          <Field label="Max agents">
            <input className="llp-in llp-in-n" type="number" min={1} max={MAX_AGENTS_LIMIT} aria-label="Max agents" value={max} onChange={(e) => setMax(e.target.value)} />
          </Field>
          <ol className="llp-steps">
            <li className={auth === null ? 'is-now' : 'is-done'}>
              <span className="llp-step-n">1</span>
              <span className="llp-step-t">Claude sign-in</span>
              {auth === null ? (
                <Button size="sm" disabled={clean === '' || busy} onClick={() => void start()}>
                  Start sign-in
                </Button>
              ) : (
                <Button size="sm" onClick={() => openSignIn(auth.url)}>
                  Open sign-in
                </Button>
              )}
              <Info about="Claude sign-in" tip="Opens Claude's sign-in in a new tab. Sign in with the plan's account; it shows a one-time code." />
            </li>
            {auth !== null && (
              <li className="is-now">
                <span className="llp-step-n">2</span>
                <span className="llp-step-t">Code</span>
                <input className="llp-in mono" aria-label="Sign-in code" autoComplete="off" spellCheck={false} value={code} onChange={(e) => setCode(e.target.value)} />
              </li>
            )}
          </ol>
          <div className="llp-btnrow">
            {auth !== null && (
              <Button kind="primary" disabled={code.trim() === '' || !maxOk || busy} onClick={() => void finish()}>
                Add plan
              </Button>
            )}
            <Button kind="ghost" onClick={close}>
              Cancel
            </Button>
          </div>
        </>
      ) : (
        <>
          <Field label="Owner's email" info="A one-time link bound to this verified email.">
            <input className="llp-in" type="email" aria-label="Owner's email" disabled={link !== null} value={email} onChange={(e) => setEmail(e.target.value)} />
          </Field>
          {link !== null && (
            <Field label="Link">
              <input className="llp-in mono" aria-label="Link" readOnly value={link} onFocus={(e) => e.currentTarget.select()} />
            </Field>
          )}
          <div className="llp-btnrow">
            {link === null && (
              <Button kind="primary" disabled={clean === '' || !email.includes('@') || busy} onClick={() => void mint()}>
                Create link
              </Button>
            )}
            <Button kind="ghost" onClick={close}>
              {link === null ? 'Cancel' : 'Done'}
            </Button>
          </div>
        </>
      )}
    </SheetFrame>
  )
}

/* ---------------- the page ---------------- */

/**
 * The head's `?`: the old board's topics that still describe this table --
 * the window a percentage is of, the state a failed refresh leaves, and why
 * the holder count is advisory. The others were about the old board's
 * ranking, its colour bands and figures this table does not draw.
 */
const PLAN_TOPICS: readonly TopicId[] = ['binding-window', 'refresh-token-required', 'advisory-vs-lease']

function Plans({ board, reread }: { board: AccountsBoard; reread: () => Promise<void> }) {
  const linked = useLinkedParam('account')
  const [sheet, setSheet] = useState<Sheet | null>(null)
  const [rowError, setRowError] = useState<{ id: string; error: ApiError } | null>(null)
  const tenant = board.page.tenant_id
  const plans = (board.page.accounts as Plan[]).filter(isPlan)
  const find = (id: string) => plans.find((p) => p.account_id === id) ?? null

  const active: Sheet | null = sheet ?? (linked !== null ? { kind: 'manage', id: linked } : null)
  const acc = active !== null && active.kind !== 'add' ? find(active.id) : null
  // A sheet is drawn for Add plan, or for a plan the list has; a `?account=`
  // naming a plan not in it draws neither the sheet nor its scrim.
  const drawn = active?.kind === 'add' || acc !== null
  const close = () => {
    setSheet(null)
    if (linked !== null) goAccount(null)
  }
  const open = (s: Sheet) => {
    if (s.kind === 'manage') {
      setSheet(null)
      goAccount(s.id)
    } else setSheet(s)
  }
  const back = () => setSheet(null)
  const resume = async (a: Plan) => {
    setRowError(null)
    const refused = failed(await setAccountState(a.account_id, 'AVAILABLE', ''))
    if (refused === null) await reread()
    else setRowError({ id: a.account_id, error: refused })
  }

  return (
    <div className={`llp-page${drawn ? ' has-sheet' : ''}`}>
      {plans.length === 0 ? (
        <section className="llp-empty" aria-label="No plans">
          <h2>
            No plans
            <Info about="No plans" tip="Agents take a plan first; with none they fall back to the API keys of their vendor." />
          </h2>
          <span className="ctl-mark is-zero" title="The broker answered; it holds none">
            read · 0
          </span>
          <Button kind="primary" onClick={() => open({ kind: 'add' })}>
            Add plan
          </Button>
        </section>
      ) : (
        <>
          <div className="llp-bar1">
            <span className="llp-grow" />
            <Button kind="primary" onClick={() => open({ kind: 'add' })}>
              Add plan
            </Button>
          </div>
          <Cards board={board} sel={acc?.account_id ?? null} open={open} onResume={(a) => void resume(a)} rowError={rowError} />
        </>
      )}
      {drawn && <div className="llp-scrim" onClick={close} aria-hidden="true" />}
      {active?.kind === 'add' && (
        <AddPlan
          close={close}
          done={async () => {
            await reread()
            setSheet(null)
          }}
        />
      )}
      {active?.kind === 'manage' && acc !== null && (
        <Manage a={acc} owned={tenant === null || acc.owner_tenant === tenant} close={close} go={open} reread={reread} />
      )}
      {active?.kind === 'lend' && acc !== null && <LendSheet a={acc} board={board} edit={active.to} close={close} back={back} reread={reread} />}
      {active?.kind === 'remove' && acc !== null && (
        <RemoveSheet
          a={acc}
          close={close}
          back={back}
          done={async () => {
            setSheet(null)
            if (linked !== null) goAccount(null)
            await reread()
          }}
        />
      )}
    </div>
  )
}

/**
 * The Plans sub-page: `Screen` draws the one head (the tab's title and its
 * (i)) and the Plans | API Keys strip in every read state; the body is the
 * table and its sheets once the account list has been read.
 */
export function LlmPlansScreen({ title, info, nav }: { title: string; info?: ReactNode; nav?: ReactNode }) {
  // A WRITE RE-READS THE LIST, and the newer of the two reads is drawn: the
  // screen's own (its first read and its head's refresh) or this one.
  const [fresh, setFresh] = useState<AccountsBoard | null>(null)
  const reread = async () => {
    const r = await loadAccountsBoard()
    if (r.status === 'ok' || r.status === 'stale') setFresh(r.data)
  }
  return (
    // Re-read every 30 s (60 s on a phone), as the old board did, paused while the tab is hidden.
    <Screen title={title} help={PLAN_TOPICS} info={info} nav={nav} load={loadAccountsBoard} pollMs={capacityPoll(ACCOUNTS_POLL_MS)} shape="table">
      {(data) => <Plans board={fresh !== null && fresh.readAt >= data.readAt ? fresh : data} reread={reread} />}
    </Screen>
  )
}
