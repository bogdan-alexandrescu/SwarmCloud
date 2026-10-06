import { useEffect, useId, useRef, useState, type ReactNode } from 'react'

import { shortTaskId } from './agentlist'
import { loadOutcomes } from './api'
import type { RowLink } from './Ledger'
import type { ApiError, Result } from './fetch'
import {
  bucketName,
  instantLabel,
  rowsQuery,
  unreadWords,
  type EndedRow,
  type EndedRows,
  type GroupBy,
  type LedgerBucketSize,
  type LedgerView,
  type Outcomes,
  type RowOutcome,
} from './outcomes'
import { Mark } from './primitives'

/**
 * THE TASKS BEHIND ONE TIMELINE FIGURE (#116).
 *
 * "Failed 21" on a day used to open the Agents list's Recent tab, which reads
 * its own newest page whatever the span -- so the 21 were somewhere in it, or
 * not. This card lists exactly the 21: `GET /v1/outcomes?rows=failed&rows_at=…`
 * serves them from the same fold the figure was counted from, placed by the
 * same `completed_at`, under the same filters. The card says that window in
 * its head, and the count it prints is the route's `total`, never the length
 * of a capped list.
 */

type RowsPayload = Pick<Outcomes, 'tz' | 'bucket' | 'vocab' | 'scope'> & { ended_rows?: EndedRows }

const OUTCOME_WORD: Readonly<Record<RowOutcome, string>> = {
  failed: 'failed',
  cancelled: 'cancelled',
  succeeded: 'succeeded',
}

const GROUP_WORD: Readonly<Record<GroupBy, string>> = {
  runner_profile: 'profile',
  submitted_by: 'submitter',
  tenant_id: 'tenant',
}

/**
 * A link that opens in place, through the page's own view, and leaves a
 * modified click (a new tab) to the browser, which reads the same href.
 */
export function OpenLink({ link, className, children }: { link: RowLink; className?: string; children: ReactNode }) {
  return (
    <a
      className={className}
      href={link.href}
      onClick={(e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return
        e.preventDefault()
        link.open()
      }}
    >
      {children}
    </a>
  )
}

/** The task's address: its drawer, under the explicit `task/` form. */
export function taskHref(id: string): string {
  return `#work/task/${encodeURIComponent(id)}`
}

/** Where the figure was, in words: one bucket, or the span. */
export function rowsWhere(rows: EndedRows, bucket: LedgerBucketSize, tz: string): string {
  return rows.at === null ? 'in this span' : bucketName(rows.at, bucket, tz)
}

/** The card's head: the figure, exactly as the route counted it. */
export function rowsTitle(rows: EndedRows, bucket: LedgerBucketSize, tz: string): string {
  const who = rows.key === null ? '' : ` · ${rows.key === '' ? 'no submitter recorded' : rows.key}`
  return `${rows.total} ${OUTCOME_WORD[rows.outcome]} · ${rowsWhere(rows, bucket, tz)}${who}`
}

/** Why a read failed, in the card's own words: a bucket no longer in the span is the common one. */
function failedWords(e: ApiError): string {
  const detail = (e.detail ?? {}) as { reason?: unknown }
  if (e.kind === 'invalid' && detail.reason === 'not_a_bucket') {
    return 'that bucket is not in this span any more; open the figure again from the drawing'
  }
  return e.message
}

function why(row: EndedRow, data: RowsPayload): string {
  if (row.failure_class !== null) {
    return data.vocab.failure_classes.find((c) => c.key === row.failure_class)?.label ?? row.failure_class
  }
  if (row.cancel_cause !== null) {
    return data.vocab.cancel_causes.find((c) => c.key === row.cancel_cause)?.label ?? row.cancel_cause
  }
  return ''
}

