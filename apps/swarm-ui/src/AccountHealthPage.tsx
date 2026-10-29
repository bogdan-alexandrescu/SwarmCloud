// CAPACITY › ACCOUNT HEALTH (ACCOUNT-HEALTH-PAGE, part of #2302).
//
// BGD asked on 2026-10-25 for "more details about rate limit hits, error rates,
// error logs and other potential issues with the subscription accounts used and
// their health overall", and picked frames from four mock-ups
// (docs/web-ui/mockups/account-health-*.html, PR 2323):
//
//   F1  proposal B: four tiles, one per status word, a fifth that says whether
//       the pool can take work and links to Accounts; then the accounts worst
//       first with their 24 h counts by family and their newest event.
//   F3  proposal B: the 7-day swimlane, a lane per account, a mark per event,
//       limited stretches as bands, and the pool's events per hour on top.
//   F4  proposal A: the honest states -- no events yet, the record not served
//       (a dash and its reason, never a zero), an account that needs re-auth,
//       and a read that failed.
//   F5  proposal D at 390: the switch, the summary line, then the open
//       incidents first, each with its fix, then the rest.
//
// F2, one account's health, is A's side-sheet section (ACCOUNT-HEALTH-SHEET):
// every row here opens that account's sheet on Accounts.
//
// The arithmetic is in accountHealthModel.ts; this file draws it.

import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'

import {
  FAMILIES,
  FAMILY_OF,
  HOUR_MS,
  KIND_LABEL,
  NOT_SERVED_WHY,
  SPAN_HOURS,
  STATUS_HINT,
  STATUS_WORD,
  counts24h,
  familyCount,
  healthServed,
  healthyWithheld,
  healthyWithheldLine,
  hm,
  incidentSummary,
  incidentTitle,
  incidents,
  laneMarks,
  limitBands,
  poolHours,
  poolIncidents,
  served7d,
  span,
  spanStart,
  statusOf,
  when,
  whenS,
  windowWord,
  worstFirst,
  day,
  type AccountHealthRecord,
  type HealthAccount,
  type HealthFamily,
  type HealthRead,
  type HealthStatus,
  type Incident,
  type LaneMark,
  type StatusCell,
  type Tally,
} from './accountHealthModel'
import { blocksOf, isDuplicateBlock, verdictOf, type PoolAccount } from './accountsPool'
import { whyOf } from './AccountsTable'
import type { TopicId } from './help'
import { loadAccountHealthBoard, type AccountHealthBoard } from './api'
import { capacityPoll, paneHref, usePhoneTables } from './capacityPoll'
import { Screen } from './Shell'
import { unreadableFor } from './types'
import './styles/account-health.css'

/**
 * Every 60 seconds, paused while the tab is hidden (`Screen`), as proposal B
 * decided: the page is kept open through an incident, and a minute is the
 * grain its swimlane and its counts can show. `capacityPoll` keeps the phone
 * at its own 60 s.
 */
export const ACCOUNT_HEALTH_POLL_MS = 60_000

/** Where a row, a card or a fix opens the account: its side sheet on Accounts. */
export function sheetHref(accountId: string): string {
  return paneHref('capacity/accounts', { account: accountId })
}

/**
 * The page's help, as the topic index the title's `?` opens.
 *
 * NO LONGER A FOOTER (QA1-FIX-24, capacity-13, part of #2596). It was a
 * `Reading this screen:` row of underlined monospace links at the foot of
 * both layouts, because the console's glyph count is capped (B7.4,
 * `tests/help.test.ts`). VQ-033 (BGD 2026-10-26: "Move the topics behind the
 * page's one ?") moved Pools', Accounts' and Provider quota's behind the `?`
 * the title already draws -- the section's question, which a list joins
 * rather than replaces -- so this costs no glyph, and Account health does the
 * same.
 */
const HEALTH_TOPICS: readonly TopicId[] = ['account-health', 'absent-vs-zero']

export function AccountHealthScreen() {
  const phone = usePhoneTables()
  return (
    <Screen
      title="Account health"
      help={HEALTH_TOPICS}
      load={loadAccountHealthBoard}
      pollMs={capacityPoll(ACCOUNT_HEALTH_POLL_MS)}
    >
      {(b) => <AccountHealthBody board={b} now={b.readAt} phone={phone} />}
    </Screen>
  )
}

interface Model {
  accounts: HealthAccount[]
  sorted: HealthAccount[]
  records: Map<string, AccountHealthRecord>
  cell: (a: HealthAccount) => StatusCell
  health: HealthRead
  served: boolean
  now: number
  scope: string | null
  shortfall: number
}

/**
 * A row's status is `statusOf`'s, the one rule `worstFirst` sorts by, so a
 * REAUTH_REQUIRED account whose health is null leads the list as well as the
 * tiles and the cards.
 */
function cellOf(a: HealthAccount, record: AccountHealthRecord | undefined): StatusCell {
  return statusOf(a, record)
}

/** The tiles and the summary line count the statuses the list draws, from `cellOf`. */
function tallyOf(m: Model): Tally {
  const t: Tally = { healthy: 0, limited: 0, erroring: 0, reauth_needed: 0, unknown: 0 }
  for (const a of m.accounts) {
    const s = m.cell(a).status
    if (s === null) t.unknown += 1
    else t[s] += 1
  }
  return t
}

function modelOf(board: AccountHealthBoard, now: number): Model {
  const accounts = board.page.accounts as HealthAccount[]
  const records = new Map<string, AccountHealthRecord>(
    board.health.kind === 'ok' ? board.health.pool.accounts.map((r) => [r.account_id, r]) : [],
  )
  const served = healthServed(accounts, board.health)
  return {
    accounts,
    // Not served: there is no health to sort by, so the order registered.
    sorted: served ? worstFirst(accounts, records) : [...accounts].sort((x, y) => x.account_id.localeCompare(y.account_id)),
    records,
    cell: (a) => cellOf(a, records.get(a.account_id)),
    health: board.health,
    served,
    now,
    scope: board.page.tenant_id,
    shortfall: board.page.unreadable_document_count ?? 0,
  }
}

