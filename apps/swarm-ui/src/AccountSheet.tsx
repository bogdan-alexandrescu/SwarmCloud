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
import { useState, type ReactNode } from 'react'

import { accountName } from './accountName'
import { setAccountMaxAgents } from './api'
import { Button } from './components'
import { clearsOf, whyOf, WindowCell, ageOf } from './AccountsTable'
import {
  AGED_AFTER_MIN,
  DEFAULT_MAX_AGENTS,
  accountPaceOf,
  canTake,
  groupOf,
  isDuplicateBlock,
  blocksOf,
  maxAgentsOf,
  organisationLine,
  pct,
  slotsLine,
  utcAt,
  type PoolAccount,
} from './accountsPool'
import type { ApiError } from './fetch'
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
  const save = async () => {
    setBusy(true)
    setError(null)
    const res = await setAccountMaxAgents(a.account_id, n)
    setBusy(false)
    if (res.status === 'ok') {
      setEditing(false)
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
          onSubmit={(e) => {
            e.preventDefault()
            if (valid && !busy) void save()
          }}
        >
          <label>
            <span>Agents at once</span>
            <input
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
          <Button kind="ghost" size="sm" disabled={busy} onClick={() => setEditing(false)}>
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
  let say: ReactNode
  if (p === null) {
    say = (
      <>
        <span className="acct-dash">&mdash;</span> no 7-day reading to take a pace from.
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
      {p !== null && p.read && (
        <p className="acct-ss-note">
          Pace: {pct(p.perHour * 24)} of the week a day, its 7-day share used over the hours since its week opened.
        </p>
      )}
      <p className="acct-ss-note">Laptop sessions on this subscription spend the same window.</p>
    </section>
  )
}
