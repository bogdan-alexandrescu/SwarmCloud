/**
 * "WHY IT'S WAITING" (#1782, WAIT-UI-PANEL): the agent page's answer to the
 * question that brings people to a waiting agent.
 *
 * WHY THIS REPLACES THE BARS. The page for task_31c301ecb33940eeb278
 * (SIZES-SCHED-2's merge step, 2026-10-21) said "waiting for CI · eligible
 * since 19:46:25" and "last scheduler pass CI_PENDING" -- the same two lines
 * for every merge step of the CI stall, whether its checks were queued,
 * failing or done -- while its park events held the pull request, the head,
 * five pending checks by name, two wakes and a refunded attempt. WAIT-API
 * collects those facts as `task.waiting` (swarm_api/waiting_detail.py,
 * docs/waiting.md). This panel draws them, so when the API serves the object
 * the bars step aside (`Alerts` in AgentDetail.tsx); an older API that serves
 * none keeps the bars.
 *
 * RECORDED FACTS ONLY, AND AN ABSENCE SAYS SO. The API serves null for a fact
 * nothing recorded and never infers one. A null here reads "not recorded" --
 * never blank, never 0, never "none" -- because each of those is a claim the
 * platform did not make: "0 re-checks" says it looked and found none. A
 * recorded 0 or an empty list is a fact and is drawn as one.
 *
 * NOTHING IS FETCHED. The CI detail is what the merge step recorded at its
 * last check, so its check list carries that check's time ("as of"), not the
 * time of this read. Times are the viewer's clock (`clockTime`), the format
 * every other time on this page uses.
 */
import { useState, type ReactNode } from 'react'

import { HelpCard } from './HelpCard'
import { agentPath } from './OverviewRegions'
import { clockTime, formatDuration, type Task, type TaskState, type Waiting, type WaitingHistoryEntry } from './types'

/** History entries shown before "show all N". The newest stay; the oldest fold. */
export const HISTORY_SHOWN = 5

/** The parks only a person ends: the panel takes the warn rule, as the why line does (AG-14). */
const NEEDS_A_PERSON = new Set(['AWAITING_INPUT', 'HUMAN_REQUIRED', 'CREDENTIAL_MISSING', 'BUDGET_EXHAUSTED', 'MANUAL_PAUSE'])

/**
 * The full object `GET /v1/tasks/{id}` serves, or null. A list row serves only
 * `{reason, summary}` (`WaitingRow`); drawn as the full object, every fact in
 * it would read "not recorded" when it was simply not sent.
 */
export function fullWaiting(task: Task): Waiting | null {
  const w = task.waiting
  if (!w || typeof w !== 'object') return null
  if (!('resumes_when' in w) || !('detail' in w)) return null
  return w as Waiting
}

/** "not recorded", with the API's own reason as its title when it served one. */
function NotRecorded({ why }: { why?: string | null }) {
  return (
    <span className="wp-nr" title={why ?? 'The platform recorded no value for this.'}>
      not recorded
    </span>
  )
}

const str = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : null)
const int = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const obj = (v: unknown): Record<string, unknown> | null =>
  v !== null && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null
const names = (v: unknown): string[] | null => (Array.isArray(v) ? v.map((x) => String(x)) : null)

function When({ iso, now }: { iso: unknown; now: number }) {
  const at = clockTime(str(iso), now)
  if (at === null) return <NotRecorded />
  return <time dateTime={str(iso)!} title={at.title}>{at.text}</time>
}

function Text({ value, why }: { value: unknown; why?: string | null }) {
  const s = str(value)
  return s === null ? <NotRecorded why={why} /> : <>{s}</>
}

function Num({ value }: { value: unknown }) {
  const n = int(value)
  return n === null ? <NotRecorded /> : <>{String(n)}</>
}

/** One fact: a label and its value, keyed for the tests and the reader alike. */
function Fact({ k, label, note, children }: { k: string; label: string; note?: ReactNode; children: ReactNode }) {
  return (
    <div className="wp-fact" data-fact={k}>
      <dt>
        {label}
        {note}
      </dt>
      <dd>{children}</dd>
    </div>
  )
}

