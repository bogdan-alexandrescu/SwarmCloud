/**
 * ONE ACCOUNT, OPENED (#2222, proposal A's F4 side sheet and P3 phone screen).
 *
 * The parts of the detail that answer the page's question for ONE account,
 * in mock-up A's order: its verdict (the same chip and words as its row),
 * both windows against their floors, the agent cap, the organisation it was
 * signed in to, its owner and loans, what to do about a duplicate, and its own
 * forecast. The holds and the controls (lending, state, the credential, Remove)
 * stay Accounts.tsx's, so their behaviour and confirmations are the ones the
 * page already had; `Detail` there lays the two halves out in one column.
 *
 * Every figure is one `GET /v1/accounts` serves or one derived from served
 * fields (the forecast's pace). What it does not serve is a dash with its
 * reason. Another workspace's account is never named: a duplicate's other side
 * is named only when it is in this read, which the broker sends only inside
 * the reader's own workspace.
 */
import { useEffect, useRef, useState, type ReactNode } from 'react'

import { accountName } from './accountName'
import {
  FAMILIES,
  HOUR_MS,
  KINDS,
  RECORD_KEEPS,
  SERIES_HOURS,
  STATUS_WORD,
  familyOf,
  hourlyBars,
  incidentSummary,
  incidentTitle,
  incidentsOf,
  kindCount,
  kindLabel,
  limitedBands,
  windowWord,
  type HealthFamily,
  type Incident,
  type LimitedBand,
} from './accountHealth'
import {
  loadAccountHealth,
  setAccountMaxAgents,
  type AccountHealth,
  type AccountHealthCounts,
  type AccountHealthEvent,
  type AccountHealthStatus,
} from './api'
import { usePhoneTables } from './capacityPoll'
import { Button } from './components'
import { clearsOf, whyOf, WindowCell, ageOf } from './AccountsTable'
import {
  AGED_AFTER_MIN,
  DEFAULT_MAX_AGENTS,
  accountPaceOf,
  canTake,
  groupOf,
  inTime,
  isDuplicateBlock,
  blocksOf,
  maxAgentsOf,
  organisationLine,
  pct,
  slotsLine,
  utcAt,
  type PoolAccount,
} from './accountsPool'
import type { ApiError, Result } from './fetch'
import { addressToPath } from './paths'
import { FIVE_HOUR, SEVEN_DAY } from './types'

/** The most an owner may set (`MAX_AGENTS_LIMIT` in swarm-api routes/accounts.py and the broker). */
export const MAX_AGENTS_LIMIT = 50

/** The head: the row's chip, its words, and when it clears. */
export function SheetVerdict({
  a,
  scope,
  now,
  names,
}: {
  a: PoolAccount
  scope: string | null
  now: number
  names: ReadonlyMap<string, string>
}) {
  const line = slotsLine(a, scope)
  const can = canTake(a, scope)
  const g = groupOf(a, scope)
  const c = clearsOf(a, scope, now)
  const tone = can === null ? 'is-unknown' : can ? 'is-yes' : line?.startsWith('Full') ? 'is-full' : 'is-no'
  return (
    <div className="acct-sv">
      <p className="acct-sv-chip">
        <span className={`acct-chip ${tone}`}>{line ?? 'The broker does not say whether it can take a new agent'}</span>
      </p>
      <p className="acct-sv-why">
        <span className="acct-sv-k">What blocks it:</span> {whyOf(a, scope, now, names)}
      </p>
      {g !== 'now' && g !== 'unknown' && (
        <p className="acct-sv-why">
          <span className="acct-sv-k">Clears:</span> <b>{c.head}</b>
          {c.sub !== null && <> &middot; {c.sub}</>}
        </p>
      )}
    </div>
  )
}

/** Both windows, large, against their floors, and how old the reading is. */
export function SheetWindows({ a, now }: { a: PoolAccount; now: number }) {
  return (
    <section className="acct-ss" aria-label="Windows">
      <h4>Windows</h4>
      <div className="acct-ss-wins">
        <WindowCell a={a} k={FIVE_HOUR} now={now} />
        <WindowCell a={a} k={SEVEN_DAY} now={now} />
      </div>
      <p className="acct-ss-note">
        {ageOf(a, now)}.{a.observed_at !== null && !a.stale && ` After ${AGED_AFTER_MIN} min a reading counts at half.`}
        {/* THE PARTIAL MARK OF AN OLD READING (QA-049), the cue that survives
            greyscale: its figures are real but not current. */}
        {a.observed_at !== null && a.stale && (
          <>
            {' '}
            <span className="ctl-mark is-partial">partial</span>
          </>
        )}
      </p>
    </section>
  )
}

/**
 * The account whose cap was just saved: the reload that follows remounts the
 * screen, and that account's `Change the cap` takes focus back when it mounts.
 */
let capFocusBack: string | null = null

/**
 * THE AGENT CAP, AND ITS CHANGE (#2222). `PUT /v1/accounts/{id}/max-agents`
 * through swarm-api's proxy (PR 2270), owner only, 1 to 50. A borrower reads
 * the number and no control: the route refuses anyone but the owner, so a
 * control there would be one that always fails.
 */
