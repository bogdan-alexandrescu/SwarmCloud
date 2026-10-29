/**
 * THE TIMELINE VIEW OF CAPACITY › ACCOUNTS (#2222, ACCOUNTS-TIMELINE).
 *
 * BGD, 2026-10-24 17:33Z: the page is proposal A, with a Table / Timeline
 * toggle beside the verdict whose Timeline draws every account the way
 * proposal B does (`docs/web-ui/mockups/accounts-b.html`, frames B1-B6). Table
 * is the default; the last choice is remembered per browser and rides in the
 * address as `?view=timeline`, so it can be linked.
 *
 * One lane per account on a shared axis from now to seven days out (B's split
 * axis: the next 12 hours, then the week). Each window is a band whose height
 * is its share left (square-root scale), its floor a dashed line, a diamond
 * where it resets -- filled when that is where it clears back over its floor.
 * A vertical line marks the next free slot through every lane, with the
 * waiting agents queued against it; the pool row counts how many accounts can
 * take an agent over time, and the pace row does the same with each
 * subscription's week spent at this pace.
 *
 * THE WORDS SIT ABOVE THE BANDS, ON AN OPAQUE CHIP (B's review fix): no line,
 * band or marker is ever drawn through a word. Reset and clear times are in
 * the words; the diamonds stay on the track.
 *
 * SAME ANSWERS AS THE TABLE. Who can take an agent, the ranking, the reasons,
 * the clear times and the forecast are `accountsPool.ts`'s, the functions the
 * Table and the verdict use -- `forecastOf` above all, so the pace row and the
 * forecast under it cannot disagree with the Table's.
 *
 * PRESSING A LANE OPENS THE TABLE'S SIDE SHEET (BGD, 2026-10-25 10:05Z), the
 * whole screen on a phone, not proposal B's detail under the lane: the lane
 * only asks (`onChoose`), and Accounts.tsx `Pool` draws the one sheet both
 * views share. The opened lane stays marked under the sheet's scrim.
 */
import { useId, useState, type ReactNode } from 'react'

import { accountName } from './accountName'
import { ageOf, clearsOf } from './AccountsTable'
import { Forecast, WAITING_HREF, type WaitRead } from './AccountsVerdict'
import {
  DEFAULT_MAX_AGENTS,
  FLOORS,
  blocksOf,
  canTake,
  forecastOf,
  freeSlots,
  groupOf,
  isCapBlock,
  isDuplicateBlock,
  isFirstReadingBlock,
  isWindowBlock,
  maxAgentsOf,
  organisationLine,
  pct,
  rankAccounts,
  unknownWindows,
  utcAt,
  windowName,
  type Forecast as ForecastResult,
  type PoolAccount,
} from './accountsPool'
import {
  bandOf,
  heightOf,
  holdDeadlineOf,
  keptOf,
  nextFreeAt,
  onAxis,
  paceLine,
  paceSteps,
  poolSteps,
  queueOf,
  ticksOf,
  xOf,
  type AccountsView,
  type AxisKind,
  type Band,
  type LanedAccount,
  type Step,
} from './accountsLanes'
import { Segmented } from './components'
import { HelpNote } from './HelpCard'
import { FIVE_HOUR, SEVEN_DAY, pluralise, unreadableFor } from './types'
import './styles/accounts-timeline.css'

// ---------------------------------------------------------------------------
// The toggle
// ---------------------------------------------------------------------------

/**
 * Table | Timeline, beside the verdict. Each is a link to its own address
 * (`?view=`), so the choice can be copied, opened in a new tab, or followed
 * back; a plain click is routed in place by `onChange`.
 */
export function ViewToggle({
  view,
  hrefOf,
  onChange,
}: {
  view: AccountsView
  hrefOf: (v: AccountsView) => string
  onChange: (v: AccountsView) => void
}) {
  return (
    <Segmented<AccountsView>
      label="Accounts view"
      className="acct-view-toggle"
      value={view}
      onChange={onChange}
      options={[
        {
          key: 'table',
          label: 'Table',
          href: hrefOf('table'),
          title: 'The accounts as a ranked table',
        },
        {
          key: 'timeline',
          label: 'Timeline',
          href: hrefOf('timeline'),
          title: 'Every account on one axis, now to seven days out',
        },
      ]}
    />
  )
}

// ---------------------------------------------------------------------------
// Pieces
// ---------------------------------------------------------------------------