/** A dependency or a child: a link to its page, and its state. */
function Related({ rows, label }: { rows: unknown; label: (r: Record<string, unknown>) => string }) {
  if (!Array.isArray(rows)) return <NotRecorded />
  if (rows.length === 0) return <>no task named</>
  return (
    <ul className="wp-on">
      {rows.map((raw, i) => {
        const r = obj(raw) ?? {}
        const id = str(r.task_id) ?? ''
        const state = str(r.state)
        return (
          <li key={`${id}-${i}`} data-wait-on={id}>
            <a className="ctl-link" href={agentPath({ id, state: (state ?? 'PARKED') as TaskState })}>
              {label(r)}
            </a>{' '}
            {state !== null ? <span className="dt-chip">{state}</span> : <NotRecorded why="Its state was not read." />}
          </li>
        )
      })}
    </ul>
  )
}

function CiFacts({ d, now }: { d: Record<string, unknown>; now: number }) {
  const pr = obj(d.pull_request)
  const number = pr ? int(pr.number) : null
  const url = pr ? str(pr.url) : null
  const head = str(d.head)
  const pending = names(d.pending)
  const failed = names(d.failed)
  const counted = typeof d.attempts_counted === 'boolean' ? d.attempts_counted : null
  const checks = [
    ...(failed ?? []).map((n) => ({ n, state: 'failed' as const })),
    ...(pending ?? []).map((n) => ({ n, state: 'pending' as const })),
  ]
  const asOf = clockTime(str(d.last_checked_at), now)
  return (
    <>
      <Fact k="pr" label="Pull request">
        {number === null ? (
          <NotRecorded />
        ) : url !== null ? (
          <a className="ctl-link" href={url} target="_blank" rel="noreferrer" data-wait-pr="">
            #{number}
          </a>
        ) : (
          <>#{number}</>
        )}
      </Fact>
      <Fact k="head" label="Head commit">
        {head === null ? <NotRecorded /> : <code title={head}>{head.slice(0, 8)}</code>}
      </Fact>
      {/* "AS OF" THE MERGE STEP'S OWN CHECK, never this read: nothing is
          fetched from GitHub when the page loads (docs/waiting.md). */}
      <Fact
        k="checks"
        label="Checks"
        note={
          <span className="wp-asof" data-wait-as-of="" title={asOf?.title}>
            {asOf === null ? 'as of: not recorded' : `as of ${asOf.text}`}
          </span>
        }
      >
        {pending === null && failed === null ? (
          <NotRecorded />
        ) : checks.length === 0 ? (
          <>no check pending or failed</>
        ) : (
          <ul className="wp-checks">
            {checks.map((c) => (
              <li key={`${c.state}-${c.n}`} className={`wp-check is-${c.state}`} data-check={c.n} data-check-state={c.state}>
                <span className="wp-check-n">{c.n}</span> <span className="wp-check-s">{c.state}</span>
              </li>
            ))}
          </ul>
        )}
      </Fact>
      <Fact k="rechecks" label="Re-checks so far">
        <Num value={d.wakes} />
      </Fact>
      <Fact k="attempts" label="Attempts">
        {counted === null ? <NotRecorded /> : counted ? 'counted against attempts' : 'not counted against attempts'}
      </Fact>
    </>
  )
}

function CapacityFacts({ d }: { d: Record<string, unknown> }) {
  const active = int(d.active)
  const limit = int(d.limit)
  const units = int(d.units)
  return (
    <>
      <Fact k="pool" label="Binding pool">
        <Text value={d.pool} />
      </Fact>
      <Fact k="pool-state" label="Pool state">
        <Text value={d.pool_state} />
      </Fact>
      <Fact k="units" label="Units">
        {active !== null && limit !== null ? `${active} of ${limit} units in use` : <NotRecorded />}
        {' · needs '}
        {units !== null ? String(units) : <NotRecorded />}
      </Fact>
      <Fact k="place" label="Place in line">
        {int(d.place_in_line) !== null ? String(d.place_in_line) : <NotRecorded why={str(d.place_in_line_unavailable)} />}
      </Fact>
    </>
  )
}

function ProviderFacts({ d, now }: { d: Record<string, unknown>; now: number }) {
  return (
    <>
      <Fact k="provider" label="Provider">
        <Text value={d.provider} />
      </Fact>
      <Fact k="account" label="Account">
        <Text value={d.account} />
      </Fact>
      <Fact k="until" label="Until">
        <When iso={d.until} now={now} />
      </Fact>
      {str(d.provider_state) !== null && (
        <Fact k="provider-state" label="Provider state">
          {str(d.provider_state)}
        </Fact>
      )}
    </>
  )
}