type PhoneView = 'incidents' | 'accounts'

export function AccountHealthBody({ board, now, phone }: { board: AccountHealthBoard; now: number; phone: boolean }) {
  // The phone's Incidents/Accounts choice is held HERE, above the breakpoint
  // switch: held in PhoneBody, a resize across the breakpoint unmounted it
  // and dropped the choice back to Incidents.
  const [view, setView] = useState<PhoneView>('incidents')
  const m = modelOf(board, now)
  return phone ? <PhoneBody m={m} view={view} setView={setView} /> : <DesktopBody m={m} />
}

function DesktopBody({ m }: { m: Model }) {
  return (
    <div className="ah-page" data-layout="desktop">
      <HealthBanner m={m} />
      <ReauthCallouts m={m} />
      <Tiles m={m} />
      <AccountList m={m} />
      <Timeline m={m} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// The status word: an icon AND a word, never colour alone
// ---------------------------------------------------------------------------

function StatusIcon({ s }: { s: HealthStatus | null }) {
  const common = { viewBox: '0 0 12 12', 'aria-hidden': true as const, className: 'ah-st-ic' }
  if (s === 'healthy')
    return (
      <svg {...common}>
        <path d="M2.5 6.3 5 8.6 9.6 3.6" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    )
  if (s === 'limited')
    return (
      <svg {...common}>
        <circle cx="6" cy="6" r="4.6" fill="none" stroke="currentColor" strokeWidth="1.5" />
        <path d="M6 3.4V6l1.8 1.2" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      </svg>
    )
  if (s === 'erroring')
    return (
      <svg {...common}>
        <path d="M6 1.3 11 10.4H1Z" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
        <path d="M6 4.6v2.6M6 8.7v.1" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      </svg>
    )
  if (s === 'reauth_needed')
    return (
      <svg {...common}>
        <circle cx="4" cy="6" r="2.4" fill="none" stroke="currentColor" strokeWidth="1.4" />
        <path d="M6.4 6H11M9.4 6v2M11 6v1.6" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      </svg>
    )
  return (
    <svg {...common}>
      <path d="M3 6h6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  )
}

function StatusChip({ s }: { s: HealthStatus }) {
  return (
    <span className={`ah-st is-${s}`} data-status={s}>
      <StatusIcon s={s} />
      {STATUS_WORD[s]}
    </span>
  )
}

/** A figure this deployment did not give: a dash, with the reason on it and beside it for a reader. */
function Dash({ why }: { why: string }) {
  return (
    <span className="ah-dash" title={why} aria-label={`not known: ${why}`}>
      —
    </span>
  )
}

function FamilyMark({ f }: { f: HealthFamily }) {
  return (
    <svg className={`ah-fm is-${f}`} viewBox="0 0 10 10" aria-hidden>
      {f === 'err' ? <circle cx="5" cy="5" r="4.5" /> : f === 'lim' ? <path d="M5 0 10 5 5 10 0 5Z" /> : <rect x=".5" y=".5" width="9" height="9" rx="1.5" />}
    </svg>
  )
}

/** "5-hour until 09:10Z" for a limited account, from its record. */
function untilWord(record: AccountHealthRecord | undefined, now: number): string {
  if (record === undefined || record.limited_until === null) return ''
  const until = Date.parse(record.limited_until)
  if (!Number.isFinite(until)) return ''
  const hit = record.events.find((e) => e.kind === 'rate_limit' && e.window !== null)
  const w = hit === undefined ? '' : `${windowWord(hit.window)} `
  return `${w}until ${when(until, now)}`
}

function nameOf(a: HealthAccount): string {
  return a.display_name || a.label || a.account_id
}

/**
 * WHAT THE BROKER SAYS OF IT, BESIDE ITS HEALTH (QA1-FIX-01 capacity-04).
 * Health is the provider's side; a paused account, one this workspace skips
 * because it cannot read its secret, and one subscription registered twice
 * are the broker's, and drawn Healthy or Limited alone they contradicted
 * Accounts about the same account. The words are Accounts' own (`whyOf`). A
 * signed-out account is left out: its status already says so.
 */
function BrokerNote({ m, a }: { m: Model; a: HealthAccount }) {
  if (a.state === 'REAUTH_REQUIRED') return null
  const held = a.state !== 'AVAILABLE' || unreadableFor(a, m.scope) || blocksOf(a).some(isDuplicateBlock)
  if (!held) return null
  const names = new Map(m.accounts.map((x) => [x.account_id, nameOf(x)]))
  return (
    <small className="ah-broker" data-broker={a.state}>
      {whyOf(a, m.scope, m.now, names)}
    </small>
  )
}

// ---------------------------------------------------------------------------
// F4: the deployment does not serve it, or the read failed
// ---------------------------------------------------------------------------

/**
 * A server's message as a sentence of the page's: a capital first letter (the
 * callout's paragraph starts with it) and a full stop before the page's next
 * sentence follows it.
 */
function sentence(s: string): string {
  const t = s.trim()
  if (t === '') return t
  const c = t.charAt(0).toUpperCase() + t.slice(1)
  return /[.!?…]$/.test(c) ? c : `${c}.`
}

function HealthBanner({ m, tiles = true }: { m: Model; tiles?: boolean }) {
  if (!m.served) {
    const reason = m.health.kind === 'ok' ? '' : m.health.reason
    return (
      <div className="ah-callout is-mute" role="status" data-state="not-served">
        <b>
          <span className="ah-dash">—</span> This deployment does not serve account health
        </b>
        <p>
          <code>GET /v1/accounts</code> rows carry no <code>health_status</code>. {sentence(reason || NOT_SERVED_WHY)} Rate-limit
          hits, errors and refusals are <b>unknown</b>, not zero. The pool’s capacity is still read, so{' '}
          {tiles ? 'the last tile stands' : 'the summary’s last line stands'}.
        </p>
      </div>
    )
  }
  if (m.health.kind === 'unreadable') {
    return (
      <div className="ah-callout is-bad" role="alert" data-state="unreadable">
        <b>The health read failed</b>
        <p>
          {sentence(m.health.reason)} The statuses and 24 h counts below come from the accounts list; the newest events and the
          7-day lanes could not be read, and nothing here says the pool was quiet.
        </p>
      </div>
    )
  }
  if (m.health.kind === 'not_served') {
    return (
      <div className="ah-callout is-mute" role="status" data-state="pool-not-served">
        <b>
          <span className="ah-dash">—</span> The newest events and the 7-day lanes are not served here
        </b>
        <p>{sentence(m.health.reason)} The statuses and 24 h counts below come from the accounts list.</p>
      </div>
    )
  }
  return null
}

/** F4c: an account that needs re-auth leads with its fix. */
function ReauthCallouts({ m }: { m: Model }) {
  const dead = m.sorted.filter((a) => m.cell(a).status === 'reauth_needed')
  if (dead.length === 0) return null
  return (
    <>
      {dead.map((a) => {
        // The open credential incident's start, the "since" its phone card
        // prints (QA1-FIX-01 capacity-06): the newest auth or reauth event put
        // the banner at 10:59Z and the card at 10:50Z for one incident.
        const r = m.records.get(a.account_id)
        const open = r === undefined ? undefined : incidents(r, 'reauth_needed', m.now).find((x) => x.family === 'cred' && x.open)
        const since = open === undefined || !Number.isFinite(open.from) ? '' : ` since ${when(open.from, m.now)}`
        return (
          <div key={a.account_id} className="ah-callout is-reauth" data-state="reauth" data-account={a.account_id}>
            <b>
              {nameOf(a)} needs re-auth{since}
            </b>
            <p>
              The provider refused its credential. The broker assigns this account nothing until someone signs in again; work
              that would have used it waits for another account.
            </p>
            <div className="ah-acts">
              <a className="c-btn is-sm is-primary" href={sheetHref(a.account_id)}>
                Sign in again
              </a>
            </div>
          </div>
        )
      })}
    </>
  )
}

// ---------------------------------------------------------------------------
// F1: the tiles
// ---------------------------------------------------------------------------

const TILE_ORDER: readonly HealthStatus[] = ['healthy', 'limited', 'erroring', 'reauth_needed']

/**
 * Accounts with provider errors in the last 24 h that a worse status outranks
 * (QA1-FIX-01 capacity-05): the Erroring tile counts statuses, so "Erroring 0"
 * sat over a Limited row with 9 provider errors and nothing said why.
 */
function outranked(m: Model): HealthAccount[] {
  return m.sorted.filter((a) => {
    const s = m.cell(a).status
    if (s === null || s === 'erroring') return false
    const c = counts24h(a, m.records.get(a.account_id))
    return c !== null && familyCount(c, 'err') > 0
  })
}

function OutrankedLine({ m }: { m: Model }) {
  const also = outranked(m)
  if (also.length === 0) return null
  const one = also.length === 1 ? also[0]! : null
  const s = one === null ? null : m.cell(one).status
  return (
    <small className="ah-tile-also" data-outranked={also.length}>
      {one !== null && s !== null
        ? `${nameOf(one)} also had provider errors in 24 h; it counts as ${STATUS_WORD[s]}, which outranks Erroring`
        : `${also.length} more had provider errors in 24 h, counted under a worse status: ${also.map(nameOf).join(', ')}`}
    </small>
  )
}

/** Under Healthy: the healthy accounts the pool still withholds, by name (capacity-04). */
function WithheldLine({ m }: { m: Model }) {
  const held = healthyWithheld(m.accounts, m.records, m.scope)
  const line = healthyWithheldLine(held)
  if (line === null) return null
  return (
    <small className="ah-tile-also" data-withheld={held.length}>
      {line}
    </small>
  )
}

function Tiles({ m }: { m: Model }) {
  const t = tallyOf(m)
  const v = verdictOf(m.accounts as PoolAccount[], m.scope, m.shortfall, m.now)
  return (
    <div className="ah-tiles" role="list" aria-label="Accounts by health status">
      {TILE_ORDER.map((s) => (
        <div key={s} className={`ah-tile is-${s}`} role="listitem" data-tile={s}>
          <div className="ah-tile-w">
            <StatusChip s={s} />
          </div>
          <div className="ah-tile-n" data-count={m.served ? t[s] : ''}>
            {m.served ? t[s] : <Dash why={NOT_SERVED_WHY} />}
          </div>
          <small>{m.served ? STATUS_HINT[s] : 'not served'}</small>
          {m.served && s === 'erroring' && <OutrankedLine m={m} />}
          {m.served && s === 'healthy' && <WithheldLine m={m} />}
        </div>
      ))}
      <div className="ah-tile is-verdict" role="listitem" data-tile="verdict">
        <div className="ah-tile-w">Can the pool take work now?</div>
        <div className="ah-tile-n">
          {v.n === null ? <Dash why="The broker did not say which accounts can take a new agent." /> : v.n > 0 ? 'Yes' : 'No'}
          {v.n !== null && (
            <small>
              {' '}
              · {v.n} {v.n === 1 ? 'account' : 'accounts'}
            </small>
          )}
        </div>
        <small>
          {v.n === null ? 'not said' : `${v.free} free ${v.free === 1 ? 'slot' : 'slots'}`} ·{' '}
          <a className="ctl-link" href="#capacity/accounts">
            Accounts ›
          </a>
        </small>
      </div>
      {m.served && t.unknown > 0 && (
        <p className="ah-tiles-note" data-unknown={t.unknown}>
          {t.unknown} {t.unknown === 1 ? 'account has' : 'accounts have'} no status on this read: the broker could not read
          {t.unknown === 1 ? ' its record' : ' their records'}.
        </p>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// F1: the accounts, worst first
// ---------------------------------------------------------------------------

function StatusCellView({ m, a }: { m: Model; a: HealthAccount }) {
  const c = m.cell(a)
  if (c.status === null) return <Dash why={c.why} />
  const until = c.status === 'limited' ? untilWord(m.records.get(a.account_id), m.now) : ''
  // The reset time sits under the word, free to wrap: inside the nowrap chip
  // it held the Status column wide enough to push the table 12 px past its
  // card at 1440 and cut the re-auth row's "Sign in again ›".
  return (
    <>
      <StatusChip s={c.status} />
      {until && <small className="ah-st-until">{until}</small>}
      <BrokerNote m={m} a={a} />
    </>
  )
}

/** The last day label's distance from the chart's right edge, in px. */
const EDGE_INSET = 2

/** Why a record-derived cell is a dash on this read. */
function recordWhy(m: Model): string {
  if (!m.served) return NOT_SERVED_WHY
  if (m.health.kind === 'not_served') return m.health.reason
  if (m.health.kind === 'unreadable') {
    // The served reasons already open with "The health read ..." ("timed out",
    // "answered in a shape this console does not know"); prefixing them again
    // read "The health read failed: The health read ..." on the phone.
    const why = sentence(m.health.reason)
    return /^the health read\b/i.test(why) ? why : `The health read failed: ${why}`
  }
  return "The broker sent no record for this account."
}

function LastEvent({ m, a }: { m: Model; a: HealthAccount }) {
  const r = m.records.get(a.account_id)
  if (r === undefined) return <Dash why={recordWhy(m)} />
  const e = r.events[0]
  if (e === undefined) return <span className="ah-mu">none in 7 days</span>
  const at = Date.parse(e.at)
  return (
    <span className="ah-last">
      <span className="ah-kd">
        <FamilyMark f={FAMILY_OF[e.kind]} />
        {KIND_LABEL[e.kind]}
      </span>
      <small>
        {when(at, m.now)} · {span(m.now - at)} ago
      </small>
      {/* Whole, wrapping in its held column (QA1-FIX-01 chrome-C26): cut to
          one line, the rest was a title no keyboard or touch could open. */}
      <small className="ah-msg">
        {e.message ?? 'another workspace’s event: details withheld'}
      </small>
    </span>
  )
}

function FamCell({ m, a, f }: { m: Model; a: HealthAccount; f: HealthFamily }) {
  const c = counts24h(a, m.records.get(a.account_id))
  if (c === null) return <td className="ah-num"><Dash why={m.served ? 'The broker sent no 24 h counts for this account.' : NOT_SERVED_WHY} /></td>
  const n = familyCount(c, f)
  return <td className={`ah-num${n ? '' : ' ah-mu'}`} data-fam={f}>{n}</td>
}

function AccountList({ m }: { m: Model }) {
  return (
    <section className="ctl-card ah-list">
      <div className="ctl-card-head">
        <h2 className="ctl-card-title">Accounts</h2>
        <span className="ctl-card-note">
          {m.served ? 'worst first · counts are the last 24 h' : 'in the order registered: there is no health to sort by'}
        </span>
      </div>
      {m.accounts.length === 0 ? (
        <p className="ah-empty-line">
          No accounts are registered for this workspace. <a className="ctl-link" href="#capacity/accounts">Add one on Accounts ›</a>
        </p>
      ) : (
        <div className="ctl-table is-scroll">
          <table aria-label="Accounts, worst first">
            <thead>
              <tr>
                <th>Account</th>
                <th>Status</th>
                <th className="ah-num">Limits</th>
                <th className="ah-num">Provider errors</th>
                <th className="ah-num">Credential</th>
                <th>Last event</th>
                <th aria-label="Open" />
              </tr>
            </thead>
            <tbody>
              {m.sorted.map((a) => {
                const s = m.cell(a).status
                return (
                  <tr key={a.account_id} data-account={a.account_id} data-status={s ?? 'unknown'}>
                    <td>
                      <a className="ah-nm" href={sheetHref(a.account_id)}>
                        {nameOf(a)}
                      </a>
                      <small className="ah-id">{a.account_id}</small>
                    </td>
                    <td>
                      <StatusCellView m={m} a={a} />
                    </td>
                    <FamCell m={m} a={a} f="lim" />
                    <FamCell m={m} a={a} f="err" />
                    <FamCell m={m} a={a} f="cred" />
                    <td className="ah-last-td">
                      <LastEvent m={m} a={a} />
                    </td>
                    <td className="ah-open">
                      {s === 'reauth_needed' ? (
                        <a className="ctl-link" href={sheetHref(a.account_id)}>
                          Sign in again ›
                        </a>
                      ) : (
                        <a className="ctl-link" href={sheetHref(a.account_id)}>
                          Open ›
                        </a>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
      {m.accounts.length > 0 && (
        <p className="ah-foot">
          {m.accounts.length} {m.accounts.length === 1 ? 'row' : 'rows'}. Limits are rate-limit hits, exhausted reports and
          refusals at assignment; provider errors are 529s, 5xx and network failures; credential is 401s, failed refreshes
          and re-auth marks. A row opens the account on Accounts.
        </p>
      )}
    </section>
  )
}

// ---------------------------------------------------------------------------
// F3: the swimlane
// ---------------------------------------------------------------------------

/** The chart's width before its box is measured (and under a test DOM, which has no layout). */
const W_DEFAULT = 1040
/** Below this the chart stops shrinking and its box scrolls: an hour narrower than ~1 px merges marks. */
const W_MIN = 320
const STRIP_TOP = 22
const STRIP_H = 72
const LANE_H = 46
const LANES_TOP = 112
/**
 * Half a mark button (`.ah-hit` is 12 px): the hours end this far inside the
 * chart's right edge, so a mark at now is drawn whole. Mapped to the edge
 * itself, the newest marks reached 3-6 px past the box at 390 (PR 2357's
 * fourth review).
 */
const MARK_R = 6

function markSay(mk: LaneMark, name: string, now: number): string {
  const e = mk.event
  const parts = [`${name}`, whenS(mk.at, now), KIND_LABEL[e.kind]]
  if (e.window) parts.push(`${windowWord(e.window)} window`)
  if (e.tenant_id) parts.push(`tenant ${e.tenant_id}`)
  if (e.message) parts.push(e.message)
  return parts.join(' · ')
}

/**
 * The width of the box the chart is drawn into, followed as it resizes. The
 * chart is DRAWN TO FIT that box rather than at a fixed width inside a
 * scroller: at a fixed 1040 px the newest hours (and the now line) sat past the
 * card's edge at 1440 and only one day of seven showed at 390, so the lanes
 * read as quieter than the counts beside them.
 *
 * It is the CONTENT width: `clientWidth` counts the scroller's own side padding
 * (`--ctl-pad-chrome`, 16 px a side), and a chart sized to it started 16 px in
 * and ran 32 px past the box, so the newest hour scrolled out of sight and
 * "now 12:00Z" read "now 12:0" (PR 2357's re-review).
 *
 * `undefined` until the box has been measured once: the chart is not drawn
 * before then, and the measure runs in a LAYOUT effect, before the browser
 * paints, so no frame is drawn at `W_DEFAULT` and then refitted (at 390 the
 * first frame was 1040 px wide in a 324 px box). `null` is a box measured with
 * no width (a hidden tab, or a test DOM), which draws at `W_DEFAULT`.
 */
function useBoxWidth(el: HTMLElement | null): number | null | undefined {
  const [w, setW] = useState<number | null | undefined>(undefined)
  useLayoutEffect(() => {
    if (el === null) return
    const measure = () => {
      const cs = getComputedStyle(el)
      const content = el.clientWidth - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0)
      setW(content > 0 ? content : null)
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const watch = new ResizeObserver(measure)
    watch.observe(el)
    return () => watch.disconnect()
  }, [el])
  return w
}

function Timeline({ m }: { m: Model }) {
  const [picked, setPicked] = useState<LaneMark | null>(null)
  const [fit, setFit] = useState<HTMLDivElement | null>(null)
  const boxW = useBoxWidth(fit)
  const head = (
    <div className="ctl-card-head">
      <h2 className="ctl-card-title">Hits and errors over time</h2>
      <span className="ctl-card-note" data-timeline-note="">
        {m.health.kind === 'ok' ? null : '7 days'}
      </span>
    </div>
  )
  if (m.health.kind !== 'ok') {
    return (
      <section className="ctl-card ah-timeline" data-state={m.health.kind}>
        {head}
        <p className="ah-empty-line">
          <Dash why={recordWhy(m)} /> {recordWhy(m)} The lanes are not drawn: an empty chart would say nothing went wrong.
        </p>
      </section>
    )
  }
  const pool = m.health.pool
  const start = spanStart(pool, m.now)
  const rows = m.sorted
    .map((a) => ({ a, r: m.records.get(a.account_id) }))
    .filter((x): x is { a: HealthAccount; r: AccountHealthRecord } => x.r !== undefined)
  const marks = rows.flatMap((x) => laneMarks(x.r, start, m.now))
  const busy = new Set(marks.map((mk) => mk.account_id)).size
  const counted = served7d(pool)
  const older = Math.max(0, counted - marks.length)
  if (marks.length === 0 && counted === 0) {
    return (
      <section className="ctl-card ah-timeline" data-state="no-events">
        {head}
        <div className="ah-quiet" data-empty="no-events">
          <p className="ah-quiet-h">
            <StatusIcon s="healthy" />
            No events in 7 days
          </p>
          <p>
            No rate-limit hits, refusals, provider errors or credential trouble on any account since {day(start)}{' '}
            {hm(start)}. The records were read at {hm(m.now)} and are empty: a measured nothing, so there is nothing to
            draw.
          </p>
        </div>
      </section>
    )
  }
  const W = boxW === null || boxW === undefined ? W_DEFAULT : Math.max(W_MIN, Math.floor(boxW))
  const narrow = W < 640
  const GUTTER = narrow ? 88 : 130
  // A lane name longer than the gutter is cut with an ellipsis; its full name is the lane's title.
  const fitName = (n: string) => {
    const most = Math.floor((GUTTER - 8) / 6.5)
    return n.length <= most ? n : `${n.slice(0, most - 1)}…`
  }
  const bw = (W - MARK_R - GUTTER) / SPAN_HOURS
  const xOf = (t: number) => GUTTER + ((t - start) / HOUR_MS) * bw
  const hours = poolHours(pool, start)
  const mx = Math.max(1, ...hours.map((h) => h.total))
  const tallest = hours.reduce((best, h) => (h.total > best.total ? h : best), hours[0]!)
  const H = LANES_TOP + rows.length * LANE_H + 24
  const nowX = Math.min(W - MARK_R, xOf(m.now))
  const days: number[] = []
  for (let t = Math.ceil(start / (24 * HOUR_MS)) * 24 * HOUR_MS; t < start + SPAN_HOURS * HOUR_MS; t += 24 * HOUR_MS) days.push(t)
  // A day narrower than its label labels every other day (the phone); the grid line still marks each.
  const labelEvery = 24 * bw < 52 ? 2 : 1
  const nameById = new Map(rows.map((x) => [x.a.account_id, nameOf(x.a)]))
  const pick = (mk: LaneMark) => setPicked((p) => (p !== null && p.event === mk.event ? null : mk))
  return (
    <section className="ctl-card ah-timeline" data-state="ok">
      <div className="ctl-card-head">
        <h2 className="ctl-card-title">Hits and errors over time</h2>
        <span className="ctl-card-note" data-timeline-note="" data-marks={marks.length}>
          {marks.length} {marks.length === 1 ? 'event' : 'events'} on {busy} {busy === 1 ? 'account' : 'accounts'} in 7 days
        </span>
      </div>
      <div className="ah-chart-scroll" ref={setFit}>
        {boxW !== undefined && (
          <div className="ah-swim-box" style={{ width: W, height: H }} data-chart-width={W}>
            <svg
              className="ah-swim"
              viewBox={`0 0 ${W} ${H}`}
              width={W}
              height={H}
              role="group"
              aria-label="Rate-limit hits and errors per account over 7 days"
            >
              <text className="ah-ax" x={nowX} y={12} textAnchor="end">
                now {hm(m.now)}
              </text>
              <text className="ah-ax is-nm" x={0} y={34}>
                {narrow ? 'Pool, per hour' : 'Whole pool, per hour'}
              </text>
              <text className="ah-ax" x={0} y={50}>
                {narrow ? `top ${tallest.total} at ${hm(tallest.at)}` : `tallest: ${tallest.total} at ${hm(tallest.at)}`}
              </text>
              {hours.map((h, i) => {
                if (h.total === 0) return null
                const x = GUTTER + i * bw + 0.3
                const w = Math.max(1, bw - 0.6)
                let y = STRIP_TOP + STRIP_H
                return (
                  <g key={`h${i}`} data-pool-hour={h.total}>
                    <title>
                      {`${day(h.at)} ${hm(h.at)}: ${h.lim} limits · ${h.err} provider errors · ${h.cred} credential`}
                    </title>
                    {(['err', 'lim', 'cred'] as const).map((f) => {
                      if (h[f] === 0) return null
                      const hh = Math.max(1.5, (h[f] / mx) * STRIP_H)
                      y -= hh
                      return <rect key={f} className={`ah-bar is-${f}`} x={x} y={y} width={w} height={hh} />
                    })}
                  </g>
                )
              })}
              <line className="ah-grid" x1={GUTTER} x2={W} y1={STRIP_TOP + STRIP_H} y2={STRIP_TOP + STRIP_H} />
              {days.map((t, i) => {
                const x = xOf(t)
                const label = day(t)
                const edge = x + 3 + label.length * 7 > W
                // An end-anchored label at exactly W overhung the scroll box by
                // 0.18 px at 390 (its glyph box is wider than its advance), and
                // that sliver drew a scrollbar that scrolled nothing (#2302,
                // ACCOUNT-HEALTH-PAGE-REVIEW5). EDGE_INSET keeps it inside.
                return (
                  <g key={t}>
                    <line className="ah-grid" x1={x} x2={x} y1={STRIP_TOP} y2={H - 18} />
                    {(days.length - 1 - i) % labelEvery === 0 && (
                      <text className="ah-ax" x={edge ? W - EDGE_INSET : x + 3} y={H - 4} textAnchor={edge ? 'end' : 'start'}>
                        {label}
                      </text>
                    )}
                  </g>
                )
              })}
              {rows.map(({ a, r }, i) => {
                const y = LANES_TOP + i * LANE_H
                return (
                  <g key={a.account_id} data-lane={a.account_id}>
                    <rect className="ah-lane" x={GUTTER} y={y + 3} width={W - GUTTER} height={LANE_H - 6} rx={4} />
                    <text className="ah-ax is-nm" x={0} y={y + LANE_H / 2 + 4}>
                      <title>{nameOf(a)}</title>
                      {fitName(nameOf(a))}
                    </text>
                    {limitBands(r).map((b) => {
                      const x0 = Math.max(GUTTER, xOf(b.from))
                      const x1 = Math.min(W, xOf(b.to))
                      return (
                        <g key={`b${b.from}`} data-band={b.window ?? ''}>
                          <title>{`${nameOf(a)}: ${windowWord(b.window) || 'a'} window · hit ${when(b.from, m.now)} · resets ${when(b.to, m.now)} · ${b.hits} ${b.hits === 1 ? 'hit' : 'hits'}`}</title>
                          <rect className="ah-band" x={x0} y={y + 3} width={Math.max(3, x1 - x0)} height={LANE_H - 6} />
                          <line className="ah-band-l" x1={x0} x2={x0} y1={y + 3} y2={y + LANE_H - 3} />
                          {x1 - x0 > 44 && (
                            <text className="ah-ax is-band" x={x0 + 4} y={y + 15}>
                              {windowWord(b.window)}
                            </text>
                          )}
                        </g>
                      )
                    })}
                  </g>
                )
              })}
              <line className="ah-now" x1={nowX} x2={nowX} y1={16} y2={H - 18} />
            </svg>
            {/* THE MARKS ARE BUTTONS, laid over the chart at its own coordinates
                (it is drawn at a fixed pixel size, so a chart unit is a pixel).
                Each is one served event: hover names it, a press or Enter shows
                its time, kind, tenant and task under the chart. HTML rather than
                an SVG `<g tabindex>`, because the console's focus model
                (focus.ts, the keyboard sweep) reaches HTML controls only. */}
            {rows.map(({ a, r }, i) => {
              const y = LANES_TOP + i * LANE_H
              return laneMarks(r, start, m.now).map((mk, j) => {
                const x = xOf(mk.at)
                const yy = y + LANE_H / 2 + 5 + (mk.family === 'err' ? -8 : mk.family === 'lim' ? 0 : 8)
                const say = markSay(mk, nameOf(a), m.now)
                const on = picked !== null && picked.event === mk.event
                return (
                  <button
                    key={`${a.account_id}-${j}`}
                    type="button"
                    className={`ah-hit is-${mk.family}${on ? ' is-on' : ''}`}
                    style={{ left: x - 6, top: yy - 6 }}
                    data-mark={mk.family}
                    data-mark-of={a.account_id}
                    title={say}
                    aria-label={say}
                    aria-pressed={on}
                    onClick={() => pick(mk)}
                  >
                    <FamilyMark f={mk.family} />
                  </button>
                )
              })
            })}
          </div>
        )}
      </div>
      {picked !== null && <PickedEvent mk={picked} name={nameById.get(picked.account_id) ?? picked.account_id} now={m.now} />}
      <Legend />
      <p className="ah-foot">
        One lane per account. A mark is one event: errors sit high, limits on the line, credential low. A band runs from a
        rate-limit hit to its window’s reset. Hover a mark for its message, or press it for the task.
        {older > 0 && (
          <>
            {' '}
            <span data-older={older}>
              {older} older {older === 1 ? 'event counts' : 'events count'} in the strip and the 7-day totals only: each
              account’s record keeps its last {pool.events_max ?? 50} messages.
            </span>
          </>
        )}
      </p>
    </section>
  )
}

function PickedEvent({ mk, name, now }: { mk: LaneMark; name: string; now: number }) {
  const e = mk.event
  return (
    <div className="ah-picked" role="status" data-picked="">
      <b>
        {name} · {whenS(mk.at, now)}
      </b>
      <span className="ah-kd">
        <FamilyMark f={mk.family} />
        {KIND_LABEL[e.kind]}
        {e.window ? ` · ${windowWord(e.window)} window` : ''}
        {e.reset_at ? ` · resets ${when(Date.parse(e.reset_at), now)}` : ''}
      </span>
      <span>{e.tenant_id ? `tenant ${e.tenant_id}` : 'another workspace'}</span>
      {e.task_id ? (
        <a className="ctl-link" href={`#work/task/${encodeURIComponent(e.task_id)}`}>
          {e.task_id}
        </a>
      ) : (
        <span className="ah-mu">no task shown</span>
      )}
      <span className="ah-msg">{e.message ?? 'details withheld: another workspace’s event'}</span>
    </div>
  )
}

function Legend() {
  return (
    <div className="ah-legend">
      {FAMILIES.map((f) => (
        <span key={f.key}>
          <FamilyMark f={f.key} />
          {f.label}
        </span>
      ))}
      <span>
        <i className="ah-band-k" />
        limited: from the hit to the window’s reset
      </span>
    </div>
  )
}

// ---------------------------------------------------------------------------
// F5: the phone (proposal D, P1)
// ---------------------------------------------------------------------------

function PhoneBody({ m, view, setView }: { m: Model; view: PhoneView; setView: (v: PhoneView) => void }) {
  const statusFor = (id: string) => {
    const a = m.accounts.find((x) => x.account_id === id)
    return a === undefined ? null : m.cell(a).status
  }
  const inc =
    m.health.kind === 'ok'
      ? poolIncidents(
          m.health.pool,
          statusFor,
          m.now,
          m.accounts.map((a) => ({ account_id: a.account_id, label: nameOf(a) })),
        )
      : null
  // FOCUS STAYS ON THE SWITCH (QA1-FIX-01 capacity-09). Choosing Accounts
  // dropped focus to <body> in the live browser, so the next Tab began again
  // at the top. A choice made on the switch puts focus on the chosen button
  // once the panels have swapped, and the arrow keys move between the two.
  const sw = useRef<HTMLDivElement>(null)
  const chose = useRef(false)
  useEffect(() => {
    if (!chose.current) return
    chose.current = false
    sw.current?.querySelector<HTMLButtonElement>('button[aria-pressed="true"]')?.focus()
  }, [view])
  const choose = (v: PhoneView) => {
    chose.current = true
    if (v === view) sw.current?.querySelector<HTMLButtonElement>('button[aria-pressed="true"]')?.focus()
    else setView(v)
  }
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'ArrowLeft' || e.key === 'Home') choose('incidents')
    else if (e.key === 'ArrowRight' || e.key === 'End') choose('accounts')
    else return
    e.preventDefault()
  }
  return (
    <div className="ah-page" data-layout="phone">
      <div className="ah-switch" role="group" aria-label="View" ref={sw} onKeyDown={onKey}>
        <button type="button" aria-pressed={view === 'incidents'} onClick={() => choose('incidents')}>
          Incidents {inc === null ? <span className="ah-dash">—</span> : `· ${inc.open.length} open`}
        </button>
        <button type="button" aria-pressed={view === 'accounts'} onClick={() => choose('accounts')}>
          Accounts
        </button>
      </div>
      <HealthBanner m={m} tiles={view === 'accounts'} />
      {view === 'incidents' ? (
        <>
          <SummaryLine m={m} open={inc?.open ?? null} />
          {inc === null ? (
            <>
              {/* The incidents need the health read; a re-auth does not. The
                  list's own status (or the row's state) still says which
                  account needs signing in again, and that fix is the one card
                  Incidents must not lose. */}
              <ReauthCallouts m={m} />
              <p className="ah-empty-line" data-state="no-incidents">
              <Dash why={recordWhy(m)} /> Incidents cannot be drawn: {recordWhy(m)} An empty list would claim nothing went
              wrong.
              </p>
            </>
          ) : (
            <>
              <section className="ah-open-list" aria-label="Open incidents" data-section="open">
                <h2 className="ah-sec-h">Open now · {inc.open.length}</h2>
                {inc.open.length === 0 && <p className="ah-mu">Nothing is open on any account.</p>}
                {inc.open.map((x) => (
                  <IncidentCard key={`${x.account_id}-${x.family}-${x.from}`} x={x} now={m.now} />
                ))}
              </section>
              <section className="ah-closed-list" aria-label="Closed incidents" data-section="closed">
                <h2 className="ah-sec-h">Closed, 7 days · {inc.closed.length}</h2>
                {inc.closed.length === 0 ? (
                  <p className="ah-mu">No closed incidents in the records.</p>
                ) : (
                  <ul>
                    {inc.closed.map((x) => (
                      <li key={`${x.account_id}-${x.family}-${x.from}`} data-incident={x.family}>
                        <span className="ah-kd">
                          <FamilyMark f={x.family} />
                          {incidentTitle(x)}
                        </span>
                        <small>
                          {when(x.from, m.now)} · {x.events.length} {x.events.length === 1 ? 'event' : 'events'}
                        </small>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            </>
          )}
        </>
      ) : (
        <>
          <ReauthCallouts m={m} />
          <Tiles m={m} />
          <AccountCards m={m} />
          <Timeline m={m} />
        </>
      )}
    </div>
  )
}

function SummaryLine({ m, open }: { m: Model; open: Incident[] | null }) {
  const t = tallyOf(m)
  const v = verdictOf(m.accounts as PoolAccount[], m.scope, m.shortfall, m.now)
  const on = open === null ? null : new Set(open.map((x) => x.account_id)).size
  return (
    <div className="ah-sumline" data-summary="">
      {open !== null && (
        <span>
          <b>{open.length} open</b> on {on} {on === 1 ? 'account' : 'accounts'}
        </span>
      )}
      {m.served &&
        TILE_ORDER.map((s) => (
          <span key={s} data-sum={s}>
            <StatusChip s={s} /> {t[s]}
          </span>
        ))}
      <span>
        {v.n === null ? (
          'Whether the pool can take work was not said'
        ) : (
          <>
            The pool <b>{v.n > 0 ? 'can take work' : 'cannot take work'}</b>: {v.n} {v.n === 1 ? 'account' : 'accounts'},{' '}
            {v.free} free {v.free === 1 ? 'slot' : 'slots'}
          </>
        )}{' '}
        <a className="ctl-link" href="#capacity/accounts">
          (Accounts)
        </a>
      </span>
    </div>
  )
}

function IncidentCard({ x, now }: { x: Incident; now: number }) {
  const last = x.events[x.events.length - 1]
  const spanText =
    last === undefined
      ? 'since before the served events'
      : x.family === 'lim'
      ? x.open && x.to !== null
        ? `since ${when(x.from, now)} · clears ${when(x.to, now)} (in ${span(x.to - now)})`
        : x.to !== null
          ? `${when(x.from, now)} – ${when(x.to, now)}`
          : when(x.from, now)
      : x.open
        ? `since ${when(x.from, now)} · last ${span(now - x.last)} ago`
        : x.events.length > 1
          ? `${when(x.from, now)} – ${hm(x.last)}`
          : when(x.from, now)
  return (
    <article className={`ah-inc is-${x.family}${x.open ? ' is-open' : ''}`} data-incident={x.family} data-open={x.open}>
      <div className="ah-inc-h">
        <FamilyMark f={x.family} />
        <h3>{incidentTitle(x)}</h3>
        <span className={`ah-chip${x.open ? ' is-open' : ''}`}>{x.open ? 'open' : 'closed'}</span>
      </div>
      <p className="ah-inc-when">{spanText}</p>
      {last === undefined ? (
        <p className="ah-msg" data-state="no-cred-event">
          The account’s status is re-auth needed, but its credential event is not among the events served (older than 7
          days, or past the newest 50).
        </p>
      ) : (
        <>
          <p className="ah-inc-sum">{incidentSummary(x)}</p>
          <p className="ah-msg">{last.message ?? 'details withheld: another workspace’s event'}</p>
        </>
      )}
      <div className="ah-acts">
        {x.family === 'cred' && x.open && (
          <a className="c-btn is-sm is-primary" href={sheetHref(x.account_id)} data-fix="signin">
            Sign in again
          </a>
        )}
        {x.family === 'lim' && x.open && x.to !== null && (
          <span className="ah-fix" data-fix="clears">
            Clears {when(x.to, now)}
          </span>
        )}
        <a className="c-btn is-sm" href={sheetHref(x.account_id)}>
          {x.label} ›
        </a>
      </div>
    </article>
  )
}

function AccountCards({ m }: { m: Model }): ReactNode {
  return (
    <section className="ah-cards" aria-label="Accounts, worst first">
      {m.sorted.map((a) => {
        const r = m.records.get(a.account_id)
        const c = counts24h(a, r)
        const cell = m.cell(a)
        const e = r?.events[0]
        return (
          <a key={a.account_id} className="ah-acard" href={sheetHref(a.account_id)} data-account={a.account_id}>
            <span className="ah-acard-top">
              <span className="ah-nm">{nameOf(a)}</span>
              {cell.status === null ? <Dash why={cell.why} /> : <StatusChip s={cell.status} />}
            </span>
            {cell.status === 'limited' && untilWord(r, m.now) && (
              <span className="ah-meta">
                <b>{untilWord(r, m.now)}</b>
              </span>
            )}
            <BrokerNote m={m} a={a} />
            <span className="ah-meta">
              {c === null ? (
                <>
                  24 h: <Dash why={recordWhy(m)} />
                </>
              ) : (
                `24 h: ${familyCount(c, 'lim')} limits · ${familyCount(c, 'err')} provider errors · ${familyCount(c, 'cred')} credential`
              )}
            </span>
            <span className="ah-meta">
              {r === undefined ? (
                <>
                  last: <Dash why={recordWhy(m)} />
                </>
              ) : e === undefined ? (
                'no events in 7 days'
              ) : (
                `last: ${KIND_LABEL[e.kind].toLowerCase()}, ${span(m.now - Date.parse(e.at))} ago`
              )}
            </span>
          </a>
        )
      })}
    </section>
  )
}