const pctX = (x: number) => `${(x * 100).toFixed(3)}%`

/** The next-free-slot line, drawn in every track cell at the same x. */
function FreeLine({ x }: { x: number | null }) {
  if (x === null) return null
  return <i className="acct-tl-free" style={{ left: pctX(x) }} aria-hidden="true" />
}

function Steps({
  steps,
  now,
  axis,
  phone,
  pace,
  label,
}: {
  steps: Step[]
  now: number
  axis: AxisKind
  phone: boolean
  pace: boolean
  label: string
}) {
  const segs = steps
    .map((s, i) => {
      const x0 = xOf(s.at, now, axis)
      const next = steps[i + 1]
      const x1 = next === undefined ? 1 : xOf(next.at, now, axis)
      return { ...s, x0, x1 }
    })
    .filter((s) => s.x1 > s.x0)
  // A SEGMENT SAYS `N open` OR NOTHING (VQ-035, visual QA pass 1). A narrow
  // one used to print its bare count -- one segment read `7`, a figure with
  // no noun beside `3 open` -- and the slivers too thin for even that were
  // unlabelled. Now a segment carries its words only where they fit, and the
  // line under the bar says every step in words whenever one did not, and
  // always on a phone.
  const fits = (w: number) => w >= (phone ? 0.3 : 0.1)
  const unsaid = phone || segs.some((s) => !fits(s.x1 - s.x0))
  return (
    <>
      <div className={`acct-tl-steps${pace ? ' is-pace' : ''}`} role="img" aria-label={label}>
        {segs.map((s) => (
          <span
            key={s.at}
            className={`acct-tl-step${s.count === 0 ? ' is-zero' : ''}`}
            style={{ left: pctX(s.x0), width: pctX(s.x1 - s.x0) }}
            title={`${s.count} from ${utcAt(s.at, now)}`}
          >
            {fits(s.x1 - s.x0) ? `${s.count} open` : null}
          </span>
        ))}
      </div>
      {unsaid && (
        <p className="acct-tl-steplist">
          {/* The separator outside the clause, so the line wraps BETWEEN
              clauses and never inside one, and never runs off the card. */}
          {segs.map((s, i) => (
            <span key={s.at}>
              {i > 0 && ' · '}
              <span className="acct-tl-clause">
                <b>{s.count}</b> {i === 0 ? 'now' : `from ${utcAt(s.at, now)}`}
              </span>
            </span>
          ))}
        </p>
      )}
    </>
  )
}

function bandWords(b: Band, now: number): ReactNode {
  const name = windowName(b.key)
  const unknown = b.key !== FIVE_HOUR && b.key !== SEVEN_DAY
  const lead = unknown ? (
    <>
      <b>{b.key}</b> · a window the console does not know by name, held to the {pct(b.floor)} floor ·{' '}
    </>
  ) : (
    <>{name} · </>
  )
  if (b.kind === 'never')
    return (
      <>
        {lead}
        <b>first reading pending</b>
      </>
    )
  if (b.kind === 'absent')
    return (
      <>
        {lead}
        <span className="acct-dash">—</span> not in the last reading
      </>
    )
  // A date and its time are one word: never split across lines.
  const at = <span className="acct-tl-nw">{b.resetsAt === null ? '—' : utcAt(b.resetsAt, now)}</span>
  if (b.kind === 'reset')
    return (
      <>
        {lead}
        <b>reset {at}</b>, no use read since
      </>
    )
  const left = b.left ?? 0
  return (
    <>
      {lead}
      <b>{pct(left)} left</b> ({pct(1 - left)} used)
      {b.kind === 'stale' && <> · counted at half: {pct(b.counted ?? 0)}</>}
      {!unknown && <> · floor {pct(b.floor)}</>} ·{' '}
      {/* A reset already past is not when it clears (capacity-03). */}
      {b.resetsAt !== null && b.resetsAt <= now ? (
        <>reset {at} passed, nothing read since</>
      ) : b.blocks ? (
        <b className="acct-tl-clears">clears {at}</b>
      ) : (
        <>resets {at}</>
      )}
    </>
  )
}

