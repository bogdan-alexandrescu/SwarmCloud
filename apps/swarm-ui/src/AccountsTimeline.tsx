/**
 * CAPACITY › ACCOUNTS: THE TABLE / TIMELINE TOGGLE AND THE TIMELINE (#2222).
 *
 * BGD, 2026-10-24 17:33Z: build proposal A ("Answer first") with a Table /
 * Timeline view toggle whose Timeline draws every account the way proposal
 * B ("Timeline", docs/web-ui/mockups/accounts-b.html) does. The toggle is
 * "Table default, remembered": the page opens on Table, the toggle sits
 * beside the verdict, and the last choice is remembered per browser and in
 * the address (`?view=timeline`), so a view can be linked.
 *
 * WHAT THE TIMELINE ANSWERS THAT THE TABLE DOES NOT: "when", drawn rather
 * than read. On 2026-10-24 the question was not whether anything was free
 * but when anything would be; the next-free-slot line and the pool row say
 * it without arithmetic.
 *
 * Every figure is one `GET /v1/accounts` serves. The broker serves no hold
 * deadlines in the listing, so a full account's lane says how many agents
 * hold it and that no clock frees it -- not the deadline bars B drew from
 * example data. The pace is `forecastOf`'s, the same the Forecast tiles
 * under both views read.
 */
import { useState, type CSSProperties, type ReactNode } from 'react'

import { accountName } from './accountName'
import { clearsOf } from './AccountsTable'
import { WAITING_HREF, andList, type WaitRead } from './AccountsVerdict'
import {
  DEFAULT_MAX_AGENTS,
  FLOORS,
  blocksOf,
  canTake,
  forecastOf,
  freeSlots,
  groupOf,
  isCapBlock,
  isFirstReadingBlock,
  isWindowBlock,
  maxAgentsOf,
  organisationLine,
  pct,
  rankAccounts,
  utcAt,
  verdictOf,
  windowName,
  type Forecast,
  type PoolAccount,
} from './accountsPool'
import {
  NEAR_SHARE,
  axisTicks,
  axisX,
  bandHeight,
  bandsOf,
  dupMark,
  nextSlotAt,
  openRow,
  paceCloseAt,
  paceRow,
  type Band,
  type DupMark,
  type Segment,
} from './accountsTimeline'
import { linkedParam, paneHref, useLinkedParam } from './capacityPoll'
import { Segmented } from './components'
import { FIVE_HOUR, SEVEN_DAY, ageSpan, pluralise, unreadableFor } from './types'
import './styles/accounts-timeline.css'

// ---------------------------------------------------------------------------
// The toggle: Table default, remembered
// ---------------------------------------------------------------------------

export type AccountsView = 'table' | 'timeline'
/** Where this browser remembers the last choice. */
export const VIEW_KEY = 'swarm.accounts.view'

function stored(): AccountsView | null {
  try {
    const v = window.localStorage.getItem(VIEW_KEY)
    return v === 'timeline' || v === 'table' ? v : null
  } catch {
    // Storage refused (a private window, a sandboxed frame): the default.
    return null
  }
}

function remember(v: AccountsView): void {
  try {
    window.localStorage.setItem(VIEW_KEY, v)
  } catch {
    // Not remembered; the address still carries the choice.
  }
}

/** Push an in-app address and tell the router, as `Accounts.tsx` does for `?account=`. */
function follow(hash: string): void {
  const oldURL = window.location.href
  window.history.pushState(window.history.state, '', hash)
  window.dispatchEvent(new HashChangeEvent('hashchange', { oldURL, newURL: window.location.href }))
}

/**
 * The view: the address's `view` when it names one (a followed link wins),
 * else this browser's last choice, else Table. Choosing writes both, so the
 * address can be copied and the next visit opens where this one left.
 */
export function useAccountsView(): [AccountsView, (v: AccountsView) => void] {
  const linked = useLinkedParam('view')
  const [kept, setKept] = useState<AccountsView | null>(() => (typeof window === 'undefined' ? null : stored()))
  const view: AccountsView = linked === 'timeline' || linked === 'table' ? linked : (kept ?? 'table')
  const choose = (v: AccountsView) => {
    remember(v)
    setKept(v)
    // Table is the default, so it leaves the address bare rather than writing `view=table`.
    follow(paneHref('capacity/accounts', { account: linkedParam('account'), view: v === 'timeline' ? 'timeline' : null }))
  }
  return [view, choose]
}

