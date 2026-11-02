/**
 * THE ACCOUNTS, RANKED BY WHEN EACH CAN TAKE A NEW AGENT (#2222, proposal A).
 *
 * One table on a desktop, one card per account on a phone, in the same order
 * and the same groups: Can take now -> Clears at a known time (soonest first)
 * -> Frees when an agent finishes (BGD's "Known clock first"), and, only when
 * one exists, the accounts a person holds. Each row says in words what blocks
 * it, with its floor, and when that clears, and draws both windows against
 * their floors as a tick on the bar.
 *
 * A WINDOW THAT HAS RESET HAS NO FIGURE. It reads "reset HH:MMZ, nothing read
 * since" over a dashed empty bar: on 2026-10-24 the page showed `~40%` for a
 * window that had refilled hours before, and an operator read it as plenty.
 *
 * Choosing a row is the address the page already had (`?account=`), and the
 * name is the same `acct-open` button the list was, so the account's detail
 * and every control on it are where they were.
 */
import { Fragment, useState, type ReactNode } from 'react'

import { accountName } from './accountName'
import { AcctState } from './AccountState'
import { UsageTrack, type TrackTone } from './components'
import {
  AGED_AFTER_MIN,
  agoPastDay,
  DEFAULT_MAX_AGENTS,
  blocksOf,
  canTake,
  clockOf,
  clockPassed,
  floorOf,
  freeSlots,
  groupOf,
  inTime,
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
  type PoolAccount,
} from './accountsPool'
import { FIVE_HOUR, SEVEN_DAY, ageSpan, neverAssigned, readingOf, unreadableFor } from './types'
import './styles/accounts-ranked.css'

// ---------------------------------------------------------------------------
// Cells
// ---------------------------------------------------------------------------

/**
 * WHICH KIND OF ACCOUNT A ROW IS, IN WORDS (#2688). The pool holds Claude
 * subscriptions (`anthropic`) and, since the owner's choice of 2026-11-01,
 * ChatGPT plans (`openai`, which codex runs on); routes/accounts.py's
 * ACCOUNT_PROVIDERS. A provider this console does not know by name is null
 * here and is drawn as its own id: a guess at what it is would be a claim
 * nobody made.
 */
const PROVIDER_NAMES: Readonly<Record<string, string>> = {
  anthropic: 'Claude subscription',
  openai: 'ChatGPT plan',
}

export function providerName(provider: string): string | null {
  return Object.prototype.hasOwnProperty.call(PROVIDER_NAMES, provider) ? PROVIDER_NAMES[provider]! : null
}

/** The provider's words, or its id drawn as an id when it has none. */
export function ProviderName({ provider }: { provider: string }) {
  const name = providerName(provider)
  return name === null ? <span className="mono">{provider}</span> : <>{name}</>
}

/** The can-take chip: `Yes · 6 free`, `Full · 6 of 6`, `No · paused` (cap2-01), `No`. */
export function CanTakeChip({ a, scope }: { a: PoolAccount; scope: string | null }) {
  const can = canTake(a, scope)
  if (can === null) {
    return (
      <span className="acct-chip is-unknown" title="This broker does not say whether the account can take a new agent">
        &mdash;
      </span>
    )
  }
  if (can) return <span className="acct-chip is-yes">Yes &middot; {freeSlots(a)} free</span>
  const cap = blocksOf(a).find(isCapBlock)
  if (cap !== undefined && groupOf(a, scope) === 'agent') {
    return (
      <span className="acct-chip is-full">
        Full &middot; {cap.cap} of {cap.max_agents}
      </span>
    )
  }
  if (a.state === 'PAUSED') return <span className="acct-chip is-no">No &middot; paused</span>
  return <span className="acct-chip is-no">No</span>
}

function leftShare(a: PoolAccount, key: string): number | null {
  const w = a.windows?.[key]
  if (!w || w.reset || !Number.isFinite(w.utilization)) return null
  return Math.max(0, 1 - w.utilization)
}