/** One window: its words on a chip, then its band. */
function WindowBand({
  a,
  b,
  now,
  axis,
  forecast,
  judged,
}: {
  a: PoolAccount
  b: Band
  now: number
  axis: AxisKind
  forecast: ForecastResult | null
  /** The broker judges this account's windows for new agents (Can take now, or a clock). */
  judged: boolean
}) {
  const end = 1
  const resetX = b.resetsAt !== null && onAxis(b.resetsAt, now, axis) ? xOf(b.resetsAt, now, axis) : null
  const segs: ReactNode[] = []
  if (b.kind === 'reset') {
    segs.push(<span key="r" className="acct-tl-band is-refilled" style={{ left: 0, width: '100%', height: '100%' }} />)
  } else if (b.kind === 'live' || b.kind === 'stale') {
    const until = resetX ?? end
    // GREEN ONLY WHERE THE BROKER SAID SO: red for a window `blocked_by`
    // names, green for an account it would hand out by its windows, and a
    // neutral fill for one it is not judging at all (a withheld duplicate, a
    // paused or signed-out account), whose share is drawn but not called
    // over or under anything.
    const tone = b.blocks ? 'is-under' : b.kind === 'stale' ? 'is-stale' : judged ? 'is-over' : 'is-unjudged'
    segs.push(
      <span
        key="b"
        className={`acct-tl-band ${tone}`}
        style={{
          left: 0,
          width: pctX(until),
          height: pctX(Math.max(0.04, heightOf(b.counted ?? 0))),
        }}
      />,
    )
    if (resetX !== null && resetX < end) {
      segs.push(
        <span
          key="r"
          className="acct-tl-band is-refilled"
          style={{
            left: pctX(resetX),
            width: pctX(end - resetX),
            height: '100%',
          }}
        />,
      )
    }
  }
  const line =
    forecast !== null && b.key === SEVEN_DAY && (b.kind === 'live' || b.kind === 'stale')
      ? paceLine(forecast, a, now)
      : []
  const drawn = b.kind === 'live' || b.kind === 'stale' || b.kind === 'reset'
  return (
    <div className="acct-tl-win">
      <span className="acct-tl-chip">{bandWords(b, now)}</span>
      {drawn && (
        <div
          className="acct-tl-bandrow"
          role="img"
          aria-label={`${windowName(b.key)}: ${b.kind === 'reset' ? 'reset, full, nothing read since' : `${pct(b.counted ?? 0)} left as counted`}; floor ${pct(b.floor)}`}
        >
          {segs}
          <s className="acct-tl-floor" style={{ bottom: pctX(heightOf(b.floor)) }} aria-hidden="true" />
          {line.length > 1 && (
            <svg className="acct-tl-pace" viewBox="0 0 1000 100" preserveAspectRatio="none" aria-hidden="true" focusable="false">
              <polyline
                points={line
                  .map(([t, s]) => `${(xOf(t, now, axis) * 1000).toFixed(1)},${(100 * (1 - heightOf(s))).toFixed(1)}`)
                  .join(' ')}
                vectorEffect="non-scaling-stroke"
              />
            </svg>
          )}
          {resetX !== null && (b.kind === 'live' || b.kind === 'stale') && (
            <i className={`acct-tl-mark${b.blocks ? ' is-clears' : ''}`} style={{ left: pctX(resetX) }} aria-hidden="true" />
          )}
        </div>
      )}
    </div>
  )
}

type Tone = 'ok' | 'bad' | 'warn' | 'dim'

/** The lane's one-line state, in the Table's words, compact. */
function statusOf(a: PoolAccount, scope: string | null, names: ReadonlyMap<string, string>): { tone: Tone; text: ReactNode } {
  if (a.state === 'REAUTH_REQUIRED') return { tone: 'bad', text: 'Signed out: its sign-in expired' }
  if (a.state === 'PAUSED') return { tone: 'dim', text: <>Paused{a.reason ? `: ${a.reason}` : ''}</> }
  if (a.state === 'DRAINING') return { tone: 'dim', text: 'Draining: takes no new agent' }
  if (a.state !== 'AVAILABLE')
    return {
      tone: 'dim',
      text: `${String(a.state).toLowerCase().replace(/_/g, ' ')}: a state this console does not recognise`,
    }
  if (unreadableFor(a, scope))
    return {
      tone: 'bad',
      text: 'Skipped here: this workspace could not read its secret',
    }
  const dup = blocksOf(a).find(isDuplicateBlock)
  if (dup !== undefined) {
    // Named only when the other registration is in this read (BGD, Q2):
    // another workspace's account is never named.
    const other = dup.duplicate_of ? names.get(dup.duplicate_of) : undefined
    return {
      tone: 'warn',
      text: `Not handed out: one subscription with ${other ?? 'an account in another workspace'}`,
    }
  }
  const can = canTake(a, scope)
  if (can === null) return { tone: 'dim', text: 'The broker does not say' }
  if (can) return { tone: 'ok', text: 'Can take a new agent' }
  for (const b of blocksOf(a)) {
    if (isWindowBlock(b)) {
      const left = Math.max(0, 1 - b.used)
      return {
        tone: 'bad',
        text: b.aged_reading
          ? `${windowName(b.window)}: ${pct(left)} left, counted at half (${pct(left / 2)}), under its ${pct(b.floor)} floor`
          : `${windowName(b.window)}: ${pct(left)} left, under its ${pct(b.floor)} floor`,
      }
    }
    if (isCapBlock(b)) return { tone: 'warn', text: `Full: ${b.cap} of ${b.max_agents} agents` }
    if (isFirstReadingBlock(b))
      return {
        tone: 'warn',
        text: `First reading pending: holds ${b.assigned} of ${b.max_before_reading}`,
      }
  }
  return { tone: 'dim', text: 'Blocked: the broker gave no reason' }
}