/** The Timeline's card frame (`ctl-card ol-card`), with the close button in its head. */
function Frame({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const id = useId()
  const box = useRef<HTMLElement>(null)
  // Opened from a Reliability cell far down the page, the card is above the
  // reader: bring it to them.
  useEffect(() => {
    box.current?.scrollIntoView?.({ block: 'nearest' })
  }, [])
  return (
    <section ref={box} className="ctl-card ol-card is-wide ol-rows" aria-labelledby={`${id}-t`}>
      <div className="ctl-card-head">
        <h2 className="ctl-card-title" id={`${id}-t`}>
          {title}
        </h2>
        <span className="ctl-card-note">
          <button type="button" className="c-link is-sm ol-rows-close" onClick={onClose}>
            close
          </button>
        </span>
      </div>
      <div className="ctl-card-body">{children}</div>
    </section>
  )
}

export function EndedRowsCard({
  view,
  tz,
  nonce,
  onClose,
}: {
  view: LedgerView
  tz: string
  /** The page's refresh count: a refresh re-reads the rows too. */
  nonce: number
  onClose: () => void
}) {
  const q = rowsQuery(view, tz)
  const key = q === null ? null : q.toString()
  const [got, setGot] = useState<{ key: string; result: Result<RowsPayload> } | null>(null)
  useEffect(() => {
    if (key === null) return
    let live = true
    const read = loadOutcomes(new URLSearchParams(key), ['totals']) as Promise<Result<RowsPayload>>
    read.then((r) => {
      if (live) setGot({ key, result: r })
    })
    return () => {
      live = false
    }
  }, [key, nonce])

  if (key === null) return null
  const result = got !== null && got.key === key ? got.result : null
  if (result === null || result.status === 'loading') {
    return (
      <Frame title="The tasks behind this figure" onClose={onClose}>
        <p className="ol-rows-note" aria-busy="true">
          <Mark kind="pending" say="The tasks behind this figure are still being read." /> reading…
        </p>
      </Frame>
    )
  }
  if (result.status === 'error' || result.status === 'empty' || result.data.ended_rows === undefined) {
    const words = result.status === 'error' ? failedWords(result.error) : 'the route served no rows block'
    return (
      <Frame title="The tasks behind this figure" onClose={onClose}>
        <p className="ol-rows-note" role="status">
          <Mark kind="unread" say={`The tasks could not be read: ${words}.`} /> {words}
        </p>
      </Frame>
    )
  }
  const data = result.data
  const rows = data.ended_rows!
  const platform = data.scope.kind === 'platform'
  const where = rowsWhere(rows, data.bucket, data.tz)
  return (
    <Frame title={rowsTitle(rows, data.bucket, data.tz)} onClose={onClose}>
      {/* THE WINDOW, STATED: these are the figure's own tasks -- every task
          that ended so, placed by completed_at, under the page's filters --
          and not a page of the Agents list. */}
      <p className="ol-rows-note">
        every task that ended {OUTCOME_WORD[rows.outcome]} {rows.at === null ? 'in this span' : `in ${where}`}, by{' '}
        <code>completed_at</code>, under these filters
        {rows.key !== null && ` · ${GROUP_WORD[rows.group]} ${rows.key === '' ? 'not recorded' : rows.key}`}
        {rows.rows.length < rows.total && ` · newest ${rows.rows.length} of ${rows.total} listed`}
      </p>
      {rows.unread_reason !== null ? (
        <p className="ol-rows-note" role="status">
          <Mark kind="unread" say={`This bucket was not read: ${unreadWords(rows.unread_reason)}.`} />{' '}
          this bucket was not read ({unreadWords(rows.unread_reason)}), so no task in it can be listed
        </p>
      ) : rows.total === 0 ? (
        <p className="ol-rows-note">none</p>
      ) : (
        <div className="ctl-table is-scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">Agent</th>
                {platform && <th scope="col">Tenant</th>}
                <th scope="col">Profile</th>
                <th scope="col">Submitted by</th>
                <th scope="col">Ended</th>
                <th scope="col">Why</th>
              </tr>
            </thead>
            <tbody>
              {rows.rows.map((r) => (
                <tr key={`${r.tenant_id}/${r.id}`} data-id={r.id}>
                  <th scope="row">
                    <a className="ctl-link mono" href={taskHref(r.id)} title={r.id}>
                      {r.step_id ?? shortTaskId(r.id)}
                    </a>
                    {r.state === 'DEAD_LETTERED' && <span className="ol-q"> · dead-lettered</span>}
                  </th>
                  {platform && <td>{r.tenant_id}</td>}
                  <td>{r.runner_profile ?? ''}</td>
                  <td>{r.submitted_by === '' ? <span className="ol-q">not recorded</span> : r.submitted_by}</td>
                  <td>{instantLabel(r.completed_at, data.tz)}</td>
                  <td>{why(r, data)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Frame>
  )
}