/** What blocks it, in words, with its floor. */
export function whyOf(
  a: PoolAccount,
  scope: string | null,
  now: number,
  names: ReadonlyMap<string, string> = new Map(),
): ReactNode {
  const can = canTake(a, scope)
  if (a.state === 'REAUTH_REQUIRED') return <><b>Signed out</b>: its sign-in expired, and it takes nothing until someone signs in again</>
  if (a.state === 'PAUSED') return <><b>Paused</b>{a.reason ? `: ${a.reason}` : ''}. Its owner resumes it</>
  if (a.state === 'DRAINING') return <><b>Draining</b>{a.reason ? ` (${a.reason})` : ''}: it serves the agents it holds and takes no new one</>
  if (a.state !== 'AVAILABLE') return <><b>{a.state.toLowerCase().replace(/_/g, ' ')}</b>: a state this console does not recognise</>
  if (unreadableFor(a, scope)) return <><b>Skipped here</b>: this workspace could not read its secret</>
  const dup = blocksOf(a).find(isDuplicateBlock)
  if (dup !== undefined) {
    return (
      <>
        <b>One subscription twice</b>:{' '}
        {dup.duplicate_of
          ? `registered on the same organisation as ${names.get(dup.duplicate_of) ?? dup.duplicate_of}`
          : (dup.reason ?? 'registered on an organisation another account holds')}
        . The pool hands out only the other
      </>
    )
  }
  if (can === null) return <span className="acct-dash">the broker does not say</span>
  if (can) {
    const parts = [FIVE_HOUR, SEVEN_DAY].map((k) => {
      const left = leftShare(a, k)
      if (a.windows?.[k]?.reset) return `${windowName(k)} reset`
      if (left === null) return null
      return `${windowName(k)} ${pct(left)} left, above its ${pct(floorOf(a, k))} floor`
    })
    const said = parts.filter((p): p is string => p !== null)
    return (
      <>
        <b>Nothing.</b>
        {said.length > 0 ? ` ${said.join('; ')}` : a.observed_at === null ? ' Not measured yet; it takes one agent until it is' : ''}
      </>
    )
  }
  const out: ReactNode[] = []
  for (const b of blocksOf(a)) {
    if (isWindowBlock(b)) {
      const left = Math.max(0, 1 - b.used)
      out.push(
        b.aged_reading ? (
          <span key={b.window}>
            <b>An aged reading</b>: {windowName(b.window)} {pct(left)} left, read{' '}
            {a.observed_at ? `${ageSpan(now - Date.parse(a.observed_at))} ago` : 'long ago'}, counts at half ({pct(left / 2)}),
            under its {pct(b.floor)} floor
          </span>
        ) : (
          <span key={b.window}>
            <b>{windowName(b.window)} window</b>: {pct(left)} left, under its {pct(b.floor)} floor
          </span>
        ),
      )
    } else if (isCapBlock(b)) {
      const others = blocksOf(a).some(isWindowBlock)
      out.push(
        <span key="cap">
          <b>Full</b>: {b.cap} of {b.max_agents} agents.{others ? '' : ' Both windows are above their floors'}
        </span>,
      )
    } else if (isFirstReadingBlock(b)) {
      out.push(
        <span key="first">
          <b>Waiting for its first reading</b>: a new account takes {b.max_before_reading} agent until it is measured, and it
          holds {b.assigned}
        </span>,
      )
    }
  }
  if (out.length === 0) return <span className="acct-dash">the broker gave no reason</span>
  return out.map((x, i) => (
    <span key={i} className="acct-why-part">
      {x}
    </span>
  ))
}

/** When it clears: a clock, or what ends it when no clock says. */
export function clearsOf(a: PoolAccount, scope: string | null, now: number): { head: ReactNode; sub: ReactNode } {
  const g = groupOf(a, scope)
  if (g === 'now') return { head: 'now', sub: `${freeSlots(a)} slot${freeSlots(a) === 1 ? '' : 's'}` }
  if (g === 'clock') {
    const at = clockOf(a) as number
    // A reset already past, nothing read since (capacity-03): never "Clears <a past time> · now".
    if (clockPassed(a, now)) return { head: 'at the next reading', sub: `reset ${utcAt(at, now)} passed, nothing read since` }
    const aged = blocksOf(a).some((b) => isWindowBlock(b) && b.aged_reading)
    return aged
      ? { head: 'at the next reading', sub: `or ${utcAt(at, now)} at the latest` }
      : { head: utcAt(at, now), sub: inTime(at, now) }
  }
  if (g === 'agent') {
    if (blocksOf(a).some(isFirstReadingBlock)) return { head: 'at its first reading', sub: 'or when its agent finishes' }
    return { head: `when one of its ${a.assigned} finishes`, sub: 'no clock says when' }
  }
  if (isWithheldDup(a)) return { head: 'never, while both are registered', sub: 'fix the duplicate' }
  if (g === 'unknown') return { head: '—', sub: null }
  return { head: 'when a person acts', sub: 'no clock says when' }
}