const ICON: Readonly<Record<Tone, string>> = {
  ok: '✓',
  bad: '✕',
  warn: '⚠',
  dim: '–',
}

function clearsLine(a: PoolAccount, scope: string | null, now: number): ReactNode {
  const g = groupOf(a, scope)
  if (g === 'unknown') return null
  if (g === 'now') {
    const max = maxAgentsOf(a)
    return `${pluralise(freeSlots(a), 'free slot')}${max === null ? '' : ` of ${max}`}`
  }
  const c = clearsOf(a, scope, now)
  if (g === 'agent')
    return (
      <>
        Frees {c.head}
        {c.sub && <> · {c.sub}</>}
      </>
    )
  return (
    <>
      Clears <b>{c.head}</b>
      {c.sub && <> · {c.sub}</>}
    </>
  )
}

function agentsLine(a: PoolAccount, scope: string | null): string {
  const max = maxAgentsOf(a)
  const held =
    max === null ? `${a.assigned} agent${a.assigned === 1 ? '' : 's'} · cap not served` : `${a.assigned} of ${max} agents`
  const owned = scope === null || a.owner_tenant === scope
  const lent = !owned ? 'lent to this workspace' : a.lend_to.length > 0 ? `lent to ${a.lend_to.join(', ')}` : 'not lent'
  return `${held} · ${lent}`
}

/**
 * A held account's holds (any lane with an agent on it, full or not -- B3
 * draws devops-team's at 5 of 6): one block per agent holding it, of its cap,
 * and a bar from now to the LATEST HOLD DEADLINE the broker serves (B3/B4): the
 * latest instant any of them can still hold a slot. Labelled "latest", never
 * "expected" -- an agent that finishes sooner frees its slot sooner, and no
 * clock says when. A deadline the broker does not serve is a dash with its
 * reason (`holdDeadlineOf`); the opened account's sheet lists who holds it.
 */
function Holds({ a, now, axis, full }: { a: LanedAccount; now: number; axis: AxisKind; full: boolean }) {
  const max = maxAgentsOf(a) ?? DEFAULT_MAX_AGENTS
  const cells = Array.from({ length: Math.max(max, a.assigned) }, (_, i) => i < a.assigned)
  const deadline = holdDeadlineOf(a, now)
  return (
    <div className="acct-tl-holds">
      <span className="acct-tl-holdcells" role="img" aria-label={`${a.assigned} of ${max} agent slots held`}>
        {cells.map((held, i) => (
          <i key={i} className={held ? 'is-held' : ''} />
        ))}
      </span>
      {deadline.at !== null && (
        <span className="acct-tl-holdrow" aria-hidden="true">
          <i className="acct-tl-holdbar" style={{ width: pctX(Math.min(1, xOf(deadline.at, now, axis))) }} />
        </span>
      )}
      <span className="acct-tl-chip">
        {pluralise(a.assigned, 'agent')} {a.assigned === 1 ? 'holds' : 'hold'} it · latest hold deadline{' '}
        {deadline.at !== null ? (
          <b className="acct-tl-nw">{utcAt(deadline.at, now)}</b>
        ) : (
          <>
            <span className="acct-dash">—</span> {deadline.reason}
          </>
        )}
        {/* Only a full lane waits on an agent finishing; one with a free slot takes the next agent now. */}
        {full && ' · no clock: frees when one finishes'}
      </span>
    </div>
  )
}