export function ViewToggle({ view, onChange }: { view: AccountsView; onChange: (v: AccountsView) => void }) {
  return (
    <Segmented
      label="Show the accounts as"
      small
      className="acct-view"
      value={view}
      options={[
        { key: 'table', label: 'Table', title: 'Ranked by when each can take a new agent' },
        { key: 'timeline', label: 'Timeline', title: 'Every account on one axis, from now to seven days out' },
      ]}
      onChange={onChange}
    />
  )
}

// ---------------------------------------------------------------------------
// Pieces of a lane
// ---------------------------------------------------------------------------

const pctX = (x: number): string => `${Math.round(x * 100) / 100}%`

function NextLine({ at, now }: { at: number | null; now: number }) {
  if (at === null) return null
  return <i className="acct-tl-nx" style={{ left: pctX(axisX(at, now)) }} aria-hidden="true" />
}

function Axis({ now }: { now: number }) {
  return (
    <div className="acct-tl-ax" aria-hidden="true">
      {axisTicks(now).map((t) => (
        <span key={`${t.x}-${t.label}`} className={`acct-tl-tk is-${t.kind}`} style={{ left: pctX(t.x) }}>
          {t.kind === 'now' ? <b>{t.label}</b> : t.label}
        </span>
      ))}
      <span className="acct-tl-brk" style={{ left: pctX(NEAR_SHARE * 100) }} title="axis break" />
      <span className="acct-tl-zone" style={{ left: 4 }}>
        next 12 hours
      </span>
      <span className="acct-tl-zone" style={{ left: `calc(${NEAR_SHARE * 100}% + 8px)` }}>
        the week
      </span>
    </div>
  )
}

function BandView({ b, now, pace }: { b: Band; now: number; pace: { at: number; from: number; to: number } | null }) {
  const cls = ['acct-tl-band', b.key === FIVE_HOUR ? 'is-five' : '', b.known ? '' : 'is-unknown'].filter(Boolean).join(' ')
  const floor = <i className="acct-tl-fl" style={{ bottom: pctX(bandHeight(b.floor)) }} title={`floor ${pct(b.floor)}`} />
  const unknownNote = b.known ? null : <span className="acct-tl-dim"> (a window the console does not know by name)</span>
  if (b.kind === 'pending' || b.kind === 'absent') {
    return (
      <div className="acct-tl-win">
        <span className="acct-tl-tx">
          {b.name} &middot;{' '}
          <span className="acct-tl-dim">
            &mdash; {b.kind === 'pending' ? 'first reading pending' : `the provider serves no ${b.name} window for this account`}
          </span>
        </span>
        <div className={`${cls} is-empty`} />
      </div>
    )
  }
  if (b.kind === 'reset') {
    return (
      <div className="acct-tl-win">
        <span className="acct-tl-tx">
          {b.name}
          {unknownNote} &middot; <b>reset {b.resetsAt !== undefined ? utcAt(b.resetsAt, now) : '—'}</b>, no use read since
        </span>
        <div className={cls}>
          <i className="acct-tl-ar is-af" style={{ left: 0, width: '100%', height: '100%' }} />
          {floor}
        </div>
      </div>
    )
  }
  const x = b.x ?? 100
  const left = b.left ?? 0
  const tone = b.blocked ? (b.aged ? 'is-st' : 'is-bk') : 'is-ok'
  const rl = `${b.blocked ? 'clears' : 'resets'} ${b.resetsAt !== undefined ? utcAt(b.resetsAt, now) : '—'}`
  return (
    <div className="acct-tl-win">
      <span className="acct-tl-tx">
        {b.name}
        {unknownNote} &middot; <b>{pct(left)} left</b> ({pct(b.used ?? 0)} used) &middot; floor {pct(b.floor)}
        {b.aged && ' · counted at half'}
        {/* On a phone the label beside the marker is dropped; the words carry it. */}
        <span className="acct-tl-rlw"> &middot; {rl}</span>
      </span>
      <div className={cls}>
        <i className={`acct-tl-ar ${tone}`} style={{ left: 0, width: pctX(x), height: pctX(Math.max(6, bandHeight(left))) }} />
        {x < 100 && <i className="acct-tl-ar is-af" style={{ left: pctX(x), width: pctX(100 - x), height: '100%' }} />}
        {floor}
        {pace !== null && (
          <svg className="acct-tl-pace" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
            <polyline
              points={`0,${100 - bandHeight(pace.from)} ${axisX(pace.at, now)},${100 - bandHeight(pace.to)}`}
              vectorEffect="non-scaling-stroke"
            />
          </svg>
        )}
        {x < 100 && <i className={`acct-tl-rs${b.blocked ? ' is-clr' : ''}`} style={{ left: pctX(x) }} title={rl} />}
        {x < 100 && (
          <span className={`acct-tl-rl${b.blocked ? ' is-clr' : ''}${x > 78 ? ' is-lft' : ''}`} style={{ left: pctX(x) }}>
            {rl}
          </span>
        )}
      </div>
    </div>
  )
}

