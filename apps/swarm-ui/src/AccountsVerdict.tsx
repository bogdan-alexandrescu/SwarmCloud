/**
 * THE ANSWER, FIRST (#2222, proposal A's F1-F3 and F5).
 *
 * The question an operator brings to this page is "can the pool take another
 * agent, and if not, when?". So it opens on that: one large number, one
 * sentence naming the account that frees first and when, and how many agents
 * wait on the pool, linked to them. Under it the rule the broker applies (the
 * floor line), any duplicate subscription in words, and, after the table, when
 * the pool goes dark at this pace.
 *
 * Nothing here re-derives the rule: `can_take_new_agent`, `blocked_by` and
 * `next_eligible_at` are the broker's own answers (`accountsPool.ts`).
 */
import { useEffect, useState, type ReactNode } from 'react'

import { loadTasksInState } from './api'
import { accountName } from './accountName'
import {
  AGED_AFTER_MIN,
  DEFAULT_MAX_AGENTS,
  FLOORS,
  blocksOf,
  forecastOf,
  freeSlots,
  inTime,
  isDuplicateBlock,
  maxAgentsOf,
  organisationLine,
  pct,
  poolWaiting,
  utcAt,
  verdictOf,
  windowName,
  type PoolAccount,
  type PoolWaiting,
} from './accountsPool'
import { FIVE_HOUR, SEVEN_DAY, pluralise } from './types'
import './styles/accounts-ranked.css'

/** Work › Agents › Waiting, where the parked agents are listed. */
export const WAITING_HREF = '#work/running/waiting'

/** "a, b and c". */
export function andList(xs: readonly string[]): string {
  if (xs.length <= 1) return xs.join('')
  return `${xs.slice(0, -1).join(', ')} and ${xs[xs.length - 1]}`
}

// ---------------------------------------------------------------------------
// The waiting list
// ---------------------------------------------------------------------------

export type WaitRead =
  | { kind: 'loading' }
  | { kind: 'ok'; waiting: PoolWaiting }
  | { kind: 'error'; message: string }

/**
 * The PARKED page, re-read whenever the board is (`key` is its read instant).
 * A failed read is a dash with its reason, NEVER 0: "nobody waits" and "the
 * waiting list could not be read" are opposite facts.
 */
export function usePoolWaiting(key: number): WaitRead {
  const [read, setRead] = useState<WaitRead>({ kind: 'loading' })
  useEffect(() => {
    let live = true
    const failed = (message: string) => {
      if (live) setRead({ kind: 'error', message })
    }
    // A read that throws, or answers nothing, is the dash too: this count sits
    // beside the verdict and must never take the page down with it.
    new Promise<Awaited<ReturnType<typeof loadTasksInState>>>((resolve) =>
      resolve(loadTasksInState('PARKED', { view: 'summary', limit: 200 })),
    )
      .then((res) => {
        if (!live) return
        if (!res) failed('the waiting list read returned nothing')
        else if (res.status === 'empty') setRead({ kind: 'ok', waiting: { agents: 0, later: 0, atLeast: false } })
        else if (res.status === 'ok' || res.status === 'stale')
          setRead({ kind: 'ok', waiting: poolWaiting(res.data.tasks, !!res.data.next_page_token) })
        else if (res.status === 'error') failed(res.error.message)
      })
      .catch((e: unknown) => failed(e instanceof Error ? e.message : String(e)))
    return () => {
      live = false
    }
  }, [key])
  return read
}

// ---------------------------------------------------------------------------
// The verdict
// ---------------------------------------------------------------------------

function Name({ a }: { a: PoolAccount }) {
  return <b>{accountName(a)}</b>
}

function WaitBox({ read, free }: { read: WaitRead; free: number | null }) {
  if (read.kind !== 'ok') {
    return (
      <div className="acct-verdict-wait">
        <span className="acct-verdict-wn" aria-hidden="true">
          &mdash;
        </span>
        <a href={WAITING_HREF}>agents waiting on the pool &rarr;</a>
        <small>{read.kind === 'loading' ? 'reading the waiting list' : 'could not read the waiting list'}</small>
      </div>
    )
  }
  const w = read.waiting
  const startable = free === null ? 0 : Math.min(free, w.agents)
  return (
    <div className="acct-verdict-wait">
      <span className="acct-verdict-wn">
        {w.atLeast && <small>at least </small>}
        {w.agents}
      </span>
      <a href={WAITING_HREF}>
        {w.agents === 1 ? 'agent waits' : 'agents wait'} on the pool &rarr;
      </a>
      {w.later > 0 && (
        <small>
          and {pluralise(w.later, 'later step')} wait{w.later === 1 ? 's' : ''} on them
        </small>
      )}
      {startable > 0 && (
        <small>the pool can start {startable} of them now, oldest first; another ceiling may start fewer</small>
      )}
    </div>
  )
}