export function AgentCap({ a, owned, reload }: { a: PoolAccount; owned: boolean; reload: () => void }) {
  const served = maxAgentsOf(a)
  const max = served ?? DEFAULT_MAX_AGENTS
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(String(max))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const n = Number(draft)
  const valid = /^\d+$/.test(draft.trim()) && n >= 1 && n <= MAX_AGENTS_LIMIT
  // FOCUS GOES INTO THE EDITOR AND BACK OUT TO ITS CONTROL (visual QA pass 1
  // capacity-08). `Change the cap` unmounted under the keyboard and left focus
  // on <body>, outside the sheet; after `Save cap` it landed on the sheet's
  // Close. Opening focuses `Agents at once`; Cancel and Save hand focus back
  // to `Change the cap`. Save's reload remounts the screen, so that hand-over
  // is held outside the component (`capFocusBack`) and the new button takes it
  // on mount, before the sheet's own focus effect looks.
  const change = useRef<HTMLButtonElement>(null)
  const field = useRef<HTMLInputElement>(null)
  const back = useRef(false)
  useEffect(() => {
    if (editing) {
      field.current?.focus()
      return
    }
    const remounted = capFocusBack === a.account_id
    if (!back.current && !remounted) return
    back.current = false
    if (remounted) capFocusBack = null
    change.current?.focus()
    // Keyed on the editor opening and closing; the account is fixed per sheet.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editing])
  const close = () => {
    back.current = true
    setEditing(false)
  }
  const save = async () => {
    setBusy(true)
    setError(null)
    const res = await setAccountMaxAgents(a.account_id, n)
    setBusy(false)
    if (res.status === 'ok') {
      capFocusBack = a.account_id
      close()
      reload()
    } else if (res.status === 'error' || res.status === 'stale') {
      setError(res.error)
    }
  }
  return (
    <div className="acct-cap">
      <p className="acct-ss-note">
        {served === null ? (
          <>
            At most <span className="acct-dash" title="This broker does not serve max_agents">&mdash;</span> agents at once:
            this broker does not say, and the deployed default is {DEFAULT_MAX_AGENTS}.
          </>
        ) : (
          <>
            At most <b>{max}</b> agent{max === 1 ? '' : 's'} at once.
          </>
        )}
        {a.observed_at === null && ' Until its first reading it takes one.'}{' '}
        {owned ? (
          !editing && (
            <button
              ref={change}
              type="button"
              className="c-link acct-cap-change"
              onClick={() => {
                setDraft(String(max))
                setError(null)
                setEditing(true)
              }}
            >
              Change the cap
            </button>
          )
        ) : (
          <>Its owner sets it.</>
        )}
        {owned && !editing && <> (1 to {MAX_AGENTS_LIMIT}, owner only)</>}
      </p>
      {owned && editing && (
        <form
          className="acct-cap-edit"
          // ESCAPE IS THE EDITOR'S FIRST (visual QA pass 2 cap2-04): it reached
          // the sheet's document handler and closed the whole sheet. Here it
          // closes only the editor, focus back on `Change the cap`; a second
          // Escape is the sheet's.
          onKeyDown={(e) => {
            if (e.key !== 'Escape' || busy) return
            e.preventDefault()
            e.stopPropagation()
            close()
          }}
          onSubmit={(e) => {
            e.preventDefault()
            if (valid && !busy) void save()
          }}
        >
          <label>
            <span>Agents at once</span>
            <input
              ref={field}
              type="number"
              inputMode="numeric"
              min={1}
              max={MAX_AGENTS_LIMIT}
              step={1}
              value={draft}
              disabled={busy}
              aria-invalid={!valid}
              aria-describedby={`acct-cap-hint-${a.account_id}`}
              onChange={(e) => setDraft(e.target.value)}
            />
          </label>
          <Button kind="primary" size="sm" type="submit" busy={busy ? 'saving…' : false} disabled={!valid || n === max}>
            Save cap
          </Button>
          <Button kind="ghost" size="sm" disabled={busy} onClick={close}>
            Cancel
          </Button>
          <small id={`acct-cap-hint-${a.account_id}`} className={valid ? 'acct-dash' : 'warn-text'}>
            {valid
              ? 'Lowering it moves no agent already on it; the pool applies it at the next hand-out.'
              : `A whole number from 1 to ${MAX_AGENTS_LIMIT}. To stop it taking agents, pause it.`}
          </small>
          {error !== null && (
            <p className="warn-text acct-cap-err" role="alert">
              Not saved: {error.message}
            </p>
          )}
        </form>
      )}
    </div>
  )
}

/** A key and its value, as mock-up A's `Account` list draws them. */
function Kv({ k, children }: { k: string; children: ReactNode }) {
  return (
    <>
      <dt>{k}</dt>
      <dd>{children}</dd>
    </>
  )
}

/** Signed in to, owner, lent to (with Edit lending for the owner), id. */
export function SheetAccount({
  a,
  scope,
  owned,
  onEditLending,
  editing,
}: {
  a: PoolAccount
  scope: string | null
  owned: boolean
  onEditLending: () => void
  editing: boolean
}) {
  const org = organisationLine(a)
  return (
    <section className="acct-ss" aria-label="Account">
      <h4>Account</h4>
      <dl className="acct-kv">
        <Kv k="Signed in to">
          {org ?? (
            <span className="acct-dash" title="The broker records the organisation at add and refresh">
              &mdash; not recorded yet
            </span>
          )}
        </Kv>
        <Kv k="Owner">
          <span className="mono">{a.owner_tenant}</span>
          {owned ? ' (this workspace)' : scope !== null ? `, lent to ${scope}` : ''}
        </Kv>
        <Kv k="Lent to">
          {!owned ? (
            <span className="acct-dash">only its owner sees its loans</span>
          ) : a.lend_to.length === 0 ? (
            'not lent'
          ) : (
            a.lend_to.map((t, i) => (
              <span key={t}>
                {i > 0 && ', '}
                <span className="mono acct-id">{t}</span>
              </span>
            ))
          )}
          {owned && !editing && (
            <>
              {' '}
              <button type="button" className="c-link acct-edit-lending" onClick={onEditLending}>
                Edit lending
              </button>
            </>
          )}
        </Kv>
        <Kv k="Id">
          <span className="mono acct-id">{a.account_id}</span>
        </Kv>
      </dl>
    </section>
  )
}

/**
 * WHAT TO DO ABOUT A DUPLICATE, in this account's detail (#2222). A proven
 * pair (`duplicate_of`, the broker's organisation id) withholds the newer
 * registration; a possible one (identical readings on two polls) is advisory
 * and both are still handed out. The other side is named only when this read
 * holds it, which is only inside this workspace (BGD, Q2); otherwise it is
 * "another workspace", and nothing more.
 */
export function SheetDuplicate({
  a,
  accounts,
  owned,
}: {
  a: PoolAccount
  accounts: readonly PoolAccount[]
  owned: boolean
}) {
  const byId = new Map(accounts.map((x) => [x.account_id, x]))
  const withheld = blocksOf(a).some(isDuplicateBlock)
  const provenOf = a.duplicate_of ?? blocksOf(a).find(isDuplicateBlock)?.duplicate_of ?? null
  const proven = withheld || a.duplicate_of != null || (a.duplicate_reason != null && a.duplicate_reason !== '')
  const possible = a.possible_duplicate ?? null
  if (!proven && possible === null) return null
  const x = accountName(a)
  const otherId = proven ? provenOf : (possible?.of ?? null)
  const other = otherId === null ? null : (byId.get(otherId) ?? null)
  const y = other === null ? null : accountName(other)
  const where = y ?? 'an account in another workspace'
  return (
    <section className="acct-ss acct-ss-dup" aria-label="Duplicate">
      <h4>{proven ? 'One subscription, registered twice' : 'Possibly one subscription, registered twice'}</h4>
      <p>
        {proven ? (
          withheld ? (
            <>
              {x} was signed in to the same organisation as {where}. The pool hands out only {y ?? 'the other registration'}, so{' '}
              {x} adds no capacity.
            </>
          ) : (
            <>
              {where === y ? `${y} was` : 'Another workspace’s account was'} signed in to the same organisation later. The
              pool keeps handing out {x} and withholds the other, which adds no capacity.
            </>
          )
        ) : (
          <>
            {x} and {where} read identically on two polls. The pool still hands out both; if they are one subscription,
            the second adds no capacity.
          </>
        )}
      </p>
      <p>
        {owned ? (
          proven && !withheld ? (
            <>
              <b>What to do:</b> nothing here. {y !== null ? `Fix it on ${y}` : 'Its owner fixes it in their workspace'}.
            </>
          ) : (
            <>
              <b>What to do:</b> Replace the credential below, signed in to the organisation you meant from that
              organisation&rsquo;s browser profile (one claude.ai login can hold two, and the sign-in takes the one that is
              selected). Or remove {x}.
            </>
          )
        ) : (
          <>
            <b>What to do:</b> {a.owner_tenant} owns {x} and can fix it.
          </>
        )}
      </p>
    </section>
  )
}

/** This account's own forecast, from today's reading (BGD, "From today's readings"). */
export function SheetForecast({ a, now }: { a: PoolAccount; now: number }) {
  const p = accountPaceOf(a, now)
  // A RESET ALREADY PAST (visual QA pass 2 cap2-05): the reading is from the
  // week before it, so no pace can be taken to that reset. The forecast says
  // the window should have reset, in the Windows block's `passed` words.
  const passed = p !== null && p.read && p.resetsAt <= now
  let say: ReactNode
  if (p === null) {
    say = (
      <>
        <span className="acct-dash">&mdash;</span> no 7-day reading to take a pace from.
      </>
    )
  } else if (passed) {
    say = (
      <>
        Its week should have reset: <b>reset {utcAt(p.resetsAt, now)} passed</b>, and nothing has been read since. The
        forecast waits for a fresh reading.
      </>
    )
  } else if (p.underNow) {
    say = (
      <>
        Its 7-day window is under its {pct(p.floor)} floor now, and its week resets <b>{utcAt(p.resetsAt, now)}</b>.
      </>
    )
  } else if (!p.read) {
    say = (
      <>
        Its week reset and nothing has been read since, so there is no pace yet. The next reset is{' '}
        <b>{utcAt(p.resetsAt, now)}</b>.
      </>
    )
  } else if (p.floorAt === null) {
    say = (
      <>
        At this pace it stays above its {pct(p.floor)} floor until its week resets <b>{utcAt(p.resetsAt, now)}</b>.
      </>
    )
  } else {
    say = (
      <>
        At this pace it reaches its {pct(p.floor)} floor near <b>{utcAt(p.floorAt, now)}</b>, and its week resets{' '}
        <b>{utcAt(p.resetsAt, now)}</b>.
      </>
    )
  }
  return (
    <section className="acct-ss" aria-label="Forecast">
      <h4>Forecast</h4>
      <p>{say}</p>
      {p !== null && p.read && !passed && (
        <p className="acct-ss-note">
          Pace: {pct(p.perHour * 24)} of the week a day, its 7-day share used over the hours since its week opened.
        </p>
      )}
      <p className="acct-ss-note">Laptop sessions on this subscription spend the same window.</p>
    </section>
  )
}

// ---------------------------------------------------------------------------
// Health (#2302, ACCOUNT-HEALTH-SHEET: mock-up A's F2 and F4, D's P2 at 390)
// ---------------------------------------------------------------------------

/** HH:MM:SSZ today, `Oct 23 HH:MM:SSZ` before: an event's time to the second. */
function eventAt(at: string, now: number): string {
  const t = Date.parse(at)
  const d = new Date(t)
  const s = String(d.getUTCSeconds()).padStart(2, '0')
  return utcAt(t, now).replace(/Z$/, `:${s}Z`)
}

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`
}

const sumOf = (c: AccountHealthCounts | null, fam?: HealthFamily): number =>
  c === null ? 0 : KINDS.reduce((n, k) => (fam === undefined || k.fam === fam ? n + c[k.k] : n), 0)

/** A family's mark: shape as well as colour, so it survives greyscale (mock-up A's `fm`). */
export function FamilyMark({ fam }: { fam: HealthFamily }) {
  return (
    <svg className={`acct-hs-fm is-${fam}`} viewBox="0 0 10 10" aria-hidden="true">
      {fam === 'err' ? (
        <circle cx="5" cy="5" r="4.5" />
      ) : fam === 'lim' ? (
        <path d="M5 0 10 5 5 10 0 5Z" />
      ) : (
        <rect x=".5" y=".5" width="9" height="9" rx="1.5" />
      )}
    </svg>
  )
}

/** The broker's word, always with its icon's place and a word; a dash with its reason when there is none. */
function HealthWord({ status, extra }: { status: AccountHealthStatus | null; extra?: string }) {
  if (status === null) {
    return (
      <span className="acct-hs-st is-unknown" title="The record carries no status word this console knows">
        &mdash; Not served
      </span>
    )
  }
  return (
    <span className={`acct-hs-st is-${status}`}>
      {STATUS_WORD[status]}
      {extra ? ` · ${extra}` : ''}
    </span>
  )
}

/** One sentence of why, from the record (mock-up A's `why`). */
function healthWhy(h: AccountHealth, bands: LimitedBand[], now: number): string | null {
  const c24 = h.counts.last_24h
  const c1 = h.counts.last_1h
  if (h.status === 'limited') {
    const until = h.limited_until === null ? NaN : Date.parse(h.limited_until)
    const band = bands.filter((b) => b.hits > 0).slice(-1)[0]
    const refusals = c24 === null ? null : c24.assign_refused
    const tail = refusals === null ? '' : `: ${plural(refusals, 'refusal')} at assignment in 24 h`
    if (Number.isNaN(until)) return `The broker withholds it until its window resets${tail}.`
    const head =
      band !== undefined && band.window !== null
        ? `Its ${windowWord(band.window)} window refused at ${utcAt(band.from, now)} and resets ${utcAt(until, now)} (${inTime(until, now)}).`
        : `It is limited until ${utcAt(until, now)} (${inTime(until, now)}).`
    return `${head} The broker withholds it until then${tail}.`
  }
  if (h.status === 'erroring' && c1 !== null) {
    const errs = sumOf(c1, 'err')
    return `${plural(errs, 'provider error')} in the last hour, ${c1.overloaded} of them 529 overloaded. It still takes work.`
  }
  if (h.status === 'healthy' && c24 !== null) {
    const n = sumOf(c24)
    if (n === 0) return null
    return sumOf(c24, 'lim') === 0 ? `${plural(n, 'event')} in 24 h, none of them a limit.` : `${plural(n, 'event')} in 24 h.`
  }
  return null
}

const SPANS = [
  ['1 h', 'last_1h'],
  ['24 h', 'last_24h'],
  ['7 d', 'last_7d'],
] as const

/** A cell of the counts table: a dash with its reason when the span was not served, never a 0. */
function CountCell({ c, n }: { c: AccountHealthCounts | null; n: number }) {
  if (c === null) {
    return (
      <td className="is-z">
        <span className="acct-dash" title="The record does not serve this span">
          &mdash;
        </span>
      </td>
    )
  }
  return <td className={n > 0 ? 'is-nz' : 'is-z'}>{n}</td>
}

/** Counts by kind for 1 h, 24 h and 7 d, grouped by family (mock-up A's `countsTable`, zero rows folded). */
export function HealthCounts({ h }: { h: AccountHealth }) {
  const c7 = h.counts.last_7d
  const hidden: string[] = []
  const rows: ReactNode[] = []
  for (const f of FAMILIES) {
    const kinds = KINDS.filter((k) => k.fam === f.f)
    rows.push(
      <tr key={f.f} className="is-fam">
        <th scope="row">
          <FamilyMark fam={f.f} />
          {f.label}
        </th>
        {SPANS.map(([, s]) => (
          <CountCell key={s} c={h.counts[s]} n={sumOf(h.counts[s], f.f)} />
        ))}
      </tr>,
    )
    for (const k of kinds) {
      // A kind with none in 7 days folds into the caption -- but only when 7
      // days WAS served: an unserved span cannot show a kind is zero.
      if (c7 !== null && c7[k.k] === 0) {
        hidden.push(k.label.toLowerCase())
        continue
      }
      rows.push(
        <tr key={k.k}>
          <th scope="row">{k.label}</th>
          {SPANS.map(([, s]) => (
            <CountCell key={s} c={h.counts[s]} n={h.counts[s]?.[k.k] ?? 0} />
          ))}
        </tr>,
      )
    }
  }
  return (
    <>
      <table className="acct-hs-counts" aria-label="Events by kind, last hour, day and week">
        <thead>
          <tr>
            <th scope="col">Kind</th>
            {SPANS.map(([w]) => (
              <th key={w} scope="col">
                {w}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows}
          <tr className="is-tot">
            <th scope="row">All kinds</th>
            {SPANS.map(([, s]) => (
              <CountCell key={s} c={h.counts[s]} n={sumOf(h.counts[s])} />
            ))}
          </tr>
        </tbody>
      </table>
      {hidden.length > 0 && <p className="acct-ss-note">None in 7 days, so not drawn as rows: {hidden.join(', ')}.</p>}
    </>
  )
}

/** D's P2 counts: the last hour, day and week stacked, each total with its kinds. */
function HealthStats({ h }: { h: AccountHealth }) {
  return (
    <div className="acct-hs-stats">
      {(
        [
          ['Last hour', h.counts.last_1h],
          ['Last 24 h', h.counts.last_24h],
          ['Last 7 days', h.counts.last_7d],
        ] as const
      ).map(([w, c]) => (
        <div key={w} className="acct-hs-stat">
          <span className="acct-hs-stat-k">{w}</span>
          {c === null ? (
            <span className="acct-hs-stat-v acct-dash" title="The record does not serve this span">
              &mdash;
            </span>
          ) : (
            <span className="acct-hs-stat-v">{sumOf(c)}</span>
          )}
          <small>
            {c === null
              ? 'not served'
              : sumOf(c) === 0
                ? 'none'
                : KINDS.filter((k) => c[k.k] > 0)
                    .map((k) => kindCount(k.k, c[k.k]))
                    .join(' · ')}
          </small>
        </div>
      ))}
    </div>
  )
}

const DAY_MS = 24 * HOUR_MS
/** The narrowest a limited stretch is drawn, so a 5-hour one is a mark the reader can find at 340 px. */
const BAND_MIN_PX = 8

/**
 * PER HOUR, 7 DAYS (mock-up A's `hourlyChart`): a stacked bar per hour by
 * family, the account's limited stretches shaded from each hit to its
 * window's reset, a dashed line at now. The counts table above is its
 * textual equivalent; the chart's label says what it holds.
 */
export function HealthSeries({ h, now, width }: { h: AccountHealth; now: number; width: number }) {
  const bars = hourlyBars(h.hourly, now)
  const bands = limitedBands(h.events, h.limited_until, now)
  const W = width
  const Hh = 130
  const L = 30
  const top = 18
  const bot = 20
  const ph = Hh - top - bot
  const start = bars[0]?.t ?? now
  const bw = (W - L - 5) / SERIES_HOURS
  const x = (t: number) => L + ((t - start) / HOUR_MS) * bw
  const mx = Math.max(1, ...bars.map((b) => b.n))
  const total = bars.reduce((n, b) => n + b.n, 0)
  const narrow = W < 420
  const days: number[] = []
  for (let d = start - (start % DAY_MS) + DAY_MS; d <= now; d += DAY_MS) days.push(d)
  const nx = Math.min(W - 2, x(now))
  const label = `Events per hour over 7 days: ${plural(total, 'event')}, at most ${mx} in one hour; ${plural(
    bands.length,
    'limited stretch',
    'limited stretches',
  )} shaded`
  // A 5-hour stretch is ~12 px of 436 (2.4 px an hour) and its hit's own bars
  // stand in it, so a stretch is drawn at least BAND_MIN_PX wide, and also as
  // a solid strip along the plot's top edge, drawn after the bars so none
  // covers it: the legend's key promises a mark the reader can find.
  const spans = bands.flatMap((b) => {
    let x0 = Math.max(L, x(b.from))
    const x1 = Math.min(W - 5, x(b.to))
    if (x1 <= L) return []
    const w = Math.max(BAND_MIN_PX, x1 - x0)
    x0 = Math.max(L, Math.min(x0, W - 5 - w))
    return [{ b, x0, w }]
  })
  return (
    <figure className="acct-hs-series">
      <svg className="acct-hs-chart" viewBox={`0 0 ${W} ${Hh}`} role="img" aria-label={label}>
        {spans.map(({ b, x0, w }, i) => (
          <g key={i} className="acct-hs-band" data-window={b.window ?? ''}>
            <rect x={x0.toFixed(1)} y={top} width={w.toFixed(1)} height={ph} />
            <line x1={x0.toFixed(1)} x2={x0.toFixed(1)} y1={top} y2={top + ph} />
            {w > 40 && b.window !== null && (
              <text x={(x0 + 3).toFixed(1)} y={11}>
                {windowWord(b.window)}
              </text>
            )}
          </g>
        ))}
        <line className="acct-hs-grid" x1={L} x2={W} y1={top + ph} y2={top + ph} />
        <line className="acct-hs-grid" x1={L} x2={W} y1={top} y2={top} />
        <text x={L - 6} y={top + 10} textAnchor="end">
          {mx}
        </text>
        <text x={L - 6} y={top + ph} textAnchor="end">
          0
        </text>
        {days.map((d, i) => {
          const dx = x(d)
          const dt = new Date(d)
          const show = !narrow || i % 2 === 0
          // The month on every tick (visual QA pass 2 cap2-15): at 390 a bare
          // `23 25 27` named no month; a phone shows every other day instead.
          const word = `${MONTH_SHORT[dt.getUTCMonth()]} ${dt.getUTCDate()}`
          const edge = dx + 3 + word.length * 7 > W
          return (
            <g key={d}>
              <line className="acct-hs-grid" x1={dx.toFixed(1)} x2={dx.toFixed(1)} y1={top} y2={top + ph + 4} />
              {show && (
                <text x={(edge ? W : dx + 3).toFixed(1)} y={Hh - 4} textAnchor={edge ? 'end' : undefined}>
                  {word}
                </text>
              )}
            </g>
          )
        })}
        {/* The now line first, behind the bars, so the current hours' bars are never under it. */}
        <line className="acct-hs-now" x1={nx.toFixed(1)} x2={nx.toFixed(1)} y1={top - 2} y2={top + ph} />
        {bars
          .map((b, i) => ({ b, i }))
          .filter(({ b }) => b.n > 0)
          .sort((p, q) => q.b.n - p.b.n || p.i - q.i)
          .map(({ b, i }) => {
            const wn = Math.max(2.6, bw - 0.6)
            const X = Math.min(nx - 2 - wn, L + (i + 0.5) * bw - wn / 2)
            let y = top + ph
            return (
              <g key={i} className="acct-hs-bar">
                <title>
                  {`${utcAt(b.t, now)}–${utcAt(b.t + HOUR_MS, now)}: ${FAMILIES.map((f) => `${b[f.f]} ${f.label.toLowerCase()}`).join(' · ')}`}
                </title>
                {(['err', 'lim', 'cred'] as const).map((f) => {
                  if (b[f] === 0) return null
                  const hh = (b[f] / mx) * ph
                  y -= hh
                  return (
                    <rect key={f} className={`is-${f}`} x={X.toFixed(2)} y={y.toFixed(2)} width={wn.toFixed(2)} height={hh.toFixed(2)} />
                  )
                })}
              </g>
            )
          })}
        {spans.map(({ b, x0, w }, i) => (
          <rect
            key={i}
            className="acct-hs-band-mark"
            data-window={b.window ?? ''}
            x={x0.toFixed(1)}
            y={top - 4}
            width={w.toFixed(1)}
            height={3}
          />
        ))}
      </svg>
      <figcaption className="acct-hs-legend">
        {FAMILIES.map((f) => (
          <span key={f.f}>
            <FamilyMark fam={f.f} />
            {f.label}
          </span>
        ))}
        {bands.length > 0 && (
          <span>
            <i className="acct-hs-bandkey" aria-hidden="true" />
            limited: from the hit to the window&rsquo;s reset
          </span>
        )}
      </figcaption>
    </figure>
  )
}

const MONTH_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** Which Agents list a task most likely sits in: a limit parks it (Waiting); an error or a credential ends it (Recent). */
function taskPath(e: AccountHealthEvent): string {
  return addressToPath(`work/task/${encodeURIComponent(e.task_id as string)}`, familyOf(e.kind) === 'lim' ? 'waiting' : 'recent')
}

/** One event: time, kind, window and reset, the redacted message, tenant and task (mock-up A's `eventsList`). */
function HealthEventItem({ e, now }: { e: AccountHealthEvent; now: number }) {
  return (
    <li className="acct-hs-ev">
      <div className="acct-hs-ev-l1">
        <span className="acct-hs-ev-t">{eventAt(e.at, now)}</span>
        <span className="acct-hs-kd">
          <FamilyMark fam={familyOf(e.kind)} />
          {kindLabel(e.kind)}
        </span>
        {e.window !== null && (
          <span className="acct-dash">
            <b>{windowWord(e.window)}</b>
            {e.reset_at !== null && <> · resets {utcAt(e.reset_at, now)}</>}
          </span>
        )}
      </div>
      {e.message !== null && e.message !== '' ? (
        <div className="acct-hs-msg">{e.message}</div>
      ) : (
        !e.own &&
        e.tenant_id === null && (
          <div className="acct-ss-note">Another workspace&rsquo;s event: its tenant, task and message are not shown, and its time is to the hour.</div>
        )
      )}
      {(e.tenant_id !== null || e.task_id !== null) && (
        <div className="acct-hs-ev-l3">
          {e.tenant_id !== null && (
            <span>
              tenant <span className="mono">{e.tenant_id}</span>
            </span>
          )}
          {e.task_id !== null && (
            <span>
              task{' '}
              <a className="mono" href={taskPath(e)} title="Opens Agents at this task">
                {e.task_id.length > 13 ? `${e.task_id.slice(0, 13)}…` : e.task_id}
              </a>
            </span>
          )}
        </div>
      )}
    </li>
  )
}

/** What the list holds against the record (mock-up A's `recordNote`). */
function recordNote(h: AccountHealth): string {
  const shown = h.events.length
  const week = sumOf(h.counts.last_7d)
  if (h.counts.last_7d !== null && week > shown) {
    return `The record keeps the last ${RECORD_KEEPS} events: drawn here are ${shown} of the ${week} in 7 days. The ${week - shown} older ones count in the hourly series and the 7 d column, and their messages are gone.`
  }
  return `All ${plural(shown, 'event')} of 7 days, newest first (the record keeps the last ${RECORD_KEEPS}).`
}

/** D's incident card: the open one expanded, a closed one folded to its latest message. */
function IncidentCard({ x, now, expanded }: { x: Incident; now: number; expanded: boolean }) {
  const [open, setOpen] = useState(expanded)
  const latest = x.events[x.events.length - 1]
  const when =
    x.fam === 'lim' && x.to !== null
      ? x.open
        ? `since ${utcAt(x.from, now)} · clears ${utcAt(x.to, now)} (${inTime(x.to, now)})`
        : `${utcAt(x.from, now)} – ${utcAt(x.to, now)}`
      : x.open
        ? `since ${utcAt(x.from, now)}`
        : x.events.length > 1
          ? `${utcAt(x.from, now)} – ${utcAt(x.last, now)}`
          : utcAt(x.from, now)
  return (
    <article className={`acct-hs-inc is-${x.fam}${x.open ? ' is-open' : ''}`}>
      <div className="acct-hs-inc-h">
        <FamilyMark fam={x.fam} />
        <h5>{incidentTitle(x)}</h5>
        <span className={`acct-hs-inc-chip${x.open ? ' is-open' : ''}`}>{x.open ? 'open' : 'closed'}</span>
      </div>
      <p className="acct-ss-note">
        {when} · {incidentSummary(x)}
      </p>
      {open ? (
        <ul className="acct-hs-evs">
          {x.events
            .slice()
            .reverse()
            .map((e, i) => (
              <HealthEventItem key={`${e.at}-${i}`} e={e} now={now} />
            ))}
        </ul>
      ) : (
        <>
          {latest !== undefined && latest.message !== null && latest.message !== '' && <div className="acct-hs-msg">{latest.message}</div>}
          <button type="button" className="c-link" aria-expanded={false} onClick={() => setOpen(true)}>
            {plural(x.events.length, 'event')} ▾
          </button>
        </>
      )}
    </article>
  )
}

type HealthRead = Result<AccountHealth> | { status: 'threw'; message: string }

/**
 * THE ACCOUNT'S HEALTH SECTION (#2302). ONE READ WHEN THE SHEET OPENS --
 * `GET /v1/accounts/{id}/health`, keyed by the account id alone, so the page's
 * own 30 s poll re-rendering the sheet never asks again -- and one more each
 * time its reader presses Refresh. Nothing here polls.
 *
 * What it draws, by what the read returned (mock-up A's F4):
 *   * the record with events: the word and why, counts by kind for 1 h, 24 h
 *     and 7 d, the 7-day hourly series with its limited stretches, and the
 *     last 50 events; at 390, D's P2 instead -- the three counts stacked, the
 *     series, then the incidents, the open one expanded;
 *   * the record with none: a measured zero said once in words, no chart;
 *   * a 404: the record is not served here -- a dash and its reason, NEVER a 0,
 *     and Refresh rather than Try again (a retry would 404 again);
 *   * needs re-auth: the fix first;
 *   * any other failure: unreadable, a dash with what went wrong, and Try again.
 */
export function SheetHealth({ a, owned }: { a: PoolAccount; owned: boolean }) {
  const phone = usePhoneTables()
  const [nonce, setNonce] = useState(0)
  const [res, setRes] = useState<HealthRead>({ status: 'loading', since: 0 })
  const [now, setNow] = useState(() => Date.now())
  const [reading, setReading] = useState(true)
  const id = a.account_id
  useEffect(() => {
    let live = true
    setReading(true)
    setRes((prev) => (prev.status === 'ok' || prev.status === 'stale' ? prev : { status: 'loading', since: Date.now() }))
    const settle = (next: HealthRead) => {
      if (!live) return
      setNow(Date.now())
      setReading(false)
      setRes((prev) =>
        // A failed refresh with a record in hand keeps it, marked stale: the
        // reader sees what was read and that the last read failed.
        next.status === 'error' && (prev.status === 'ok' || prev.status === 'stale')
          ? { status: 'stale', data: prev.data, fetchedAt: prev.fetchedAt, error: next.error }
          : next,
      )
    }
    // A read that throws (it should not: `read` classifies every failure) is
    // an unreadable read, said as one, rather than a broken sheet.
    try {
      loadAccountHealth(id).then(settle, (why: unknown) =>
        settle({ status: 'threw', message: why instanceof Error ? why.message : 'The read did not complete.' }),
      )
    } catch (why) {
      settle({ status: 'threw', message: why instanceof Error ? why.message : 'The read did not complete.' })
    }
    return () => {
      live = false
    }
  }, [id, nonce])

  const busy = reading
  const refresh = (
    <button type="button" className="c-link acct-hs-refresh" disabled={busy} onClick={() => setNonce((n) => n + 1)}>
      {busy
        ? 'Reading…'
        : // A 404 is a route this deployment does not serve: a retry would 404 again.
          (res.status === 'error' && res.error.kind !== 'not_found') || res.status === 'threw'
          ? 'Try again'
          : 'Refresh'}
    </button>
  )
  const head = (word: ReactNode, note: string | null) => (
    <div className="acct-hs-head">
      <h4>Health</h4>
      {word}
      {note !== null && <span className="acct-ss-note">{note}</span>}
      {refresh}
    </div>
  )
  const path = `GET /v1/accounts/${id}/health`

  if (res.status === 'loading') {
    return (
      <section className="acct-ss acct-hs" aria-label="Health" aria-busy="true">
        {head(null, null)}
        <p className="acct-dash">Reading the health record…</p>
      </section>
    )
  }
  if (res.status === 'empty') {
    // `loadAccountHealth` never answers `empty`; a record is always a record.
    return null
  }
  if (res.status === 'error' && res.error.kind === 'not_found') {
    return (
      <section className="acct-ss acct-hs is-not-served" aria-label="Health">
        {head(<HealthWord status={null} />, null)}
        <p>
          <span className="acct-dash">&mdash;</span> This deployment does not serve the account health record:{' '}
          <code>{path}</code> answered 404. Its quota broker predates the record (ACCOUNT-429-SCOPE). Rate-limit hits,
          errors and refusals are <b>unknown</b> here, not zero.
        </p>
      </section>
    )
  }
  if (res.status === 'error' || res.status === 'threw') {
    const why = res.status === 'threw' ? res.message : res.error.message
    return (
      <section className="acct-ss acct-hs is-unreadable" aria-label="Health">
        {head(<span className="acct-hs-st is-unknown">&mdash; Unreadable</span>, null)}
        {/* Not an alert: the sheet's own actions say their failures with
            `role="alert"`, and a read the reader did not just ask for is not
            one of them. */}
        <p className="acct-hs-unread">
          <span className="acct-dash">&mdash;</span> The health record could not be read: {why}. Its counts and events are{' '}
          <b>unknown</b> until a read succeeds, not zero.
        </p>
      </section>
    )
  }

  const h = res.data
  const bands = limitedBands(h.events, h.limited_until, now)
  const until = h.status === 'limited' && h.limited_until !== null ? h.limited_until : null
  const lastBand = bands.filter((b) => b.window !== null).slice(-1)[0]
  const extra = until === null ? undefined : `${lastBand ? `${windowWord(lastBand.window)} ` : ''}until ${utcAt(until, now)}`
  const readAt = h.observed_at ?? new Date(res.fetchedAt).toISOString()
  const why = h.status === 'reauth_needed' ? null : healthWhy(h, bands, now)
  const nothing = h.events.length === 0 && h.counts.last_7d !== null && sumOf(h.counts.last_7d) === 0
  const width = phone ? 340 : 436
  const incidents = phone ? incidentsOf(h.events, h.status, now) : []
  const firstOpen = incidents.findIndex((x) => x.open)
  // D's P2: the open one expanded, the closed ones folded -- so with none open, all fold.
  const expandAt = firstOpen

  return (
    <section className={`acct-ss acct-hs${phone ? ' is-phone' : ''}`} aria-label="Health">
      {head(<HealthWord status={h.status} extra={extra} />, `read ${utcAt(readAt, now)}`)}
      {res.status === 'stale' && (
        <p className="warn-text">
          The last read failed ({res.error.message}); this is the record read at {utcAt(res.fetchedAt, now)}.
        </p>
      )}
      {h.status === 'reauth_needed' && (
        <div className="acct-hs-callout" role="note">
          <b>Needs re-auth</b>
          <p>
            The provider refused its credential. The broker assigns it nothing until someone signs in again; work that
            would have used it waits for another account.{' '}
            {owned ? (
              <>
                <b>Sign in again</b> is below, with this account&rsquo;s actions.
              </>
            ) : (
              <>
                Its owner, <span className="mono">{a.owner_tenant}</span>, signs in again.
              </>
            )}
          </p>
        </div>
      )}
      {why !== null && <p>{why}</p>}
      {nothing ? (
        <>
          <p className="acct-hs-zero">No events in 7 days: no limits, errors or credential trouble.</p>
          <p className="acct-ss-note">The record was read and is empty. That is a measured zero, so no chart is drawn.</p>
        </>
      ) : phone ? (
        <>
          <HealthStats h={h} />
          <HealthSeries h={h} now={now} width={width} />
          <div className="acct-hs-sub">
            <h5>
              {plural(incidents.length, 'incident')}, {plural(h.events.length, 'event')}
            </h5>
          </div>
          {incidents.map((x, i) => (
            <IncidentCard key={`${x.fam}-${x.from}`} x={x} now={now} expanded={i === expandAt} />
          ))}
        </>
      ) : (
        <>
          <HealthCounts h={h} />
          <div className="acct-hs-sub">
            <h5>Per hour, 7 days</h5>
          </div>
          <HealthSeries h={h} now={now} width={width} />
          <div className="acct-hs-sub">
            <h5>Events</h5>
            <span className="acct-ss-note">{recordNote(h)}</span>
          </div>
          {h.events.length > 0 && (
            <ul className="acct-hs-evs" aria-label="Events, newest first">
              {h.events.slice(0, RECORD_KEEPS).map((e, i) => (
                <HealthEventItem key={`${e.at}-${i}`} e={e} now={now} />
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  )
}