function Lane({
  a,
  scope,
  now,
  axis,
  forecast,
  freeX,
  names,
  kept,
  open,
  onChoose,
}: {
  a: LanedAccount
  scope: string | null
  now: number
  axis: AxisKind
  forecast: ForecastResult | null
  freeX: number | null
  names: ReadonlyMap<string, string>
  /** The other half's name when this is the kept half of a pair, null when it is not in this read (`keptOf`). */
  kept: string | null | undefined
  open: boolean
  onChoose: (id: string | null) => void
}) {
  const s = statusOf(a, scope, names)
  const org = organisationLine(a)
  const dup = blocksOf(a).some(isDuplicateBlock) || !!a.possible_duplicate
  const possible = a.possible_duplicate
  const possibleOf = possible?.of ? names.get(possible.of) : undefined
  const windows = [SEVEN_DAY, FIVE_HOUR, ...unknownWindows(a)]
  const group = groupOf(a, scope)
  const full = group === 'agent' && blocksOf(a).some(isCapBlock)
  const judged = group === 'now' || group === 'clock' || group === 'agent'
  const cls = [
    'acct-tl-row',
    'acct-tl-lane',
    a.stale && a.observed_at !== null ? 'is-stale' : '',
    dup ? 'is-dup' : '',
    kept !== undefined ? 'is-kept' : '',
    open ? 'is-open' : '',
  ]
    .filter(Boolean)
    .join(' ')
  return (
    <div className={cls} data-account={a.account_id}>
      <div className="acct-tl-words">
        <div className="acct-tl-namerow">
          {/* THE LANE OPENS THE TABLE'S SIDE SHEET (BGD, 2026-10-25): a
              dialog-like panel over the page, not an expansion of this row. */}
          <button
            type="button"
            className="acct-tl-open"
            data-account={a.account_id}
            aria-haspopup="dialog"
            aria-current={open ? 'true' : undefined}
            onClick={() => onChoose(a.account_id)}
          >
            {accountName(a)}
          </button>
        </div>
        <p className="acct-tl-meta">
          {org ?? (
            <>
              organisation <span className="acct-dash">—</span> not recorded at sign-in
            </>
          )}
        </p>
        <p className={`acct-tl-status is-${s.tone}`}>
          <span aria-hidden="true">{ICON[s.tone]}</span> {s.text}
        </p>
        {clearsLine(a, scope, now) !== null && <p className="acct-tl-meta">{clearsLine(a, scope, now)}</p>}
        <p className="acct-tl-meta">{agentsLine(a, scope)}</p>
        {a.stale && a.observed_at !== null && <p className="acct-tl-meta is-stale-note">stale: {ageOf(a, now)}</p>}
        {possible && !blocksOf(a).some(isDuplicateBlock) && (
          <p className="acct-tl-meta is-dup-note">
            May be one subscription with {possibleOf ?? 'an account in another workspace'}: they read identically
          </p>
        )}
        {/* THE KEPT HALF IS MARKED TOO (B2's twin): without it the withheld
            lane named an account whose own lane said nothing about the pair. */}
        {kept !== undefined && (
          <p className="acct-tl-meta is-kept-note">
            {kept === null
              ? 'One subscription with an account in another workspace · handed out here'
              : `One subscription with ${kept} · older, handed out`}
          </p>
        )}
      </div>
      <div className="acct-tl-track">
        <FreeLine x={freeX} />
        {a.observed_at === null ? (
          <span className="acct-tl-chip">
            5-hour and 7-day: <b>first reading pending</b>. No bands until the broker reads its windows; it takes one agent
            until then
          </span>
        ) : (
          windows.map((k) => (
            <WindowBand key={k} a={a} b={bandOf(a, k)} now={now} axis={axis} forecast={forecast} judged={judged} />
          ))
        )}
        {a.assigned > 0 && <Holds a={a} now={now} axis={axis} full={full} />}
      </div>
    </div>
  )
}