export function Verdict({
  accounts,
  scope,
  shortfall,
  now,
  waiting,
}: {
  accounts: readonly PoolAccount[]
  scope: string | null
  shortfall: number
  now: number
  waiting: WaitRead
}) {
  const v = verdictOf(accounts, scope, shortfall)
  const of = `of ${v.short ? 'at least ' : ''}${v.m}`
  let head: ReactNode
  let sub: ReactNode = null
  if (v.m === 0) {
    head = 'No account is registered, so no agent that needs the pool can start.'
    sub =
      'Agents whose runner profile uses the pool wait until an account is added here, or one is lent to this workspace. Agents with their own key are not affected.'
  } else if (v.n === null) {
    head = 'This broker does not say which accounts can take a new agent.'
    sub = 'It predates the per-account answer; the table shows the windows it served.'
  } else if (v.n === 0) {
    const next = v.next
    head = next ? (
      <>
        No account can take a new agent. Next: {accountName(next.account)} at {utcAt(next.at, now)}.
      </>
    ) : v.full ? (
      <>No account can take a new agent. Next: {accountName(v.full)}, when one of its agents finishes.</>
    ) : (
      'No account can take a new agent, and no clock says when one will.'
    )
    const top = v.underBy[0]
    sub = (
      <>
        {next && (
          <>
            {inTime(next.at, now)}
            {next.window !== null && <>, when its {windowName(next.window)} window resets</>}.{' '}
          </>
        )}
        {v.then && (
          <>
            Then <Name a={v.then.account} /> at <b>{utcAt(v.then.at, now)}</b>.{' '}
          </>
        )}
        {next && v.full && (
          <>
            <Name a={v.full} /> frees sooner if one of its agents finishes.{' '}
          </>
        )}
        {top !== undefined && (
          <>
            {top.count} of the {v.m} {top.count === 1 ? 'is' : 'are'} under the floor on {top.count === 1 ? 'its' : 'their'}{' '}
            {windowName(top.window)} window.
          </>
        )}
      </>
    )
  } else {
    const slots = pluralise(v.free, 'free slot')
    const names = v.open.map((a) => accountName(a))
    const each = v.open.map(freeSlots)
    head =
      v.n === 1 ? (
        <>
          1 account can take a new agent: {slots} on {names[0]}.
        </>
      ) : (
        <>
          {v.n} accounts can take a new agent: {slots}.
        </>
      )
    const rest = v.next ? (
      <>
        {' '}
        Of the rest, <Name a={v.next.account} /> frees first, at <b>{utcAt(v.next.at, now)}</b>.
      </>
    ) : v.full ? (
      <>
        {' '}
        Of the rest, <Name a={v.full} /> frees when one of its agents finishes.
      </>
    ) : null
    sub = (
      <>
        {v.n > 1 && (
          <>
            {andList(names)}
            {each.every((x) => x === each[0]) ? `, ${each[0]} each.` : ` (${andList(each.map(String))}).`}
          </>
        )}
        {rest}
      </>
    )
  }
  const tone = v.n !== null && v.n > 0 ? 'is-some' : 'is-none'
  return (
    <section className={`acct-verdict ${tone}`} aria-label="Can the pool take a new agent">
      <div className="acct-verdict-big">
        {v.n === null ? <span title="This broker does not serve can_take_new_agent">&mdash;</span> : v.n}{' '}
        <small>{v.m === 0 ? 'accounts' : of}</small>
      </div>
      <div className="acct-verdict-say">
        <p className="acct-verdict-head">{head}</p>
        {sub !== null && <p className="acct-verdict-sub">{sub}</p>}
      </div>
      <WaitBox read={waiting} free={v.n === null ? null : v.free} />
    </section>
  )
}