function RetryFacts({ d, now }: { d: Record<string, unknown>; now: number }) {
  const next = int(d.next_attempt)
  const max = int(d.max_attempts)
  return (
    <>
      <Fact k="last-error" label="Last error">
        {str(d.last_error) === null ? <NotRecorded /> : <span className="wp-err">{str(d.last_error)}</span>}
      </Fact>
      <Fact k="attempt" label="Attempt">
        {next === null ? <NotRecorded /> : String(next)}
        {' of '}
        {max === null ? <NotRecorded /> : String(max)}
      </Fact>
      <Fact k="next-try" label="Next try">
        <When iso={d.next_try_at} now={now} />
      </Fact>
      {str(d.cause) !== null && (
        <Fact k="cause" label="Cause">
          {str(d.cause)}
        </Fact>
      )}
    </>
  )
}

function PersonFacts({ reason, d, now }: { reason: string; d: Record<string, unknown>; now: number }) {
  const who = obj(d.who_may_answer)
  const how = str(d.answer_command) ?? str(d.answer_route)
  return (
    <>
      <Fact k="who" label="Who may answer">
        <Text value={who?.text} />
      </Fact>
      <Fact k="expires" label="Until">
        <When iso={d.expires_at} now={now} />
      </Fact>
      <Fact k="answer" label={reason === 'HUMAN_REQUIRED' ? 'Answer with' : 'Answer command'}>
        {how === null ? <NotRecorded /> : <code className="wp-cmd">{how}</code>}
      </Fact>
      {str(d.cause) !== null && (
        <Fact k="cause" label="Cause">
          {str(d.cause)}
        </Fact>
      )}
    </>
  )
}

/**
 * A MERGE HELD BY THE OWNER'S QUESTIONS (#2313): who may answer, how many
 * questions and where they are -- the held-questions card under this panel,
 * with the answer control -- and NO "Answer with" human-ready route: the API
 * serves none (swarm_api/waiting_detail.py `_owner_questions`), because the
 * last answer resumes it by itself. Only a hold the answer route cannot read
 * whole carries one, and then it is drawn.
 */
function OwnerQuestionFacts({ d }: { d: Record<string, unknown> }) {
  const who = obj(d.who_may_answer)
  const asked = Array.isArray(d.questions) ? d.questions.length : null
  const unreadable = int(d.unreadable)
  const route = str(d.answer_route)
  return (
    <>
      <Fact k="who" label="Who may answer">
        <Text value={who?.text} />
      </Fact>
      <Fact k="questions" label="Questions">
        {asked === null ? (
          <NotRecorded />
        ) : (
          <>
            {asked} open ·{' '}
            {/* A button, not `href="#held-questions"`: the console still routes
                legacy hash addresses, so that anchor sent the page to Overview
                (nav.links.test.tsx). This scrolls to the card instead. */}
            <button
              type="button"
              className="ctl-link"
              onClick={() => document.getElementById('held-questions')?.scrollIntoView?.({ block: 'start' })}
            >
              answer them below
            </button>
          </>
        )}
      </Fact>
      {unreadable !== null && unreadable > 0 && (
        <Fact k="unreadable" label="Unreadable entries">
          {String(unreadable)}
        </Fact>
      )}
      {route !== null && (
        <Fact k="answer" label="Then">
          <code className="wp-cmd">{route}</code>
        </Fact>
      )}
    </>
  )
}

function DetailFacts({ w, now }: { w: Waiting; now: number }) {
  const d = obj(w.detail) ?? {}
  switch (w.reason) {
    case 'CI_PENDING':
      return <CiFacts d={d} now={now} />
    case 'READY_CAPACITY':
      return <CapacityFacts d={d} />
    case 'PROVIDER_QUOTA_EXHAUSTED':
    case 'PROVIDER_COOLDOWN':
    case 'PROVIDER_OUTAGE':
      return <ProviderFacts d={d} now={now} />
    case 'SCHEDULED_RETRY':
      return <RetryFacts d={d} now={now} />
    case 'DEPENDENCY_INCOMPLETE':
      return (
        <Fact k="waiting-on" label="Upstream steps">
          <Related rows={d.waiting_on} label={(r) => str(r.step_id) ?? str(r.task_id) ?? 'a task'} />
        </Fact>
      )
    case 'CHILDREN_INCOMPLETE':
      return (
        <>
          <Fact k="unfinished" label="Unfinished">
            <Num value={d.unfinished} />
            {' of '}
            <Num value={d.total} />
          </Fact>
          <Fact k="children" label="Children">
            <Related rows={d.children} label={(r) => str(r.task_id) ?? 'a task'} />
          </Fact>
        </>
      )
    case 'HUMAN_REQUIRED':
      if (d.park_phase === 'owner_questions') return <OwnerQuestionFacts d={d} />
      return <PersonFacts reason={w.reason} d={d} now={now} />
    case 'AWAITING_INPUT':
      return <PersonFacts reason={w.reason} d={d} now={now} />
    case 'CREDENTIAL_MISSING': {
      // NAMES ONLY: the API builds `secret_names` from an allow-list and never
      // serves a value; this draws the names and nothing else.
      const secrets = names(d.secret_names)
      return (
        <Fact k="secrets" label="Missing secret">
          {secrets === null || secrets.length === 0 ? <NotRecorded /> : secrets.join(', ')}
        </Fact>
      )
    }
    case 'BUDGET_EXHAUSTED':
      return (
        <Fact k="budget" label="Budget">
          <Text value={d.budget} />
        </Fact>
      )
    case 'MANUAL_PAUSE':
      return (
        <>
          <Fact k="paused-by" label="Paused by">
            <Text value={d.paused_by} />
          </Fact>
          <Fact k="paused-at" label="Paused at">
            <When iso={d.paused_at} now={now} />
          </Fact>
          {str(d.error) !== null && (
            <Fact k="note" label="Recorded note">
              {str(d.error)}
            </Fact>
          )}
        </>
      )
    default:
      return null
  }
}