/** A full account's holds, as slots: the listing serves how many, not until when. */
function Holds({ a, full }: { a: PoolAccount; full: boolean }) {
  const max = maxAgentsOf(a) ?? DEFAULT_MAX_AGENTS
  const held = Math.max(0, a.assigned)
  return (
    <div className="acct-tl-holds">
      <span className="acct-tl-pips" aria-hidden="true">
        {Array.from({ length: Math.max(max, held) }, (_, i) => (
          <i key={i} className={i < held ? 'is-held' : ''} />
        ))}
      </span>
      <span>
        <b>
          {held} of {max} agents hold it
        </b>
        {full && <> &middot; frees when one finishes, no clock</>}
        <span className="acct-tl-dim"> &middot; until when: &mdash; not in the account list; its agents are in its detail</span>
      </span>
    </div>
  )
}

type Tone = 'ok' | 'warn' | 'bad' | 'park' | 'neu'

/** What holds the account, in a few words, with its tone: the lane's one verdict. */
export function laneStatus(a: PoolAccount, scope: string | null, dup: DupMark | null): { tone: Tone; text: string } {
  if (dup?.kind === 'withheld') {
    return { tone: 'park', text: `Not handed out: one subscription with ${dup.other ?? 'an account in another workspace'}` }
  }
  if (a.state === 'REAUTH_REQUIRED') return { tone: 'warn', text: 'Signed out: takes nothing until someone signs in again' }
  if (a.state === 'PAUSED') return { tone: 'park', text: a.reason ? `Paused: ${a.reason}` : 'Paused by its owner' }
  if (a.state === 'DRAINING') return { tone: 'neu', text: 'Draining: serves what it holds, takes no new agent' }
  if (a.state !== 'AVAILABLE') return { tone: 'neu', text: `${a.state.toLowerCase().replace(/_/g, ' ')}: a state this console does not recognise` }
  if (unreadableFor(a, scope)) return { tone: 'warn', text: 'Skipped here: this workspace could not read its secret' }
  const can = canTake(a, scope)
  if (can === null) return { tone: 'neu', text: '— the broker does not say whether it can take an agent' }
  if (can) return { tone: 'ok', text: 'Can take a new agent' }
  const cap = blocksOf(a).find(isCapBlock)
  if (cap !== undefined) return { tone: 'warn', text: `Full: ${cap.cap} of ${cap.max_agents} agents` }
  const first = blocksOf(a).find(isFirstReadingBlock)
  if (first !== undefined) return { tone: 'warn', text: 'First reading pending: holds its one agent until measured' }
  // The window that clears last is the one the account waits for.
  const w = blocksOf(a)
    .filter(isWindowBlock)
    .sort((x, y) => Date.parse(y.resets_at) - Date.parse(x.resets_at))[0]
  if (w !== undefined) {
    return {
      tone: 'bad',
      text: `${windowName(w.window)}: ${pct(Math.max(0, 1 - w.used))} left, under its ${pct(w.floor)} floor${w.aged_reading ? ' (an aged reading, counted at half)' : ''}`,
    }
  }
  return { tone: 'neu', text: 'the broker gave no reason' }
}

function lentPhrase(a: PoolAccount, scope: string | null): string {
  if (scope !== null && a.owner_tenant !== scope) return `lent to you by ${a.owner_tenant}`
  return a.lend_to.length === 0 ? 'not lent' : `lent to ${a.lend_to.join(', ')}`
}

function readAge(a: PoolAccount, now: number): ReactNode {
  if (a.observed_at === null) return null
  const at = Date.parse(a.observed_at)
  if (!Number.isFinite(at)) return null
  const min = Math.round((now - at) / 60_000)
  const age = min < 90 ? `${min} min` : ageSpan(now - at)
  return a.stale ? (
    <span className="acct-tl-stale"> &middot; stale: read {age} ago, counted at half</span>
  ) : (
    <> &middot; read {age} ago</>
  )
}