function isWithheldDup(a: PoolAccount): boolean {
  return blocksOf(a).some(isDuplicateBlock)
}

/**
 * One window against its floor. The bar is the share USED; the tick sits at
 * 100% minus the floor, so a fill past the tick is under the floor.
 */
export function WindowCell({ a, k, now }: { a: PoolAccount; k: string; now: number }) {
  const r = readingOf(a, k)
  const floor = floorOf(a, k)
  const tick = `${(1 - floor) * 100}%`
  const label = windowName(k)
  if (r.kind === 'never' || r.kind === 'absent') {
    return (
      <div className="acct-win">
        <div className="acct-win-l">
          <b>{label}</b> <span className="acct-dash">&mdash;</span>
        </div>
        <small>{r.kind === 'never' ? 'no reading yet' : 'not in the last reading'}</small>
      </div>
    )
  }
  if (r.kind === 'reset') {
    return (
      <div className="acct-win">
        <div className="acct-win-l">
          <b>{label}</b> reset {utcAt(r.resetsAt, now)}
        </div>
        <FloorTrack
          pct={null}
          tick={tick}
          label={`${label}: reset at ${utcAt(r.resetsAt, now)}, nothing read since; floor ${pct(floor)} left`}
        />
        <small>nothing read since</small>
      </div>
    )
  }
  const used = r.pct / 100
  const left = 1 - used
  const counted = r.kind === 'stale' ? left / 2 : left
  const under = counted < floor
  // THE VERDICT IS THE BROKER'S: red only for a window `blocked_by` names, so
  // no threshold is chosen in this file (CP-25: no amber band). An old reading
  // is the projected fill, as everywhere else.
  const blocks = blocksOf(a).some((b) => isWindowBlock(b) && b.window === k)
  return (
    <div className="acct-win">
      <div className="acct-win-l">
        <b>{label}</b> {pct(left)} left
      </div>
      <FloorTrack
        pct={r.pct}
        tone={blocks ? 'is-bad' : r.kind === 'stale' ? 'ov-projected' : undefined}
        tick={tick}
        label={`${label}: ${pct(used)} used, ${pct(left)} left; floor ${pct(floor)} left${under ? ', under it' : ''}`}
      />
      <small>{pct(used)} used</small>
      {/* A reset already past is said as one (capacity-03), not as a time to come. */}
      <small>
        {Date.parse(r.resetsAt) <= now ? `reset ${utcAt(r.resetsAt, now)} passed` : `resets ${utcAt(r.resetsAt, now)}`}
      </small>
    </div>
  )
}

/**
 * The shared track (`UsageTrack`, CP-25: one proportion primitive) with the
 * floor drawn as a tick at 100% minus the floor, so a fill past the tick is
 * under it. A reset window is the unmeasured track: nothing read since.
 */
function FloorTrack({ pct: p, tone, tick, label }: { pct: number | null; tone?: TrackTone; tick: string; label: string }) {
  return (
    <span className="acct-floor" role="img" aria-label={label}>
      <UsageTrack pct={p} tone={tone} />
      <s style={{ left: tick }} aria-hidden="true" />
    </span>
  )
}

/** `read 4m ago`, `read 52m ago · counts at half`, or `no reading yet`. */
export function ageOf(a: PoolAccount, now: number): string {
  if (a.observed_at === null) return 'no reading yet'
  const at = Date.parse(a.observed_at)
  const age = Number.isFinite(at) ? `read ${ageSpan(now - at)} ago` : 'read at an unknown time'
  return a.stale ? `${age} · older than ${AGED_AFTER_MIN} min, counts at half` : age
}