/**
 * THE RULE, IN ONE LINE: the floors, the halving of an aged reading, the cap,
 * and one agent until measured -- so the 15% / 5% rule can be read on the page
 * rather than known. The cap is the served one when every account agrees.
 */
export function RuleLine({ accounts }: { accounts: readonly PoolAccount[] }) {
  const caps = [...new Set(accounts.map(maxAgentsOf).filter((x): x is number => x !== null))].sort((x, y) => x - y)
  const cap =
    caps.length > 1 ? `${caps[0]} to ${caps[caps.length - 1]} agents per account, as each owner set` : `${caps[0] ?? DEFAULT_MAX_AGENTS} agents per account`
  return (
    <p className="acct-rule">
      Floors: <b>5-hour {pct(FLOORS[FIVE_HOUR] ?? 0)} left</b> &middot; <b>7-day {pct(FLOORS[SEVEN_DAY] ?? 0)} left</b> &middot; a
      reading older than {AGED_AFTER_MIN} min counts at half &middot; at most <b>{cap}</b> &middot; a new account takes one
      agent until its first reading
    </p>
  )
}

// ---------------------------------------------------------------------------
// One subscription registered twice
// ---------------------------------------------------------------------------

interface Pair {
  /** The registration that adds nothing: withheld, or the later of a possible pair. */
  extra: PoolAccount
  /** The other side, when it is in this read; null when it is another workspace's. */
  other: PoolAccount | null
  proven: boolean
}

/** The pairs to say, each once. Another workspace's account is never named (BGD, Q2). */
export function duplicatePairs(accounts: readonly PoolAccount[]): Pair[] {
  const byId = new Map(accounts.map((a) => [a.account_id, a]))
  const out: Pair[] = []
  const seen = new Set<string>()
  const add = (extra: PoolAccount, otherId: string | null, proven: boolean) => {
    const other = otherId === null ? null : (byId.get(otherId) ?? null)
    const key = [extra.account_id, other?.account_id ?? '?'].sort().join('|')
    if (seen.has(key)) return
    seen.add(key)
    out.push({ extra, other, proven })
  }
  for (const a of accounts) {
    const withheld = blocksOf(a).find(isDuplicateBlock)
    if (withheld !== undefined) add(a, withheld.duplicate_of ?? a.duplicate_of ?? null, true)
  }
  for (const a of accounts) {
    if (a.possible_duplicate) add(a, a.possible_duplicate.of, false)
  }
  return out
}

/**
 * Open the account, then bring its "Replace the credential" section into view
 * once the detail has drawn it (`data-replace` in Accounts.tsx).
 */
function openReplace(id: string, onOpen: (id: string) => void) {
  onOpen(id)
  if (typeof window === 'undefined') return
  window.setTimeout(() => {
    const at = [...document.querySelectorAll<HTMLElement>('[data-replace]')].find((el) => el.dataset.replace === id)
    at?.scrollIntoView?.({ block: 'start' })
    at?.querySelector<HTMLElement>('button')?.focus({ preventScroll: true })
  }, 60)
}