/** The history, newest last; past `HISTORY_SHOWN` the oldest fold behind "show all N". */
function WaitHistory({ w, now }: { w: Waiting; now: number }) {
  const [open, setOpen] = useState(false)
  const all: WaitingHistoryEntry[] = Array.isArray(w.history) ? w.history : []
  const folded = !open && all.length > HISTORY_SHOWN
  const rows = folded ? all.slice(-HISTORY_SHOWN) : all
  return (
    <div className="wp-history" data-history="">
      <div className="wp-history-head">
        <b>Wait history</b>
        {all.length > HISTORY_SHOWN && (
          <button type="button" className="dt-more" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
            {open ? `show the last ${HISTORY_SHOWN}` : `show all ${all.length}`}
          </button>
        )}
      </div>
      {w.history_read === false ? (
        <p className="dt-note">History not read: the route read no events for this agent.</p>
      ) : all.length === 0 ? (
        <p className="dt-note">No park, wake or re-check in the events read.</p>
      ) : (
        <ol className="wp-hist">
          {rows.map((h, i) => {
            const at = clockTime(h.at, now)
            return (
              <li key={`${h.at}-${i}`} data-kind={h.kind}>
                {at === null ? <NotRecorded /> : <time dateTime={h.at} title={at.title}>{at.text}</time>}
                <span className="wp-hist-t">{h.text}</span>
              </li>
            )
          })}
        </ol>
      )}
    </div>
  )
}

/**
 * The panel, for any task that carries the full `waiting` object; nothing for
 * one that does not (another state, or an API older than WAIT-API).
 */
export function WaitPanel({ task, now }: { task: Task; now: number }) {
  const w = fullWaiting(task)
  if (w === null) return null
  const since = str(w.since) !== null ? Date.parse(w.since!) : NaN
  const sinceAt = clockTime(str(w.since), now)
  return (
    <section
      className={`dt-card wp${NEEDS_A_PERSON.has(w.reason) ? ' is-warn' : ''}`}
      data-wait-panel=""
      data-reason={w.reason}
      aria-label="Why it's waiting"
    >
      <div className="dt-card-head">
        {/* AN h2, AS EVERY DETAILS CARD'S TITLE IS (DtCardHead), so the pane's
            headings outline lists it; `.dt-card-head > h2` draws it as the `<b>` was. */}
        <h2>Why it&rsquo;s waiting</h2>
        <span className="dt-card-r">
          <code className="dt-chip" title="The reason the platform recorded">
            {w.reason}
          </code>
          <HelpCard topic="blockers-at-an-instant" />
        </span>
      </div>
      <p className="wp-summary" data-wait-summary="">
        {w.summary}
      </p>
      <p className="dt-note" data-wait-since="">
        {Number.isFinite(since) && sinceAt !== null ? (
          <span title={`since ${sinceAt.title}`}>{`waited ${formatDuration(now - since)} · since ${sinceAt.text}`}</span>
        ) : (
          'waited: not recorded'
        )}
      </p>
      <dl className="wp-facts">
        <Fact k="resumes" label="What resumes it">
          <Text value={w.resumes_when} />
        </Fact>
        <Fact k="next-check" label="Next check">
          <When iso={w.next_check_at} now={now} />
        </Fact>
        <DetailFacts w={w} now={now} />
      </dl>
      <WaitHistory w={w} now={now} />
    </section>
  )
}
