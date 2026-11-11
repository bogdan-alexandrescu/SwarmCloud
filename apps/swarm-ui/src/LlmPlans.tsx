/**
 * CAPACITY › LLM PROVIDERS › PLANS (docs/design/llm-providers.md §6, the
 * owner's pick of 10 November 2026, direction "Tables and a side sheet").
 *
 * ONE TABLE, GROUPED BY VENDOR. A row is a plan: its state, the five-hour and
 * seven-day windows, live holders over max agents, the Voice it mark and how
 * many workspaces it is lent to, with one line per lend under it. Every
 * change happens in a side sheet on the right, the table still in view: Add
 * plan, Manage (every plan control) and Lend, with Remove behind Manage.
 *
 * READS AND WRITES ARE api.ts's PLAN CALLS, nothing else: the account list
 * (`loadAccountsBoard`), `setAccountState`, `setAccountMaxAgents`,
 * `setAccountName`, `setAccountLending`, `setAccountModelClasses`,
 * `refreshAccount`, `removeAccount`, the sign-in pair and `mintSetupLink`,
 * and the Voice it pool route (`setAccountPools`, which still lives beside
 * the old board's Pools control in Accounts.tsx). A write that succeeds
 * re-reads the list rather than patching a copy; a write that fails says why
 * in its sheet and leaves the row as the platform last said it was.
 *
 * WHAT IS NOT DRAWN, because no route serves it yet: a lend's agents cap and
 * spend cap, and adding a ChatGPT plan from the browser (its sign-in is the
 * plugin's `sc account add --provider openai`). Both are questions in the
 * lane's PR, not dead controls.
 *
 * No explanatory prose on the surface: every definition sits behind an (i).
 */
import { Fragment, useEffect, useRef, useState, type ReactNode } from 'react'
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
  setAccountState,
  type AccountsBoard,
  type ServedAccount,
} from './api'
import { accountName } from './accountName'
import { MODEL_CLASSES, classesToSave, servedClasses, setAccountPools } from './Accounts'
import { Info } from './AccountsVerdict'
import { inVoiceItPool, poolsOf } from './AccountsTable'
import { DEFAULT_MAX_AGENTS, utcAt, type PoolAccount } from './accountsPool'
import { MAX_AGENTS_LIMIT } from './AccountSheet'
import { paneHref, useLinkedParam } from './capacityPoll'
import { Button } from './components'
import type { ApiError, Result } from './fetch'
import { Screen } from './Shell'
import { FIVE_HOUR, SEVEN_DAY, type AccountWindow } from './types'
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

/** A usage window: the share used and when it resets; a window past its reset is not a reading. */
function Win({ w, name, now }: { w: AccountWindow | undefined; name: string; now: number }) {
  if (w === undefined || !Number.isFinite(w.utilization)) return <span className="llp-dim">—</span>
  if (w.reset) return <span className="llp-dim" title={`${name}: reset since its last reading`}>reset</span>
  const pct = Math.round(w.utilization * 100)
  const tone = pct >= 100 ? 'is-full' : pct >= 80 ? 'is-near' : ''
  const at = utcAt(w.resets_at, now)
  return (
    <span className={`llp-over ${tone}`} title={`${name}: ${pct}% used, resets ${at}`}>
      <span className="llp-over-n">
        {pct}%<span className="llp-dim"> · {at}</span>
      </span>
      <span className="llp-bar" aria-hidden="true">
        <b style={{ width: `${Math.min(100, pct)}%` }} />
      </span>
    </span>
  )
}

function VoiceMark({ on }: { on: boolean }) {
  return on ? (
    <span className="llp-voice" title="Shared with Voice it">
      Voice it
    </span>
  ) : (
    <span className="llp-dim" title="Not shared with Voice it">
      —
    </span>
  )
}