function DupChip({ dup }: { dup: DupMark }) {
  const text =
    dup.kind === 'withheld'
      ? `one subscription with ${dup.other ?? 'an account in another workspace'}`
      : dup.kind === 'handed'
        ? `one subscription with ${dup.other ?? 'an account in another workspace'} · this one is handed out`
        : `may be one subscription with ${dup.other ?? 'an account in another workspace'}`
  return <span className="acct-tl-dupc">{text}</span>
}

function Lane({
  a,
  scope,
  now,
  next,
  forecast,
  dup,
  chosen,
  onChoose,
}: {
  a: PoolAccount
  scope: string | null
  now: number
  next: number | null
  forecast: Forecast
  dup: DupMark | null
  chosen: boolean
  onChoose: () => void
}) {
  const st = laneStatus(a, scope, dup)
  const g = groupOf(a, scope)
  const c = clearsOf(a, scope, now)
  const max = maxAgentsOf(a) ?? DEFAULT_MAX_AGENTS
  const org = organisationLine(a)
  const full = blocksOf(a).some(isCapBlock)
  const pending = a.observed_at === null
  const pace = paceCloseAt(forecast, a, now)
  const cls = ['acct-tl-row', 'acct-tl-lane', a.stale && a.observed_at !== null ? 'is-stale' : '', dup ? 'is-dup' : '', chosen ? 'is-chosen' : '']
    .filter(Boolean)
    .join(' ')
  return (
    <div className={cls} data-account={a.account_id}>
      <div className="acct-tl-lab">
        <div className="acct-tl-nm">
          <button
            type="button"
            className="acct-open acct-tl-name"
            data-account={a.account_id}
            aria-current={chosen ? 'true' : undefined}
            onClick={onChoose}
          >
            {accountName(a)}
          </button>
          {dup && <DupChip dup={dup} />}
        </div>
        {org === null ? (
          <span className="acct-tl-org acct-tl-dim">organisation &mdash; not recorded at sign-in</span>
        ) : (
          <span className="acct-tl-org">{org}</span>
        )}
        <span className={`acct-tl-why is-${st.tone}`}>{st.text}</span>
        <span className="acct-tl-clr">
          {g === 'now' ? (
            `${pluralise(freeSlots(a), 'free slot')} of ${max}`
          ) : (
            <>
              Clears <b>{c.head}</b>
              {c.sub !== null && <> &middot; {c.sub}</>}
            </>
          )}
        </span>
        <span className="acct-tl-hl">
          {a.assigned} of {max} agents &middot; {lentPhrase(a, scope)}
          {readAge(a, now)}
        </span>
      </div>
      <div className="acct-tl-trk">
        <div className="acct-tl-in">
          <NextLine at={next} now={now} />
          {pending ? (
            <p className="acct-tl-pending">
              5-hour and 7-day: <b>first reading pending</b>. No bands until the broker reads its windows; never drawn as
              0% or 100%.
            </p>
          ) : (
            bandsOf(a, now).map((b) => (
              <BandView key={b.key} b={b} now={now} pace={b.key === SEVEN_DAY ? pace : null} />
            ))
          )}
          {(a.assigned > 0 || full) && <Holds a={a} full={full} />}
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// The pool rows and the queue
// ---------------------------------------------------------------------------

function PoolRow({
  label,
  sub,
  segs,
  now,
  next,
  phone,
  pace,
}: {
  label: string
  sub: ReactNode
  segs: Segment[] | null
  now: number
  next: number | null
  phone: boolean
  pace?: ReactNode
}) {
  return (
    <div className={`acct-tl-row is-pool${pace !== undefined ? ' is-pace' : ''}`}>
      <div className="acct-tl-lab">
        <b>{label}</b>
        <span className="acct-tl-hl">{sub}</span>
      </div>
      <div className="acct-tl-trk">
        <div className="acct-tl-in">
          <NextLine at={next} now={now} />
          {segs === null ? (
            <p className="acct-tl-pending">{pace}</p>
          ) : (
            <div className="acct-tl-pl">
              {segs.map((s, i) => {
                const x = axisX(s.from, now)
                const w = axisX(s.to, now) - x
                // THE COUNT AT NOW IS ALWAYS WRITTEN, however short its stretch: it
                // is the verdict's number, and a row that opened on the next
                // stretch's figure would contradict the head.
                const first = i === 0
                const words = phone
                  ? // A phone track is ~330px: a count under 4% would sit on its neighbour's; the key below lists every one.
                    w >= 4 || first ? String(s.open) : ''
                  : w > 9 ? (s.open === 0 ? 'dark' : `${s.open} open`) : w >= 1.8 || first ? String(s.open) : ''
                return (
                  <span
                    key={s.from}
                    className={`acct-tl-sg${s.open === 0 ? ' is-zero' : ''}${first ? ' is-first' : ''}${w < 1 && !first ? ' is-thin' : ''}`}
                    style={{ left: pctX(x), width: pctX(w) }}
                    title={`${s.open} can take an agent from ${utcAt(s.from, now)} to ${utcAt(s.to, now)}`}
                  >
                    {words}
                  </span>
                )
              })}
            </div>
          )}
          {segs !== null && phone && segs.length > 1 && (
            <div className="acct-tl-plk">
              {segs.map((s) => (
                <span key={s.from}>
                  <b>{s.open}</b> from {utcAt(s.from, now)}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

/**
 * THE WAITING AGENTS, QUEUED AGAINST THE NEXT FREE SLOT: how many start now
 * (oldest first, on the free slots), and how many wait for the next account
 * to clear. Computed from the same verdict the head says; a waiting list
 * that could not be read is a dash, never 0.
 */
function QueueRow({
  accounts,
  scope,
  now,
  next,
  waiting,
}: {
  accounts: readonly PoolAccount[]
  scope: string | null
  now: number
  next: number | null
  waiting: WaitRead
}) {
  const v = verdictOf(accounts, scope, 0)
  let pills: ReactNode
  if (waiting.kind !== 'ok') {
    pills = (
      <span className="acct-tl-qq is-now" style={{ '--x': '0%' } as CSSProperties}>
        <b>&mdash;</b> {waiting.kind === 'loading' ? 'reading the waiting list' : 'could not read the waiting list'}
      </span>
    )
  } else if (waiting.waiting.agents === 0) {
    pills = (
      <span className="acct-tl-qq is-now" style={{ '--x': '0%' } as CSSProperties}>
        <b>No agent waits</b> on the pool
      </span>
    )
  } else {
    const n = waiting.waiting.agents
    const start = Math.min(v.free, n)
    const rest = n - start
    const at = v.next
    pills = (
      <>
        {start > 0 && (
          <span className="acct-tl-qq is-now" style={{ '--x': '0%' } as CSSProperties}>
            <b>{start} can start now</b> &middot; oldest first, on {andList(v.open.map((a) => accountName(a)))}
          </span>
        )}
        {rest > 0 &&
          (at !== null ? (
            <span className="acct-tl-qq" style={{ '--x': pctX(axisX(at.at, now)) } as CSSProperties}>
              <b>
                {waiting.waiting.atLeast ? 'at least ' : ''}
                {rest} queue here
              </b>{' '}
              &middot; {accountName(at.account)} opens at {utcAt(at.at, now)}
              {v.full !== null && <>; {accountName(v.full)} frees when an agent finishes</>}
            </span>
          ) : (
            <span className="acct-tl-qq" style={{ '--x': next === null ? '0%' : pctX(axisX(next, now)) } as CSSProperties}>
              <b>
                {waiting.waiting.atLeast ? 'at least ' : ''}
                {rest} wait
              </b>{' '}
              &middot;{' '}
              {accounts.length === 0
                ? 'no account is registered'
                : v.full !== null
                  ? `${accountName(v.full)} frees when an agent finishes; no clock says when`
                  : 'no clock says when'}
            </span>
          ))}
      </>
    )
  }
  return (
    <div className="acct-tl-row is-queue">
      <div className="acct-tl-lab">
        <b>Waiting on the pool</b>
        <span className="acct-tl-hl">
          <a href={WAITING_HREF}>Open them</a> in Work &rsaquo; Agents
        </span>
      </div>
      <div className="acct-tl-trk">
        <div className="acct-tl-in">
          <NextLine at={next} now={now} />
          <div className="acct-tl-qp">{pills}</div>
        </div>
      </div>
    </div>
  )
}

function Legend({ anyStale, anyPace }: { anyStale: boolean; anyPace: boolean }) {
  return (
    <div className="acct-tl-lg">
      <span>
        <i className="acct-tl-sw is-bk" />
        under its floor
      </span>
      <span>
        <i className="acct-tl-sw is-ok" />
        over its floor
      </span>
      {anyStale && (
        <span>
          <i className="acct-tl-sw is-st" />
          stale reading, counted at half
        </span>
      )}
      <span>
        <i className="acct-tl-sw is-af" />
        past its reset: full, no use read since
      </span>
      <span>
        <i className="acct-tl-sw is-fl" />
        floor (5-hour {pct(FLOORS[FIVE_HOUR] ?? 0)}, 7-day {pct(FLOORS[SEVEN_DAY] ?? 0)})
      </span>
      <span>
        <i className="acct-tl-dm" />
        clears: back over the floor
      </span>
      <span>
        <i className="acct-tl-sw is-nx" />
        next free slot
      </span>
      {anyPace && (
        <span>
          <i className="acct-tl-sw is-fc" />
          reaches its 7-day floor at this pace
        </span>
      )}
      <span className="acct-tl-dim">Band height: share left, square-root scale. Times are UTC.</span>
    </div>
  )
}

// ---------------------------------------------------------------------------
// The Timeline
// ---------------------------------------------------------------------------

export function AccountsTimeline({
  accounts,
  scope,
  now,
  waiting,
  chosen,
  phone,
  onChoose,
  empty,
}: {
  accounts: readonly PoolAccount[]
  scope: string | null
  now: number
  waiting: WaitRead
  chosen: string | null
  phone: boolean
  onChoose: (id: string) => void
  /** What an empty pool says, drawn where the lanes would be. */
  empty?: ReactNode
}) {
  // The lanes in the Table's order, so switching views never reorders the pool.
  const lanes = rankAccounts(accounts, scope).flatMap((g) => g.accounts)
  const next = nextSlotAt(accounts, scope, now)
  const forecast = forecastOf(accounts, scope, now)
  const pace = paceRow(forecast, now)
  const anyPace = accounts.some((a) => paceCloseAt(forecast, a, now) !== null)
  const pacing = forecast.unmeasured.length > 0
    ? `— ${andList(forecast.unmeasured.map((a) => accountName(a)))} ${forecast.unmeasured.length === 1 ? 'has' : 'have'} no 7-day reading to take a pace from`
    : '— no subscription here takes agents'
  return (
    <section className={`acct-tl${phone ? ' is-phone' : ''}`} aria-label="Accounts on a time axis">
      <div className="acct-tl-row is-axis">
        <div className="acct-tl-lab">
          <b>{pluralise(accounts.length, 'account')}</b>
          <span className="acct-tl-hl">
            floors: 5-hour {pct(FLOORS[FIVE_HOUR] ?? 0)}, 7-day {pct(FLOORS[SEVEN_DAY] ?? 0)}
          </span>
        </div>
        <div className="acct-tl-trk">
          <Axis now={now} />
        </div>
      </div>
      <QueueRow accounts={accounts} scope={scope} now={now} next={next} waiting={waiting} />
      <PoolRow
        label="Accounts that can take an agent"
        sub="by the clock, if nothing more is used · a full account is not counted: no clock frees it"
        segs={accounts.length === 0 ? [{ from: now, to: now + 7 * 86_400_000, open: 0 }] : openRow(accounts, scope, now)}
        now={now}
        next={next}
        phone={phone}
      />
      <PoolRow
        label="At this pace"
        sub="7-day windows over their floor, each spent at its pace since its week opened · not full accounts or 5-hour floors · the forecast below"
        segs={pace}
        now={now}
        next={next}
        phone={phone}
        pace={pacing}
      />
      {lanes.length === 0 && (
        <div className="acct-tl-row is-none">
          <div className="acct-tl-lab">
            <b>No lanes yet</b>
            <span className="acct-tl-hl">Each account you add is a lane here</span>
          </div>
          <div className="acct-tl-trk">{empty}</div>
        </div>
      )}
      {lanes.map((a) => (
        <Lane
          key={a.account_id}
          a={a}
          scope={scope}
          now={now}
          next={next}
          forecast={forecast}
          dup={dupMark(a, accounts)}
          chosen={chosen === a.account_id}
          onChoose={() => onChoose(a.account_id)}
        />
      ))}
      {/* A never-measured account is `stale` too, with no reading to draw pale. */}
      <Legend anyStale={accounts.some((a) => a.stale && a.observed_at !== null)} anyPace={anyPace} />
    </section>
  )
}