function Tags({ a, names }: { a: PoolAccount; names: ReadonlyMap<string, string> }) {
  const extra = unknownWindows(a)
  const dup = a.duplicate_of ? (names.get(a.duplicate_of) ?? a.duplicate_of) : null
  const possible = a.possible_duplicate
  const possibleOf = possible?.of ? (names.get(possible.of) ?? possible.of) : null
  // THE AGE IS A TAG, NOT A DIMMED ROW (visual QA pass 2, cap2-07): the
  // window cells of an aged row were drawn at 0.6 opacity, 2.72:1 in light.
  // The name line already says how old the reading is; the tag marks the row.
  const aged = a.stale && a.observed_at !== null
  if (extra.length === 0 && !dup && !a.duplicate_reason && !possible && !aged) return null
  return (
    <span className="acct-tags">
      {aged && <span className="acct-tag is-aged">aged reading</span>}
      {dup ? (
        <span className="acct-tag is-dup">
          same subscription as <span className="acct-nm">{dup}</span>
        </span>
      ) : a.duplicate_reason ? (
        <span className="acct-tag is-dup">registered in another workspace too</span>
      ) : null}
      {possible && !dup && (
        <span className="acct-tag is-dup">
          may be the same subscription as{' '}
          {possibleOf === null ? 'another workspace’s account' : <span className="acct-nm">{possibleOf}</span>}
        </span>
      )}
      {extra.length > 0 && (
        <span
          className="acct-tag is-unk"
          title={`${extra.join(', ')}: ${extra.length === 1 ? 'a window' : 'windows'} this console does not know by name, held to the default floor`}
        >
          {extra.length === 1 ? '1 window' : `${extra.length} windows`} not known by name:{' '}
          {/* The provider's own key, drawn as an id: it is data, not a word we chose. */}
          {extra.map((w, i) => (
            <Fragment key={w}>
              {i > 0 && ', '}
              <span className="mono">{w}</span>
            </Fragment>
          ))}
        </span>
      )}
    </span>
  )
}

function OrgLine({ a }: { a: PoolAccount }) {
  const org = organisationLine(a)
  return org === null ? (
    <small className="acct-dash" title="The broker records the organisation at add and refresh; none is recorded for this account">
      organisation not recorded
    </small>
  ) : (
    <small>{org}</small>
  )
}

/** `given out 26m ago`, or `never given out`: a pool no worker can reach has every account at never (#127). */
function givenOut(a: PoolAccount, now: number): string {
  return neverAssigned(a) || a.last_assigned_at === null ? 'never given out' : `given out ${agoPastDay(a.last_assigned_at, now)}`
}

/**
 * The marks a row keeps from the list it replaced: the account's state when it
 * is not AVAILABLE (brand §3, #503: a mark and its word, never a task's), and
 * `skipped here` when this workspace reported its secret unreadable.
 */
function RowMarks({ a, scope }: { a: PoolAccount; scope: string | null }) {
  const unusable = unreadableFor(a, scope)
  if (a.state === 'AVAILABLE' && !unusable) return null
  return (
    <span className="acct-rank-marks">
      {a.state !== 'AVAILABLE' && <AcctState state={a.state} />}
      {unusable && (
        <span
          className="ctl-mark is-unread acct-unusable"
          aria-label={`The pool is skipping this account for ${scope}: this workspace reported it could not read the account's secret, and accounts.choose skips it for this workspace because of that. The report is forgotten thirty minutes after it was made.`}
        >
          skipped here
        </span>
      )}
    </span>
  )
}

/**
 * Under the name: the id that gets pasted into a command (#1913: the Name
 * never hides which account this is), which kind of account it is, the
 * organisation, the reading's age and when it was last given out (#127).
 */
function RowFacts({ a, scope, now }: { a: PoolAccount; scope: string | null; now: number }) {
  return (
    <>
      <small className="raw">{a.account_id}</small>
      <small className="acct-prov" data-provider={a.provider}>
        <ProviderName provider={a.provider} />
      </small>
      <OrgLine a={a} />
      <small className="acct-age">{ageOf(a, now)}</small>
      <small className="acct-given">{givenOut(a, now)}</small>
      <RowMarks a={a} scope={scope} />
    </>
  )
}

function agentsOf(a: PoolAccount): string {
  const max = maxAgentsOf(a)
  return `${a.assigned} of ${max ?? DEFAULT_MAX_AGENTS}`
}

function lentOf(a: PoolAccount, scope: string | null): string {
  if (scope !== null && a.owner_tenant !== scope) return `lent to you by ${a.owner_tenant}`
  return a.lend_to.length === 0 ? 'not lent' : a.lend_to.join(', ')
}