function WaitingRow({
  accounts,
  scope,
  now,
  axis,
  phone,
  waiting,
  freeX,
}: {
  accounts: readonly PoolAccount[]
  scope: string | null
  now: number
  axis: AxisKind
  phone: boolean
  waiting: WaitRead
  freeX: number | null
}) {
  let chips: ReactNode
  let count: ReactNode = null
  if (waiting.kind !== 'ok') {
    chips = (
      <span className="acct-tl-chip">
        <span className="acct-dash">—</span>{' '}
        {waiting.kind === 'loading' ? 'reading the waiting list' : 'could not read the waiting list'}
      </span>
    )
  } else if (waiting.waiting.agents === 0) {
    chips = (
      <span className="acct-tl-chip">
        No agent waits on the pool
        {waiting.waiting.atLeast ? ' in the part of the list read' : ''}.
      </span>
    )
  } else {
    const w = waiting.waiting
    count = w.agents
    const q = queueOf(accounts, scope, w.agents, now)
    const startNow = q.now.reduce((s, x) => s + x.n, 0)
    const each = q.now.map((x) => x.n)
    const rows: Array<{ x: number; node: ReactNode; key: string }> = []
    if (startNow > 0) {
      rows.push({
        key: 'now',
        x: 0,
        node: (
          <>
            <b>{startNow} start now</b>
            {'\u00a0· '}
            {each.every((n) => n === each[0]) && q.now.length > 1
              ? `${each[0]} each on ${q.now.map((x) => accountName(x.account)).join(', ')}`
              : q.now.map((x) => `${x.n} on ${accountName(x.account)}`).join(', ')}
          </>
        ),
      })
    }
    const queued = q.later.reduce((s, x) => s + x.n, 0) + q.rest
    if (queued > 0) {
      const first = q.later[0]
      rows.push({
        key: 'later',
        x: first !== undefined ? xOf(first.at, now, axis) : (freeX ?? 0),
        node: (
          <>
            <b>{queued} queue</b>
            {q.later.map((x) => (
              <span key={x.account.account_id}>
                {/* The dot ends a line rather than starting one. */}
                {'\u00a0· '}
                <span className="acct-tl-nw">{accountName(x.account)}</span> takes {x.n}{' '}
                {x.at <= now ? (
                  'at its next check'
                ) : (
                  <>
                    at <span className="acct-tl-nw">{utcAt(x.at, now)}</span>
                  </>
                )}
              </span>
            ))}
            {q.rest > 0 && (
              <>
                {'\u00a0· '}
                {q.rest} wait for an agent to finish
              </>
            )}
          </>
        ),
      })
    }
    chips = rows.map((r) => (
      <span key={r.key} className="acct-tl-chip acct-tl-q" style={phone ? undefined : { marginLeft: pctX(Math.min(r.x, 0.45)) }}>
        <span className="acct-tl-qbar" aria-hidden="true" />
        {r.node}
      </span>
    ))
  }
  const later = waiting.kind === 'ok' ? waiting.waiting.later : 0
  return (
    <div className="acct-tl-row acct-tl-pool">
      <div className="acct-tl-words">
        <p className="acct-tl-rowhead">
          {count !== null ? (
            <>
              <b className="acct-tl-wn">{waiting.kind === 'ok' && waiting.waiting.atLeast ? `at least ${count}` : count}</b>{' '}
              waiting on the pool
            </>
          ) : (
            'Waiting on the pool'
          )}
        </p>
        {later > 0 && (
          <p className="acct-tl-meta">
            and {pluralise(later, 'later step')} wait{later === 1 ? 's' : ''} on them
          </p>
        )}
        <p className="acct-tl-meta">
          <a href={WAITING_HREF}>Open them in Work › Agents</a>
        </p>
        <p className="acct-tl-meta">
          laid against the free slots from the readings and caps, oldest first; another ceiling may start fewer
        </p>
      </div>
      <div className="acct-tl-track">
        <FreeLine x={freeX} />
        <div className="acct-tl-qs">{chips}</div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// The view
// ---------------------------------------------------------------------------

const AXES: ReadonlyArray<{ key: AxisKind; label: string }> = [
  { key: 'split', label: 'Split' },
  { key: '12h', label: '12 h' },
  { key: '7d', label: '7 days' },
]

export function AccountsTimeline({
  accounts,
  scope,
  now,
  waiting,
  phone,
  openId,
  onChoose,
  addButton,
  addForm,
}: {
  accounts: readonly PoolAccount[]
  scope: string | null
  now: number
  waiting: WaitRead
  phone: boolean
  /** The account opened from a lane (`?account=`), or null: its sheet is open, and its lane is marked. */
  openId: string | null
  /** Opens an account's side sheet (Accounts.tsx `Pool`). */
  onChoose: (id: string | null) => void
  addButton: ReactNode
  /** The add form with no click, for the empty pool only (B6); with accounts it opens in the sheet. */
  addForm: ReactNode
}) {
  const [axis, setAxis] = useState<AxisKind>('split')
  const [pace, setPace] = useState<'off' | 'on'>('on')
  const emptyHelp = useId()
  const ranked = rankAccounts(accounts, scope).flatMap((g) => g.accounts)
  const names = new Map(accounts.map((a) => [a.account_id, accountName(a)]))
  const forecast = forecastOf(accounts, scope, now)
  const free = nextFreeAt(accounts, scope, now)
  const freeX = free !== null && onAxis(free, now, axis) ? xOf(free, now, axis) : null
  const ticks = ticksOf(now, axis, phone)
  const steps = poolSteps(accounts, scope, now, now + (axis === '12h' ? 12 * 3_600_000 : 7 * 86_400_000))
  const paced = pace === 'on' ? paceSteps(accounts, scope, now, forecast) : null
  const open = openId !== null && accounts.some((a) => a.account_id === openId) ? openId : null
  const unjudged = accounts.some((a) => a.observed_at !== null && !['now', 'clock', 'agent'].includes(groupOf(a, scope)))

  const near = axis === '7d' ? 'the week' : 'next 12 hours'
  return (
    <section className="section panel acct-tl" aria-label="Subscription accounts, timeline">
      <div className="acct-list-head acct-tl-head">
        <h2>
          Subscription accounts
          <span className="ctl-card-note is-end">{pluralise(accounts.length, 'account')}</span>
        </h2>
        {addButton}
      </div>
      {accounts.length > 0 && (
        <div className="acct-tl-controls">
          <span className="acct-tl-ctl">
            <span className="acct-tl-ctlname">Axis</span>
            <Segmented<AxisKind> small label="Timeline axis" value={axis} onChange={setAxis} options={AXES} />
          </span>
          <span className="acct-tl-ctl">
            <span className="acct-tl-ctlname">Forecast</span>
            <Segmented<'off' | 'on'>
              small
              label="Forecast at this pace"
              value={pace}
              onChange={setPace}
              options={[
                { key: 'off', label: 'Off' },
                { key: 'on', label: 'At this pace' },
              ]}
            />
          </span>
          <span className="acct-tl-utc">Ranked by when each can take a new agent. Times are UTC.</span>
        </div>
      )}
      {accounts.length > 0 && addForm}
      <div className="acct-tl-grid">
        <div className="acct-tl-row acct-tl-axisrow">
          <div className="acct-tl-words">
            <p className="acct-tl-rowhead">{pluralise(accounts.length, 'account')}</p>
            <p className="acct-tl-meta">
              floors: 5-hour {pct(FLOORS[FIVE_HOUR] ?? 0)}, 7-day {pct(FLOORS[SEVEN_DAY] ?? 0)}
            </p>
          </div>
          <div className="acct-tl-track acct-tl-axis">
            <FreeLine x={freeX} />
            <span className="acct-tl-now">
              <b>now {utcAt(now, now)}</b>
              <small>{near}</small>
            </span>
            {axis === 'split' && (
              <span className="acct-tl-split" style={{ left: pctX(0.28) }}>
                <small>the week</small>
              </span>
            )}
            {ticks.map((t) => (
              <span key={t.at} className="acct-tl-tick" style={{ left: pctX(t.x) }}>
                {t.label}
              </span>
            ))}
          </div>
        </div>
        {accounts.length === 0 ? (
          <div className="acct-tl-row">
            <div className="acct-tl-words">
              {/* The empty Table's topic, here too: the cost of the zero --
                  work waits rather than fails -- is the one thing an empty pool
                  cannot show. PUBLISHED, NOT DRAWN (B7.4): the Table and the
                  Timeline are one screen, and the console's ceiling of
                  twenty-seven `?` has no room for this view to draw a second
                  copy of the glyph the empty Table already draws. */}
              <p className="acct-tl-rowhead" aria-describedby={emptyHelp}>
                No lanes yet
                <HelpNote topic="park-on-missing-credential" id={emptyHelp} />
              </p>
              <p className="acct-tl-meta">Each account you add is a lane here</p>
            </div>
            <div className="acct-tl-track">
              <span className="acct-tl-chip">
                A new account takes one agent until its first reading, and draws no bands until then.
              </span>
              {addForm}
            </div>
          </div>
        ) : (
          <>
            <WaitingRow accounts={accounts} scope={scope} now={now} axis={axis} phone={phone} waiting={waiting} freeX={freeX} />
            <div className="acct-tl-row acct-tl-pool">
              <div className="acct-tl-words">
                <p className="acct-tl-rowhead">Accounts that can take an agent</p>
                <p className="acct-tl-meta">by windows only, if nothing more is used</p>
              </div>
              <div className="acct-tl-track">
                <FreeLine x={freeX} />
                <Steps
                  steps={steps}
                  now={now}
                  axis={axis}
                  phone={phone}
                  pace={false}
                  label="Accounts that can take an agent over time"
                />
              </div>
            </div>
            {pace === 'on' && (
              <div className="acct-tl-row acct-tl-pool">
                <div className="acct-tl-words">
                  <p className="acct-tl-rowhead">At this pace</p>
                  <p className="acct-tl-meta">each subscription&rsquo;s 7-day use since its week opened, kept up</p>
                </div>
                <div className="acct-tl-track">
                  <FreeLine x={freeX} />
                  {paced !== null ? (
                    <Steps
                      steps={paced}
                      now={now}
                      axis={axis}
                      phone={phone}
                      pace
                      label="Accounts that can take an agent at this pace"
                    />
                  ) : (
                    <span className="acct-tl-chip">
                      <span className="acct-dash">—</span>{' '}
                      {forecast.unmeasured.length > 0
                        ? `not forecast: ${forecast.unmeasured.map(accountName).join(', ')} ${forecast.unmeasured.length === 1 ? 'has' : 'have'} no 7-day reading to take a pace from`
                        : 'not forecast: no subscription here takes agents'}
                    </span>
                  )}
                </div>
              </div>
            )}
            {ranked.map((a) => (
              <Lane
                key={a.account_id}
                a={a}
                scope={scope}
                now={now}
                axis={axis}
                forecast={pace === 'on' ? forecast : null}
                freeX={freeX}
                names={names}
                kept={keptOf(a, accounts, names)}
                open={open === a.account_id}
                onChoose={onChoose}
              />
            ))}
          </>
        )}
      </div>
      {accounts.length > 0 && (
        <ul className="acct-tl-legend" aria-label="Legend">
          <li>
            <i className="lg-under" aria-hidden="true" /> under its floor
          </li>
          <li>
            <i className="lg-over" aria-hidden="true" /> over its floor
          </li>
          <li>
            <i className="lg-stale" aria-hidden="true" /> stale reading, counted at half
          </li>
          {unjudged && (
            <li>
              <i className="lg-unjudged" aria-hidden="true" /> not handed out: share drawn, not judged
            </li>
          )}
          <li>
            <i className="lg-refilled" aria-hidden="true" /> past its reset: full, no use read since
          </li>
          <li>
            <i className="lg-floor" aria-hidden="true" /> floor (5-hour {pct(FLOORS[FIVE_HOUR] ?? 0)}, 7-day{' '}
            {pct(FLOORS[SEVEN_DAY] ?? 0)})
          </li>
          <li>
            <i className="lg-clears" aria-hidden="true" /> clears: back over the floor
          </li>
          <li>
            <i className="lg-resets" aria-hidden="true" /> resets
          </li>
          <li>
            <i className="lg-free" aria-hidden="true" /> next free slot
            {free === null ? ': none by the clock' : `: ${free <= now ? 'now' : utcAt(free, now)}`}
          </li>
          {pace === 'on' && (
            <li>
              <i className="lg-pace" aria-hidden="true" /> at this pace
            </li>
          )}
          {/* THE HOLDS ARE IN THE LEGEND TOO (VQ-035): a held lane's slot cells
              and its thin bar to the latest hold deadline were the two marks
              no entry named, so a held account's lane read as a different
              kind of window from an idle one with the same share left. */}
          {accounts.some((a) => a.assigned > 0) && (
            <>
              <li>
                <i className="lg-held" aria-hidden="true" /> an agent slot held
              </li>
              <li>
                <i className="lg-holdbar" aria-hidden="true" /> held until the latest hold deadline
              </li>
            </>
          )}
          <li>Band height: share left, square-root scale</li>
        </ul>
      )}
      {accounts.length > 0 && <Forecast accounts={accounts} scope={scope} now={now} />}
    </section>
  )
}