export function DuplicateBanner({
  accounts,
  scope,
  onOpen,
}: {
  accounts: readonly PoolAccount[]
  scope: string | null
  onOpen: (id: string) => void
}) {
  const pairs = duplicatePairs(accounts)
  if (pairs.length === 0) return null
  return (
    <>
      {pairs.map(({ extra, other, proven }) => {
        const x = accountName(extra)
        const y = other === null ? null : accountName(other)
        const org = organisationLine(extra)
        const owned = scope === null || extra.owner_tenant === scope
        return (
          <div key={extra.account_id} className="acct-dup" role="status">
            <b className="acct-dup-head">
              {proven
                ? y !== null
                  ? `${x} and ${y} are one subscription`
                  : `${x} is a subscription another workspace already registered`
                : y !== null
                  ? `${x} and ${y} may be one subscription`
                  : `${x} may be a subscription another workspace registered`}
            </b>
            <p>
              {proven ? (
                <>
                  {org !== null && y !== null && (
                    <>
                      Both were signed in to the organisation <b>{org}</b>.{' '}
                    </>
                  )}
                  The pool hands out only {y ?? 'the other registration'}, so {x} adds no capacity.{' '}
                  <span className="acct-dup-why">
                    One claude.ai login can hold two organisations; the sign-in took the one that was selected.
                  </span>
                </>
              ) : (
                <>
                  They read identically on two polls. The pool still hands out both; if they are one, {x} adds no capacity.
                </>
              )}
            </p>
            <p>
              {owned ? (
                <>
                  <b>To fix it:</b> on {x}, replace the credential, signed in to the organisation you meant from that
                  organisation&rsquo;s browser profile. Or remove {x}.
                </>
              ) : (
                <>
                  {x} is lent to you; {extra.owner_tenant} can fix it.
                </>
              )}
            </p>
            <div className="acct-dup-acts">
              <button type="button" className="c-btn is-sm" onClick={() => onOpen(extra.account_id)}>
                Open {x}
              </button>
              {/* The fix the banner names, one click from it. It is the chosen
                  account's own control, so it opens that account and goes to it. */}
              {owned && (
                <button type="button" className="c-btn is-sm" onClick={() => openReplace(extra.account_id, onOpen)}>
                  Replace the credential
                </button>
              )}
            </div>
          </div>
        )
      })}
    </>
  )
}

// ---------------------------------------------------------------------------
// The forecast
// ---------------------------------------------------------------------------

function Tile({ k, v, small }: { k: string; v: ReactNode; small: ReactNode }) {
  return (
    <div className="acct-fc-tile">
      <div className="acct-fc-k">{k}</div>
      <div className="acct-fc-v">{v}</div>
      <small>{small}</small>
    </div>
  )
}

/**
 * WHEN THE POOL GOES DARK, AND WHEN IT COMES BACK: "From today's readings"
 * (BGD). Each subscription's 7-day pace is its used share over the hours since
 * its week opened; a withheld duplicate is counted once. No new API field.
 */
export function Forecast({ accounts, scope, now }: { accounts: readonly PoolAccount[]; scope: string | null; now: number }) {
  const f = forecastOf(accounts, scope, now)
  const k = f.paces.length
  let tiles: ReactNode
  if (k === 0 && f.unmeasured.length === 0) {
    tiles = <Tile k="Goes dark" v="—" small="no subscription here takes agents" />
  } else if (f.unmeasured.length > 0) {
    const names = f.unmeasured.map((a) => accountName(a))
    tiles = (
      <Tile
        k="Goes dark"
        v="—"
        small={`${andList(names)} ${names.length === 1 ? 'has' : 'have'} no 7-day reading to take a pace from`}
      />
    )
  } else if (f.darkAt === null) {
    tiles = (
      <>
        <Tile k="Goes dark" v="Not within 8 days" small="at this pace" />
        <Tile k="Back" v="—" small="it does not go dark" />
      </>
    )
  } else {
    tiles = (
      <>
        <Tile
          k="Goes dark"
          v={f.darkNow ? 'now' : `≈ ${utcAt(f.darkAt, now)}`}
          small={f.darkNow ? 'every subscription is under its 7-day floor' : `${inTime(f.darkAt, now)}, at this pace`}
        />
        {!f.darkNow && f.lastOpen !== null && (
          <Tile k="Why dark" v={accountName(f.lastOpen)} small="the last one open reaches its 7-day floor" />
        )}
        <Tile
          k="Back"
          v={f.backAt === null ? '—' : utcAt(f.backAt, now)}
          small={f.back === null ? 'not within 8 days' : `${accountName(f.back)}’s week resets`}
        />
      </>
    )
  }
  return (
    <section className="acct-forecast" aria-label="Forecast">
      <div className="acct-fc-headrow">
        <h3>Forecast: when the pool goes dark, and when it comes back</h3>
        <span className="acct-fc-sub">
          7-day windows, at this pace
          {k > 0 && (
            <>
              : together {pluralise(k, 'subscription')} spend {pct(f.poolPerHour * 24)} of one week&rsquo;s window a day
            </>
          )}
        </span>
      </div>
      <div className="acct-fc-tiles">{tiles}</div>
      <p className="acct-fc-note">
        <b>Laptop sessions spend the same windows:</b> these are claudeswitch&rsquo;s subscriptions too, so the pool can go
        dark sooner than this says.
      </p>
    </section>
  )
}