/** The plan's name, its label, its plan and, when it is borrowed, whose it is. */
function Who({ a, tenant }: { a: Plan; tenant: string | null }) {
  const v = vendorOf(a.provider)
  const named = typeof a.display_name === 'string' && a.display_name !== ''
  const tier = a.organisation?.plan ? `${v?.plan ?? a.provider} ${a.organisation.plan}` : null
  const classes = servedClasses(a)
  return (
    <span className="llp-who">
      {named && (
        <span className="llp-name" title={accountName(a)}>
          {accountName(a)}
        </span>
      )}
      <span className="llp-meta">
        <span className={`mono llp-label${named ? '' : ' is-lead'}`} title={a.label}>
          {a.label}
        </span>
        {tier !== null && <span>{tier}</span>}
        {tenant !== null && a.owner_tenant !== tenant && (
          <span className="mono" title="Lent to this workspace by">
            {a.owner_tenant}
          </span>
        )}
      </span>
      {classes.length > 0 && (
        <span className="llp-classes" title="Model classes it serves">
          {classes.join(' · ')}
        </span>
      )}
    </span>
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

/* ---------------- the table ---------------- */

const COLS = 8

function Table({
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
  const owned = (a: Plan) => tenant === null || a.owner_tenant === tenant
  return (
    <div className="llp-tablewrap">
      <table className="llp-table" aria-label="Plans">
        <thead>
          <tr>
            <th>Plan</th>
            <th>State</th>
            <th>
              Agents <Info about="Agents" tip="Live holders over max agents, counted from the grant (LEASED), not from RUNNING." />
            </th>
            <th className="llp-opt">5-hour</th>
            <th className="llp-opt">7-day</th>
            <th className="llp-opt">Voice it</th>
            <th className="llp-opt">Lent</th>
            <th className="llp-act">
              <span className="llp-vh">Actions</span>
            </th>
          </tr>
        </thead>
        {byVendor(plans).map((g) => (
          <tbody key={g.provider}>
            <tr className="llp-group">
              <th colSpan={COLS} scope="colgroup">
                {vendorOf(g.provider)?.label ?? <span className="mono">{g.provider}</span>} <span className="llp-dim">{g.rows.length}</span>
              </th>
            </tr>
            {g.rows.map((a) => (
              <Fragment key={a.account_id}>
                <tr className={`llp-row${sel === a.account_id ? ' is-sel' : ''}`} aria-selected={sel === a.account_id || undefined}>
                  <td data-l="">
                    <Who a={a} tenant={tenant} />
                  </td>
                  <td data-l="State">
                    <StateMark a={a} />
                  </td>
                  <td data-l="Agents">
                    <Over used={a.assigned} cap={maxOf(a)} title="Live holders over max agents" />
                  </td>
                  <td data-l="5-hour" className="llp-opt">
                    <Win w={a.windows[FIVE_HOUR]} name="5-hour window" now={now} />
                  </td>
                  <td data-l="7-day" className="llp-opt">
                    <Win w={a.windows[SEVEN_DAY]} name="7-day window" now={now} />
                  </td>
                  <td data-l="Voice it" className="llp-opt">
                    <VoiceMark on={inVoiceItPool(a)} />
                  </td>
                  <td data-l="Lent" className="llp-num llp-opt">
                    {a.lend_to.length > 0 ? a.lend_to.length : <span className="llp-dim">—</span>}
                  </td>
                  <td className="llp-act">
                    {owned(a) && (
                      <span className="llp-acts">
                        {a.state === 'PAUSED' && (
                          <Button size="sm" onClick={() => onResume(a)}>
                            Resume
                          </Button>
                        )}
                        <Button size="sm" onClick={() => open({ kind: 'manage', id: a.account_id })}>
                          Manage
                        </Button>
                      </span>
                    )}
                    {rowError?.id === a.account_id && <Failure error={rowError.error} />}
                  </td>
                </tr>
                {a.lend_to.map((t) => (
                  <tr key={t} className="llp-lend">
                    <td colSpan={COLS - 1} data-l="Lent to">
                      <span className="llp-lend-who">
                        <span className="llp-lend-arm" aria-hidden="true" />
                        <span className="mono">{t}</span>
                      </span>
                    </td>
                    <td className="llp-act">
                      {owned(a) && (
                        <Button size="sm" kind="ghost" onClick={() => open({ kind: 'lend', id: a.account_id })}>
                          Edit lend
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </Fragment>
            ))}
          </tbody>
        ))}
      </table>
    </div>
  )
}

/* ---------------- the side sheet ---------------- */

type Sheet = { kind: 'add' } | { kind: 'manage'; id: string } | { kind: 'lend'; id: string } | { kind: 'remove'; id: string }

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

  const hasClasses = a.provider === 'anthropic'
  const maxN = Number(max)
  const maxDirty = max !== String(maxOf(a))
  const maxOk = Number.isInteger(maxN) && maxN >= 1 && maxN <= MAX_AGENTS_LIMIT
  const classesDirty = hasClasses && !sameList(classesToSave(classes), classesToSave(classesServed))
  const nameDirty = name.trim() !== (a.display_name ?? '')
  const voiceDirty = voice !== voiceServed
  const dirty = maxDirty || classesDirty || nameDirty || voiceDirty
  const ready = dirty && (!maxDirty || maxOk) && (!voiceDirty || typed === a.label) && (!hasClasses || classes.length > 0)

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
                  <li key={t} className="mono">
                    {t}
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

function LendSheet({ a, board, close, back, reread }: { a: Plan; board: AccountsBoard; close: () => void; back: () => void; reread: () => Promise<void> }) {
  const free = (board.tenants ?? []).map((t) => t.tenant_id).filter((t) => t !== a.owner_tenant && !a.lend_to.includes(t))
  const [to, setTo] = useState(board.tenants === null ? '' : (free[0] ?? ''))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)

  const send = async (lendTo: string[]) => {
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
  const ok = target !== '' && target !== a.owner_tenant && !a.lend_to.includes(target)

  return (
    <SheetFrame
      title={`Lend ${accountName(a)}`}
      sub={
        <>
          <span className="mono">{a.label}</span>
          <span>
            {a.assigned} / {maxOf(a)} agents
          </span>
        </>
      }
      onBack={back}
      onClose={close}
    >
      <Failure error={error} />
      <Field label="Workspace" info="Its agents may take this plan after their own.">
        {board.tenants === null ? (
          <input className="llp-in mono" aria-label="Workspace" autoComplete="off" spellCheck={false} value={to} onChange={(e) => setTo(e.target.value)} />
        ) : (
          <select className="llp-in" aria-label="Workspace" value={to} onChange={(e) => setTo(e.target.value)}>
            <option value="">—</option>
            {free.map((t) => (
              <option key={t}>{t}</option>
            ))}
          </select>
        )}
      </Field>
      <div className="llp-btnrow">
        <Button kind="primary" disabled={!ok || busy} onClick={() => void send([...a.lend_to, target])}>
          Lend
        </Button>
        <Button kind="ghost" onClick={back}>
          Cancel
        </Button>
      </div>
      {a.lend_to.length > 0 && (
        <Section title="Lent now" info="Ending a lend closes nothing: the workspace's holders run on; its next grant is refused.">
          <table className="llp-mini">
            <tbody>
              {a.lend_to.map((t) => (
                <tr key={t}>
                  <td className="mono">{t}</td>
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

function Plans({ board, reread }: { board: AccountsBoard; reread: () => Promise<void> }) {
  const linked = useLinkedParam('account')
  const [sheet, setSheet] = useState<Sheet | null>(null)
  const [rowError, setRowError] = useState<{ id: string; error: ApiError } | null>(null)
  const tenant = board.page.tenant_id
  const plans = (board.page.accounts as Plan[]).filter(isPlan)
  const find = (id: string) => plans.find((p) => p.account_id === id) ?? null

  const active: Sheet | null = sheet ?? (linked !== null ? { kind: 'manage', id: linked } : null)
  const acc = active !== null && active.kind !== 'add' ? find(active.id) : null
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
    <div className={`llp-page${active !== null ? ' has-sheet' : ''}`}>
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
          <Table board={board} sel={acc?.account_id ?? null} open={open} onResume={(a) => void resume(a)} rowError={rowError} />
        </>
      )}
      {active !== null && <div className="llp-scrim" onClick={close} aria-hidden="true" />}
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
      {active?.kind === 'lend' && acc !== null && <LendSheet a={acc} board={board} close={close} back={back} reread={reread} />}
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
    <Screen title={title} info={info} nav={nav} load={loadAccountsBoard} shape="table">
      {(data) => <Plans board={fresh !== null && fresh.readAt >= data.readAt ? fresh : data} reread={reread} />}
    </Screen>
  )
}