/** Each tenant whole: `u-bogdan` never breaks at its hyphen (V070). */
function LentCell({ a, scope }: { a: PoolAccount; scope: string | null }) {
  if ((scope !== null && a.owner_tenant !== scope) || a.lend_to.length === 0) return <>{lentOf(a, scope)}</>
  return (
    <>
      {a.lend_to.map((t, i) => (
        <span key={t}>
          {i > 0 && ', '}
          <span className="acct-lent">{t}</span>
        </span>
      ))}
    </>
  )
}

/** The card's form: `lent to smoke, u-bogdan`, `not lent`, `lent to you by eng`. */
function lentPhrase(a: PoolAccount, scope: string | null): string {
  const l = lentOf(a, scope)
  return l === 'not lent' || l.startsWith('lent to you') ? l : `lent to ${l}`
}

function OpenName({ a, chosen, onChoose }: { a: PoolAccount; chosen: boolean; onChoose: () => void }) {
  return (
    <button
      type="button"
      className="acct-open acct-rank-name"
      data-account={a.account_id}
      aria-current={chosen ? 'true' : undefined}
      // BORDERLESS INLINE: the spacing probe orders rules by source alone, so
      // the zero-specificity `:where(.app button)` border later in styles.css
      // out-ranks any rule in this screen's sheet, and it graded a 1px boundary
      // on the chosen row (2.89:1) that no browser draws (styles.css restates
      // `border: 0` after it for the old list's `.acct-li > .acct-open`).
      style={{ border: 0 }}
      onClick={onChoose}
    >
      {accountName(a)}
    </button>
  )
}

// ---------------------------------------------------------------------------
// The table, and the phone's cards
// ---------------------------------------------------------------------------

export function AccountsTable({
  accounts,
  scope,
  now,
  chosen,
  phone,
  onChoose,
}: {
  accounts: readonly PoolAccount[]
  scope: string | null
  now: number
  chosen: string | null
  phone: boolean
  onChoose: (id: string) => void
}) {
  const groups = rankAccounts(accounts, scope)
  const names = new Map(accounts.map((a) => [a.account_id, accountName(a)]))
  if (phone) return <AccountCards groups={groups} names={names} scope={scope} now={now} chosen={chosen} onChoose={onChoose} />
  let n = 0
  return (
    <div className="acct-rank-wrap">
      <table className="acct-rank">
        <colgroup>
          <col className="acct-rank-c-n" />
          <col className="acct-rank-c-name" />
          <col className="acct-rank-c-can" />
          <col className="acct-rank-c-why" />
          <col className="acct-rank-c-clr" />
          <col className="acct-rank-c-win" />
          <col className="acct-rank-c-win" />
          <col className="acct-rank-c-ag" />
          <col />
        </colgroup>
        <thead>
          <tr>
            <th scope="col" className="acct-rank-n">
              #
            </th>
            <th scope="col">Account · signed in to</th>
            <th scope="col">Can take</th>
            <th scope="col">What blocks it</th>
            <th scope="col">Clears</th>
            <th scope="col">5-hour</th>
            <th scope="col">7-day</th>
            <th scope="col">Agents</th>
            <th scope="col">Lent to</th>
          </tr>
        </thead>
        {groups.map((g) => (
          <tbody key={g.key}>
            <tr className="acct-rank-grp">
              <th colSpan={9} scope="colgroup">
                {g.title} &middot; {g.accounts.length}
              </th>
            </tr>
            {g.accounts.map((a) => {
              n += 1
              const c = clearsOf(a, scope, now)
              const cls = ['acct-rank-row', a.account_id === chosen ? 'is-chosen' : '', a.stale && a.observed_at !== null ? 'is-stale' : '']
                .filter(Boolean)
                .join(' ')
              return (
                <tr key={a.account_id} className={cls}>
                  <td className="acct-rank-n">{n}</td>
                  <td>
                    <OpenName a={a} chosen={a.account_id === chosen} onChoose={() => onChoose(a.account_id)} />
                    <RowFacts a={a} scope={scope} now={now} />
                    <Tags a={a} names={names} />
                  </td>
                  <td>
                    <CanTakeChip a={a} scope={scope} />
                  </td>
                  <td className="acct-rk-why">{whyOf(a, scope, now, names)}</td>
                  <td className="acct-clr">
                    <b>{c.head}</b>
                    <small>{c.sub}</small>
                  </td>
                  <td className="acct-fig">
                    <WindowCell a={a} k={FIVE_HOUR} now={now} />
                  </td>
                  <td className="acct-fig">
                    <WindowCell a={a} k={SEVEN_DAY} now={now} />
                  </td>
                  <td>{agentsOf(a)}</td>
                  <td>
                    <LentCell a={a} scope={scope} />
                  </td>
                </tr>
              )
            })}
          </tbody>
        ))}
      </table>
    </div>
  )
}

/** The phone shows this many cards before "Show all" (mock-up A, P1-P4). */
export const PHONE_CARDS = 4

function AccountCards({
  groups,
  names,
  scope,
  now,
  chosen,
  onChoose,
}: {
  groups: ReturnType<typeof rankAccounts>
  names: ReadonlyMap<string, string>
  scope: string | null
  now: number
  chosen: string | null
  onChoose: (id: string) => void
}) {
  const [all, setAll] = useState(false)
  const total = groups.reduce((s, g) => s + g.accounts.length, 0)
  // Folding saves nothing on a pool of five: the "more" line would be as long as the card.
  const cut = all || total <= PHONE_CARDS + 1 ? Infinity : PHONE_CARDS
  const hidden = groups.flatMap((g) => g.accounts).slice(cut === Infinity ? total : cut)
  // Each group's first rank, so a group keeps only the cards inside the cut.
  const starts = groups.map((_, i) => groups.slice(0, i).reduce((s, g) => s + g.accounts.length, 0))
  return (
    <div className="acct-cards">
      {groups.map((g, gi) => {
        const rows = g.accounts.slice(0, Math.max(0, cut - (starts[gi] ?? 0)))
        if (rows.length === 0) return null
        return (
          <section key={g.key} className="acct-cards-grp" aria-label={g.title}>
            <h3 className="acct-cards-gh">
              {g.title} &middot; {g.accounts.length}
            </h3>
            {rows.map((a) => {
              const c = clearsOf(a, scope, now)
              const stale = a.stale && a.observed_at !== null
              return (
                <article
                  key={a.account_id}
                  className={['acct-card', a.account_id === chosen ? 'is-chosen' : '', stale ? 'is-stale' : ''].filter(Boolean).join(' ')}
                >
                  <div className="acct-card-top">
                    <OpenName a={a} chosen={a.account_id === chosen} onChoose={() => onChoose(a.account_id)} />
                    <CanTakeChip a={a} scope={scope} />
                  </div>
                  <p className="acct-card-why">{whyOf(a, scope, now, names)}</p>
                  <p className="acct-card-meta">
                    Clears: <b>{c.head}</b> &middot; {c.sub}
                  </p>
                  <div className="acct-card-w2">
                    <WindowCell a={a} k={FIVE_HOUR} now={now} />
                    <WindowCell a={a} k={SEVEN_DAY} now={now} />
                  </div>
                  <p className="acct-card-meta">
                    <span className="acct-prov" data-provider={a.provider}>
                      <ProviderName provider={a.provider} />
                    </span>{' '}
                    &middot; <b>{agentsOf(a)}</b> agents &middot; {lentPhrase(a, scope)} &middot;{' '}
                    {organisationLine(a) ?? 'organisation not recorded'}
                  </p>
                  <p className="acct-card-meta">
                    {ageOf(a, now)} &middot; {givenOut(a, now)} &middot; <span className="raw">{a.account_id}</span>
                  </p>
                  <RowMarks a={a} scope={scope} />
                  <Tags a={a} names={names} />
                </article>
              )
            })}
          </section>
        )
      })}
      {hidden.length > 0 && (
        <p className="acct-card-more">
          {hidden.length} more:{' '}
          {hidden.map((a, i) => (
            <span key={a.account_id}>
              {i > 0 && ', '}
              {/* Whole, as in the verdict: `team-copy` broke at its hyphen at 390. */}
              <span className="acct-nm">{accountName(a)}</span>
            </span>
          ))}
          .{' '}
          <button type="button" className="ctl-link" onClick={() => setAll(true)}>
            Show all {total}
          </button>
        </p>
      )}
    </div>
  )
}
