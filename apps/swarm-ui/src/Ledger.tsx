// THE TIMELINE'S TABLE TWIN AND ITS EIGHT CARDS (#185).
//
// Every card but one draws from the same `GET /v1/outcomes` payload the ledger
// does, on the same completed_at basis and under the same filters, so a card
// and the chart above it cannot describe two different sets of work. The
// exception is "Not finished yet": open work has no completed_at, so it is
// read live from `GET /v1/stats` and the parked list, and its card-note says
// the span does not apply to it.
//
// THE HONESTY RULES, as each card carries them:
//
//   * a count the server measured is a digit, and a zero is drawn as the
//     track's real-zero tick -- never an empty bar;
//   * a figure nobody reported is the em dash or a phrase, never `0` or
//     `$0.00`; a reported zero cost IS `$0.00`;
//   * a sum over part of its attempts carries the partial mark and says
//     `k of n`;
//   * a list cut short says `showing 20 of N` in a caption with the total;
//   * a row whose parts could not be read is kept, with the not-read mark,
//     rather than dropped.
//
// No card restates the contract's arithmetic. The failure classes arrive in
// the server's fixed order (`vocab`), with its labels; this file never sorts
// them, so a filter can never re-rank them.

import { useId, type ReactNode } from 'react'

import { HelpNote } from './HelpCard'
import { type TopicId } from './help'
import {
  LOW_N,
  bucketName,
  interval,
  money,
  pct,
  secs,
  unreadWords,
  type CostCell,
  type GroupBy,
  type GroupRow,
  type LatencyOutcome,
  type Outcomes,
  type Stat,
} from './outcomes'
import { Absent, Mark, UtilTrack } from './primitives'
import { HatchDef, useHatchId } from './charts/parts'
import {
  CONCURRENCY_STATES,
  PARK_NEEDS_A_PERSON,
  pluralise,
  type Stats,
  type TaskPage,
} from './types'

/** Mini strips are drawn only while the chart has this many buckets or fewer; past it a 6px column is texture. */
export const STRIP_MAX_BUCKETS = 60

/** Where the Agents list shows failures: the closest drill-through the contract allows. */
const FAILED_HREF = '#work/running/recent/failed'
const WAITING_HREF = '#work/running/waiting'

// ---------------------------------------------------------------------------
// The card shell
// ---------------------------------------------------------------------------

function Card({
  title,
  note,
  explain,
  className,
  foot,
  children,
}: {
  title: string
  note?: ReactNode
  explain?: TopicId | undefined
  className?: string
  foot?: ReactNode
  children: ReactNode
}) {
  const id = useId()
  return (
    <section className={`ctl-card ol-card${className ? ` ${className}` : ''}`} aria-labelledby={`${id}-t`}>
      <div className="ctl-card-head">
        <h2 className="ctl-card-title" id={`${id}-t`} aria-describedby={explain === undefined ? undefined : `${id}-d`}>
          {title}
          {explain !== undefined && <HelpNote topic={explain} id={`${id}-d`} />}
        </h2>
        {note !== undefined && <span className="ctl-card-note">{note}</span>}
      </div>
      <div className="ctl-card-body">{children}</div>
      {foot !== undefined && <p className="ctl-card-foot">{foot}</p>}
    </section>
  )
}

/** A row of a card's proportion list: a name, the grey track, the count, and an optional strip. */
function Row({
  name,
  count,
  of,
  strip,
  href,
}: {
  name: string
  count: number
  of: number
  strip?: ReactNode
  href?: string | undefined
}) {
  // A MEASURED ZERO IS THE TRACK'S TICK (§8.6), not an empty bar: the row is
  // drawn, the tick says the scale starts here and the value is at its start.
  const pctOf = count === 0 ? 0 : of === 0 ? 0 : (count / of) * 100
  const label = href === undefined ? <span className="ol-row-name">{name}</span> : (
    <a className="ctl-link ol-row-name" href={href}>
      {name}
    </a>
  )
  return (
    <div className="ol-row" data-row={name}>
      {label}
      <UtilTrack pct={pctOf} zeroTitle={`${name}: a measured zero`} />
      <span className="ol-row-n">{count}</span>
      {strip}
    </div>
  )
}

/**
 * One column per bucket, on the chart's own buckets, highlighting the pick. No
 * text inside, so it is drawn at one width without scaling any type; hidden
 * below 900px by the sheet, and not drawn at all past `STRIP_MAX_BUCKETS`.
 */
function Strip({
  values,
  pickedAt,
  label,
}: {
  values: ReadonlyArray<number | null>
  pickedAt: number
  label: string
}) {
  const hatch = useHatchId()
  const max = Math.max(0, ...values.map((v) => v ?? 0))
  const col = 6
  const h = 16
  const w = values.length * col
  return (
    <svg className="ol-strip" width={w} height={h} viewBox={`0 0 ${w} ${h}`} role="img" aria-label={label}>
      <HatchDef id={hatch} />
      {pickedAt >= 0 && <rect className="ol-sel" x={pickedAt * col} y={0} width={col} height={h} />}
      {values.map((v, i) =>
        v === null ? (
          <rect key={i} className="ol-unread" x={i * col} y={0} width={col} height={h} fill={`url(#${hatch})`} />
        ) : v === 0 ? (
          <line key={i} className="ol-zero" x1={i * col + 1} x2={i * col + col - 1} y1={h - 1} y2={h - 1} />
        ) : (
          <rect key={i} className="ol-strip-col" x={i * col + 1} y={h - Math.max(2, (v / max) * h)} width={col - 2} height={Math.max(2, (v / max) * h)} />
        ),
      )}
    </svg>
  )
}

/** The picked bucket's index, for the strips. */
function pickedIndex(data: Outcomes, picked: string | null): number {
  return picked === null ? -1 : data.buckets.findIndex((b) => b.start === picked)
}

// ---------------------------------------------------------------------------
// The Table twin
// ---------------------------------------------------------------------------

/**
 * THE SAME BUCKETS AS A TABLE: the keyboard and screen-reader route to every
 * value, and the Table toggle's other half. A data table, so below 900px it
 * scrolls with the bucket column held (CH-13).
 */
export function LedgerTable({ data }: { data: Outcomes }) {
  const { bucket, tz } = data
  return (
    <div className="ctl-table is-scroll ol-table">
      <table>
        <thead>
          <tr>
            <th scope="col">{bucket === 'hour' ? 'Hour' : bucket === 'day' ? 'Day' : bucket === 'week' ? 'Week' : 'Month'}</th>
            <th scope="col" className="is-num">Succeeded</th>
            <th scope="col" className="is-num">Failed</th>
            <th scope="col" className="is-num">Dead-lettered</th>
            <th scope="col" className="is-num">Rate · k of n · 95 %</th>
            <th scope="col" className="is-num">Cancelled · requested / after a failure</th>
            <th scope="col" className="is-num">Submitted</th>
            <th scope="col" className="is-num">Finished</th>
          </tr>
        </thead>
        <tbody>
          {data.buckets.map((b) => (
            <tr key={b.start} data-state={b.state}>
              <th scope="row">
                {bucketName(b.start, bucket, tz)}
                {b.in_progress && <span className="ctl-sub"> so far</span>}
              </th>
              {b.state === 'unread' ? (
                <td colSpan={7}>
                  <Mark
                    kind="unread"
                    say={`${bucketName(b.start, bucket, tz)} was not read: ${unreadWords(b.unread_reason)}. No count is shown for it.`}
                  />{' '}
                  {unreadWords(b.unread_reason)}
                </td>
              ) : (
                <>
                  <td className="is-num">{b.succeeded}</td>
                  <td className="is-num">{b.failed}</td>
                  <td className="is-num">{b.dead_lettered}</td>
                  <td className="is-num">
                    {b.rate === null ? (
                      <span className="ol-phrase">nothing decided</span>
                    ) : (
                      `${pct(b.rate.p)} · ${b.rate.k} of ${b.rate.n} · ${interval(b.rate)}`
                    )}
                  </td>
                  <td className="is-num">
                    {b.cancelled?.total} · {b.cancelled?.requested} / {(b.cancelled?.after_failure ?? 0) + (b.cancelled?.workflow_sweep ?? 0)}
                  </td>
                  <td className="is-num">{b.submitted}</td>
                  <td className="is-num">{b.ended}</td>
                </>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ---------------------------------------------------------------------------
// 1. Why tasks failed
// ---------------------------------------------------------------------------

export function FailureClassesCard({ data, picked }: { data: Outcomes; picked: string | null }) {
  const t = data.totals.failure_classes
  const total = data.vocab.failure_classes.reduce((n, c) => n + (t[c.key] ?? 0), 0)
  const max = Math.max(0, ...data.vocab.failure_classes.map((c) => t[c.key] ?? 0))
  const strips = data.buckets.length <= STRIP_MAX_BUCKETS
  const at = pickedIndex(data, picked)
  return (
    <Card
      title="Why tasks failed"
      note={`${total} · by class`}
      explain="failure-classes"
      className="ol-failures"
      foot={<span>exit code, then last_error</span>}
    >
      {/* THE ORDER IS THE SERVER'S AND IT IS FIXED: runner error, timeout,
          lost worker, could not start, outputs missing, dispatch failed,
          other, no reason recorded. A filter never re-ranks it, and `other`
          and `no reason` are always drawn, because the class is matched from
          free text and an unmatched failure is still a failure. */}
      {data.vocab.failure_classes.map((c) => (
        <Row
          key={c.key}
          name={c.label}
          count={t[c.key] ?? 0}
          of={max}
          href={FAILED_HREF}
          strip={
            strips ? (
              <Strip
                label={`${c.label}, by ${data.bucket}`}
                pickedAt={at}
                values={data.buckets.map((b) => (b.state === 'unread' ? null : (b.failure_classes?.[c.key] ?? 0)))}
              />
            ) : undefined
          }
        />
      ))}
    </Card>
  )
}

// ---------------------------------------------------------------------------
// 2. Workflows that failed, and where
// ---------------------------------------------------------------------------

function shortId(id: string): string {
  return id.length > 14 ? `${id.slice(0, 7)}…${id.slice(-4)}` : id
}

export function WorkflowsFailedCard({
  data,
  spanLabel,
}: {
  data: Outcomes
  spanLabel: string
}) {
  const w = data.workflows_failed
  const classLabel = new Map(data.vocab.failure_classes.map((c) => [c.key, c.label]))
  if (!w.applicable) {
    return (
      <Card title="Workflows that failed, and where" className="is-wide ol-workflows">
        <Absent
          kind="zero"
          heading="standalone tasks only"
          say="The kind filter is set to standalone tasks, so no workflow step is in this view."
        />
      </Card>
    )
  }
  return (
    <Card
      title="Workflows that failed, and where"
      note={`${w.with_failed_steps} of ${w.with_ended_steps} with steps ending in ${spanLabel}`}
      className="is-wide ol-workflows"
      foot={
        w.failing_steps.length > 0 ? (
          <span>most-failing steps: {w.failing_steps.map((s) => `${s.step_id} ${s.n}`).join(' · ')}</span>
        ) : undefined
      }
    >
      {w.rows.length === 0 ? (
        <Absent
          kind="zero"
          heading="No workflow step failed in this span"
          say="A real zero: every workflow step that ended in this span ended some other way."
        />
      ) : (
        <div className="ctl-table is-scroll">
          <table>
            {w.rows_total > w.rows.length && <caption>showing {w.rows.length} of {w.rows_total}</caption>}
            <thead>
              <tr>
                <th scope="col">Workflow</th>
                <th scope="col">Submitted by</th>
                <th scope="col">Steps</th>
                <th scope="col">Failed at</th>
                <th scope="col">Why</th>
                <th scope="col" className="is-num">Cancelled after</th>
                <th scope="col">Ended</th>
              </tr>
            </thead>
            <tbody>
              {w.rows.map((r) => (
                <tr key={r.workflow_id} className="is-bad">
                  <th scope="row">
                    <span className="mono" title={r.workflow_id}>
                      {shortId(r.workflow_id)}
                    </span>
                  </th>
                  <td>{r.submitted_by ?? <i className="ctl-em">—</i>}</td>
                  <td>
                    <Steps row={r} />
                  </td>
                  <td>
                    {r.first_failed === null ? (
                      <i className="ctl-em">—</i>
                    ) : (
                      <a className="ctl-link mono" href={`#work/task/${encodeURIComponent(r.first_failed.task_id)}`}>
                        {r.first_failed.step_id}
                      </a>
                    )}
                  </td>
                  <td>{r.first_failed === null ? <i className="ctl-em">—</i> : (classLabel.get(r.first_failed.failure_class) ?? r.first_failed.failure_class)}</td>
                  <td className="is-num">{r.cascade_cancelled}</td>
                  <td className="mono">{new Date(r.last_ended_at).toLocaleString(undefined, { timeZone: data.tz, day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}

/** WF-1's outcome composition in the 8px track, or the not-read mark when the steps could not be read. */
function Steps({ row }: { row: Outcomes['workflows_failed']['rows'][number] }) {
  const s = row.steps
  if (s === null || row.state === 'UNKNOWN') {
    return (
      <span className="ol-steps">
        <Mark
          kind="unread"
          say={`The steps of ${row.workflow_id} could not be read within this view's read budget, so its composition is not drawn. The workflow is kept in the list.`}
        />{' '}
        steps not read
      </span>
    )
  }
  const segs: Array<[string, number]> = [
    ['succeeded', s.succeeded],
    ['failed', s.failed],
    ['dead-lettered', s.dead_lettered],
    ['cancelled', s.cancelled],
  ]
  const text = `${s.succeeded} of ${s.total} ok${s.open > 0 ? ` · ${s.open} open` : ''}${s.unreadable > 0 ? ` · ${s.unreadable} not read` : ''}`
  return (
    <span className="ol-steps">
      <span
        className="ctl-track wf-meter"
        role="img"
        aria-label={`${s.total} steps, all of them including any outside this span: ${s.succeeded} succeeded, ${s.failed} failed, ${s.dead_lettered} dead-lettered, ${s.cancelled} cancelled, ${s.open} still open, ${s.unreadable} not read`}
      >
        {segs
          .filter(([, n]) => n > 0)
          .map(([cls, n]) => (
            <i key={cls} className={`wf-seg ${cls}`} style={{ width: `${+((n / s.total) * 100).toFixed(4)}%` }} />
          ))}
      </span>{' '}
      <span className="ol-q">{text}</span>
    </span>
  )
}

// ---------------------------------------------------------------------------
// 3. Retries and attempts
// ---------------------------------------------------------------------------

const TRY_LABEL: Record<string, string> = { '0': 'never ran', '1': '1 try', '2': '2 tries', '3+': '3 or more' }

export function RetriesCard({ data }: { data: Outcomes }) {
  const r = data.retries
  const max = Math.max(0, ...r.tries.map((t) => t.tasks))
  return (
    <Card title="Retries and attempts" note="admissions, not runs" className="ol-retries">
      <p className="ol-sub">
        needed a retry · {r.needed_retry.k} of {r.needed_retry.of} that ran
        {r.needed_retry.of > 0 && ` (${Math.round((r.needed_retry.k / r.needed_retry.of) * 100)} %)`}
      </p>
      {r.tries.map((t) => (
        <div className="ol-row" key={t.attempts} data-row={TRY_LABEL[t.attempts] ?? t.attempts}>
          <span className="ol-row-name">{TRY_LABEL[t.attempts] ?? `${t.attempts} tries`}</span>
          <Outcome4
            parts={[t.succeeded, t.failed + t.dead_lettered, t.cancelled]}
            width={max === 0 ? 0 : (t.tasks / max) * 100}
            say={`${t.tasks} tasks: ${t.succeeded} succeeded, ${t.failed + t.dead_lettered} failed, ${t.cancelled} cancelled`}
          />
          <span className="ol-row-n">{t.tasks}</span>
        </div>
      ))}
      <p className="ol-line">
        {r.rescued} rescued by a retry · {r.failed_after_retry} failed after a retry
      </p>
      <p className="ol-line">
        did not end their task {r.not_final.attempts}
        {r.not_final.by_exit.length > 0 && ': '}
        {r.not_final.by_exit.map((e, i) => (
          <span key={`${e.exit_code ?? 'none'}`}>
            {i > 0 && ' · '}
            {/* A NULL EXIT IS ITS OWN ROW, NEVER A ZERO: the server's label says so. */}
            {e.label} {e.n}
          </span>
        ))}
      </p>
      {r.admissions_without_attempt_doc > 0 && (
        <p className="ol-line">
          <Mark
            kind="partial"
            say={`${r.admissions_without_attempt_doc} admissions have no attempt document, so their exits are not in the line above.`}
          />{' '}
          {r.admissions_without_attempt_doc} admissions with no attempt document
        </p>
      )}
    </Card>
  )
}

/**
 * An 8px track split by final outcome in TS-4's forms -- succeeded solid
 * `--ok`, failed solid `--bad` with the cut, cancelled the flat bars -- the
 * form the ledger draws, so a row here and a column there read the same way.
 */
function Outcome4({ parts, width, say }: { parts: [number, number, number]; width: number; say: string }) {
  const total = parts[0] + parts[1] + parts[2]
  const cls = ['succeeded', 'failed', 'cancelled'] as const
  if (total === 0) {
    return <UtilTrack pct={0} zeroTitle={say} />
  }
  return (
    <span className="ol-meter-wrap">
      <span className="ctl-track ol-meter" role="img" aria-label={say} style={{ width: `${Math.max(4, width)}%` }}>
        {parts.map((n, i) =>
          n > 0 ? <i key={cls[i]} className={`ol-seg ${cls[i]}`} style={{ width: `${+((n / total) * 100).toFixed(4)}%` }} /> : null,
        )}
      </span>
    </span>
  )
}

// ---------------------------------------------------------------------------
// 4. Time to result, by profile
// ---------------------------------------------------------------------------

/** The shared log axis, 1 s to 1 d. */
const LOG_MAX_S = 86_400
const TICKS: Array<[number, string]> = [
  [1, '1s'],
  [60, '1m'],
  [600, '10m'],
  [3600, '1h'],
  [LOG_MAX_S, '1d'],
]
const logX = (s: number) => (Math.log10(Math.max(1, Math.min(LOG_MAX_S, s))) / Math.log10(LOG_MAX_S)) * 100

/** Under this many results the upper figure is the max: a p95 of 12 values is one value with a new name. */
const P95_FROM = 20

function statText(label: string, s: Stat | null): string {
  if (s === null) return `${label} —`
  if (s.values_s !== null && s.n < LOW_N) return `${label} ${s.values_s.map(secs).join(', ')}`
  return `${label} p50 ${secs(s.p50_s)} · ${s.n < P95_FROM ? `max ${secs(s.max_s)}` : `p95 ${secs(s.p95_s)}`}`
}

function StatMarks({ s, kind }: { s: Stat | null; kind: 'wait' | 'run' }) {
  if (s === null) return null
  const hi = s.n < P95_FROM ? s.max_s : s.p95_s
  return (
    <>
      <i className={`ol-lat-span is-${kind}`} style={{ left: `${logX(s.p50_s)}%`, width: `${Math.max(0, logX(hi) - logX(s.p50_s))}%` }} />
      <i className={`ol-lat-dot is-${kind}`} style={{ left: `${logX(s.p50_s)}%` }} />
      <i className={`ol-lat-dot is-${kind} is-hollow`} style={{ left: `${logX(hi)}%` }} />
    </>
  )
}

function LatencyLine({ label, o, timeout }: { label: string; o: LatencyOutcome; timeout: number | null }) {
  return (
    <div className="ol-lat-line" data-outcome={label}>
      <span className="ol-lat-name">
        {label} <span className="ol-q">{o.n}</span>
      </span>
      {o.n === 0 ? (
        <span className="ol-lat-none">
          <i className="ctl-em">—</i>
        </span>
      ) : (
        <span className="ol-lat-axis" role="img" aria-label={`${label}: ${statText('wait', o.wait)}; ${statText('run', o.run)}`}>
          {timeout !== null && <i className="ol-lat-timeout" style={{ left: `${logX(timeout)}%` }} />}
          <StatMarks s={o.wait} kind="wait" />
          <StatMarks s={o.run} kind="run" />
        </span>
      )}
      {o.n > 0 && (
        <span className="ol-lat-text">
          {statText('wait', o.wait)} · {statText('run', o.run)}
        </span>
      )}
    </div>
  )
}

export function LatencyCard({ data }: { data: Outcomes }) {
  const rows = data.latency.by_profile
  return (
    <Card title="Time to result, by profile" note="wait, then run · p50 ● p95 ○" className="ol-latency">
      {rows.length === 0 ? (
        <Absent kind="zero" heading="Nothing finished in this span" say="A real zero: no task that was not cancelled ended in this span." />
      ) : (
        <>
          <div className="ol-lat-scale" aria-hidden>
            {TICKS.map(([s, l]) => (
              <span key={l} style={{ left: `${logX(s)}%` }}>
                {l}
              </span>
            ))}
          </div>
          {/* NO ALL-PROFILES ROW: mock's seconds and claude-code's minutes in one
              percentile describe neither. */}
          {rows.map((r) => (
            <div className="ol-lat-row" key={r.runner_profile} data-profile={r.runner_profile}>
              <p className="ol-lat-head">
                <b>{r.runner_profile}</b>{' '}
                <span className="ol-q">
                  {r.timeout_s === null ? 'timeout varies' : `timeout ${secs(r.timeout_s)}`}
                </span>
              </p>
              <LatencyLine label="succeeded" o={r.succeeded} timeout={r.timeout_s} />
              <LatencyLine label="failed" o={r.failed} timeout={r.timeout_s} />
            </div>
          ))}
          {data.coverage.wait_excluded > 0 && (
            <p className="ol-line">
              <Mark
                kind="partial"
                say={`${data.coverage.wait_excluded} workflow steps have no wait figure: a parent step could not be read, so when they became eligible is not known.`}
              />{' '}
              wait left out for {pluralise(data.coverage.wait_excluded, 'step')}
            </p>
          )}
        </>
      )}
    </Card>
  )
}

// ---------------------------------------------------------------------------
// 5. Reliability by profile, tenant or person
// ---------------------------------------------------------------------------

const GROUP_LABEL: Record<GroupBy, string> = {
  runner_profile: 'runner profile',
  tenant_id: 'tenant',
  submitted_by: 'person',
}

const GROUP_HEAD: Record<GroupBy, string> = {
  runner_profile: 'Profile',
  tenant_id: 'Tenant',
  submitted_by: 'Person',
}

/** A cost cell: `—` when nothing reported, `$0.00` only for a reported zero, partial when some attempts did not report. */
export function CostFigure({ c, what }: { c: CostCell; what: string }) {
  if (c.sum_usd === null || c.reporting === 0) {
    return (
      <span className="ol-cost is-absent">
        <i className="ctl-em">—</i> <span className="ol-q">0 of {c.attempts}</span>
      </span>
    )
  }
  return (
    <span className="ol-cost">
      {money(c.sum_usd)} <span className="ol-q">{c.reporting} of {c.attempts}</span>
      {c.reporting < c.attempts && (
        <>
          {' '}
          <Mark
            kind="partial"
            say={`${what}: ${c.reporting} of ${c.attempts} attempts reported a cost, so this sum is a floor.`}
          />
        </>
      )}
    </span>
  )
}

/** `5 profiles`, `3 tenants`, `1 person`, `4 people`. */
function rowsWord(by: GroupBy, n: number): string {
  if (by === 'submitted_by') return `${n} ${n === 1 ? 'person' : 'people'}`
  return pluralise(n, by === 'tenant_id' ? 'tenant' : 'profile')
}

function RateCell({ row }: { row: GroupRow }) {
  if (row.rate === null) {
    return <span className="ol-phrase">{row.ended > 0 ? '— all cancelled' : '— nothing ended'}</span>
  }
  return (
    <span className="ol-rate-cell">
      {row.rate.n < LOW_N && (
        <i className="ol-ring" role="img" aria-label={`only ${row.rate.n} decided: too few for the rate to mean much`} />
      )}
      {pct(row.rate.p)} <span className="ol-q">{row.rate.k} of {row.rate.n}</span>
    </span>
  )
}

/** The row's rate by bucket: a --series-1 line with a gap wherever nothing was decided or nothing was read. */
function Spark({ series }: { series: GroupRow['series'] }) {
  if (series === null) return <span className="ol-q">not drawn past 120 buckets</span>
  const w = 96
  const h = 20
  const step = series.length > 1 ? w / (series.length - 1) : 0
  const runs: string[] = []
  let cur: string[] = []
  series.forEach((p, i) => {
    if (p !== null && p.n > 0) cur.push(`${(i * step).toFixed(1)},${(h - 2 - (p.k / p.n) * (h - 4)).toFixed(1)}`)
    else if (cur.length > 0) {
      runs.push(cur.join(' '))
      cur = []
    }
  })
  if (cur.length > 0) runs.push(cur.join(' '))
  return (
    <svg className="ol-spark" width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden="true" focusable="false">
      {runs.map((pts, i) =>
        pts.includes(' ') ? (
          <polyline key={i} className="ol-m-rate" points={pts} />
        ) : (
          <circle key={i} className="ol-m-pt" cx={pts.split(',')[0]} cy={pts.split(',')[1]} r={1.5} />
        ),
      )}
    </svg>
  )
}

export function ReliabilityCard({
  data,
  group,
  platform,
  onGroup,
}: {
  data: Outcomes
  group: GroupBy
  platform: boolean
  onGroup: (g: GroupBy) => void
}) {
  const g = data.groups
  const choices: GroupBy[] = platform ? ['runner_profile', 'tenant_id', 'submitted_by'] : ['runner_profile', 'submitted_by']
  return (
    <Card
      title={`Reliability by ${GROUP_LABEL[g.by]}`}
      note={`${rowsWord(g.by, g.rows_total)} · ${data.buckets.length} ${data.bucket === 'hour' ? 'hours' : `${data.bucket}s`}`}
      className="is-wide ol-reliability"
    >
      <div className="ctl-seg ol-group" role="group" aria-label="Group by">
        {choices.map((c) => (
          <button key={c} type="button" aria-pressed={group === c} onClick={() => onGroup(c)}>
            {GROUP_LABEL[c]}
          </button>
        ))}
      </div>
      {g.rows.length === 0 ? (
        <Absent kind="zero" heading="Nothing ended in this span" say="A real zero: no task ended in this span under these filters." />
      ) : (
        <div className="ctl-table is-scroll">
          <table>
            {g.rows_total > g.rows.length && <caption>showing {g.rows.length} of {g.rows_total}</caption>}
            <thead>
              <tr>
                <th scope="col">{GROUP_HEAD[g.by]}</th>
                <th scope="col" className="is-num">Ended</th>
                <th scope="col">Success rate</th>
                <th scope="col" className="is-num">Failed</th>
                <th scope="col" className="is-num">Cancelled · after a failure</th>
                <th scope="col">Reported cost</th>
                <th scope="col">Rate by {data.bucket}</th>
              </tr>
            </thead>
            <tbody>
              {g.rows.map((r) => (
                <tr key={r.key} data-key={r.key}>
                  <th scope="row">
                    {r.key}
                    {r.declared_cost && (
                      <>
                        {' '}
                        <span className="ol-tag" title="this profile declares its own cost, from its input; it is test data">
                          declared
                        </span>
                      </>
                    )}
                  </th>
                  <td className="is-num">{r.ended}</td>
                  <td>
                    <RateCell row={r} />
                  </td>
                  <td className="is-num">{r.failed + r.dead_lettered}</td>
                  <td className="is-num">
                    {r.cancelled.total} · {r.cancelled.after_failure + r.cancelled.workflow_sweep}
                  </td>
                  <td>
                    <CostFigure c={r.cost} what={`${r.key}'s reported cost`} />
                  </td>
                  <td>
                    <Spark series={r.series} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}

// ---------------------------------------------------------------------------
// 6. Reported cost
// ---------------------------------------------------------------------------

function CostStrip({ data, pickedAt }: { data: Outcomes; pickedAt: number }) {
  const hatch = useHatchId()
  const n = data.buckets.length
  const col = n <= 30 ? 10 : n <= STRIP_MAX_BUCKETS ? 6 : 2
  const w = n * col
  const bars = 28
  const cell = 6
  const h = bars + 3 + cell
  const max = Math.max(0, ...data.buckets.map((b) => b.cost?.sum_usd ?? 0))
  return (
    <svg className="ol-cost-strip" width={w} height={h} viewBox={`0 0 ${w} ${h}`} role="img" aria-label={`Reported cost by ${data.bucket}, with a coverage cell under each`}>
      <HatchDef id={hatch} />
      {pickedAt >= 0 && <rect className="ol-sel" x={pickedAt * col} y={0} width={col} height={h} />}
      {data.buckets.map((b, i) => {
        const x = i * col
        if (b.state === 'unread' || b.cost === null) {
          return <rect key={b.start} className="ol-unread" x={x} y={0} width={col} height={h} fill={`url(#${hatch})`} />
        }
        const c = b.cost
        const v = c.sum_usd
        const bh = v === null || max === 0 ? 0 : Math.max(v > 0 ? 2 : 0, (v / max) * bars)
        return (
          <g key={b.start}>
            {v !== null && v > 0 && <rect className="ol-m-cost" x={x + 1} y={bars - bh} width={Math.max(1, col - 2)} height={bh} />}
            {v === 0 && <line className="ol-zero" x1={x + 1} x2={x + col - 1} y1={bars - 1} y2={bars - 1} />}
            {/* THE COVERAGE CELL: filled when every attempt reported, hatched
                when some did not, the real-zero tick when no attempt ran. */}
            {c.attempts === 0 ? (
              <line className="ol-zero" x1={x + 1} x2={x + col - 1} y1={h - 1} y2={h - 1} />
            ) : c.reporting === c.attempts ? (
              <rect className="ol-cov is-full" x={x + 1} y={bars + 3} width={Math.max(1, col - 2)} height={cell} />
            ) : (
              <rect className="ol-cov is-some" x={x + 1} y={bars + 3} width={Math.max(1, col - 2)} height={cell} fill={`url(#${hatch})`} />
            )}
          </g>
        )
      })}
    </svg>
  )
}

export function CostCard({ data, picked }: { data: Outcomes; picked: string | null }) {
  const c = data.totals.cost
  const partial = c.reporting < c.attempts
  const b = c.by_outcome
  const per = c.per_succeeded_task
  const boughtTotal = [b.succeeded, b.failed, b.dead_lettered, b.cancelled].reduce((n, x) => n + (x.sum_usd ?? 0), 0)
  return (
    <Card
      title="Reported cost"
      note={`by task end · ${c.reporting} of ${c.attempts} attempts`}
      explain="tokens-reported"
      className="ol-cost-card"
      foot={<span>Reported cost · not a bill</span>}
    >
      <p className="ol-figure-line">
        {c.sum_usd === null ? (
          <>
            <b className="ctl-figure is-absent">not reported</b>{' '}
            <Mark
              kind="absent"
              say={`No attempt of the ${c.attempts} that ran in this span reported a cost. That is an absent measurement, not a cost of zero.`}
            />
          </>
        ) : (
          <>
            <b className="ctl-figure">{money(c.sum_usd)}</b>
            {partial && (
              <>
                {' '}
                <Mark
                  kind="partial"
                  say={`Summed from the ${c.reporting} of ${c.attempts} attempts that reported a cost; the rest reported nothing, so this is a floor.`}
                />
              </>
            )}
          </>
        )}
      </p>
      <CostStrip data={data} pickedAt={pickedIndex(data, picked)} />
      {boughtTotal > 0 && (
        <p className="ol-line ol-bought">
          <span className="ol-row-name">bought</span>
          <Outcome4
            parts={[b.succeeded.sum_usd ?? 0, (b.failed.sum_usd ?? 0) + (b.dead_lettered.sum_usd ?? 0), b.cancelled.sum_usd ?? 0]}
            width={100}
            say="reported cost split by the outcome of the task that spent it"
          />
        </p>
      )}
      <p className="ol-line">
        {(
          [
            ['succeeded', b.succeeded],
            ['failed', { ...b.failed, sum_usd: b.failed.sum_usd === null && b.dead_lettered.sum_usd === null ? null : (b.failed.sum_usd ?? 0) + (b.dead_lettered.sum_usd ?? 0) }],
            ['cancelled', b.cancelled],
          ] as Array<[string, CostCell]>
        ).map(([k, v], i) => (
          <span key={k}>
            {i > 0 && ' · '}
            {k} {v.sum_usd === null ? '—' : money(v.sum_usd)}
          </span>
        ))}
      </p>
      <p className="ol-line">
        per succeeded task{' '}
        {per.n === 0 ? (
          <>— <span className="ol-q">no succeeded task fully reported</span></>
        ) : per.values_usd !== null && per.n < LOW_N ? (
          <>{per.values_usd.map(money).join(', ')}</>
        ) : (
          <>
            p50 {per.p50_usd === null ? '—' : money(per.p50_usd)} · p95 {per.p95_usd === null ? '—' : money(per.p95_usd)}
          </>
        )}{' '}
        <span className="ol-q">
          {per.n} of {per.of} fully reported
        </span>
      </p>
      <p className="ol-line">
        retries {c.retries.sum_usd === null ? '—' : money(c.retries.sum_usd)}{' '}
        <span className="ol-q">
          {c.retries.reporting} of {c.retries.attempts} attempts
        </span>
      </p>
      {c.declared.profiles.length > 0 && (
        <p className="ol-line">
          <span className="ol-tag">declared</span> {c.declared.profiles.join(', ')}{' '}
          {c.declared.sum_usd === null ? '—' : money(c.declared.sum_usd)} <span className="ol-q">included above</span>
        </p>
      )}
    </Card>
  )
}

// ---------------------------------------------------------------------------
// 7. Not finished yet
// ---------------------------------------------------------------------------

export interface OpenWork {
  /** null while reading; a failure is the Result's error. */
  stats: { status: 'ok'; data: Stats } | { status: 'error'; message: string } | null
  parked: { status: 'ok'; data: TaskPage } | { status: 'empty' } | { status: 'error'; message: string } | null
}

export function OpenWorkCard({
  open,
  platform,
  profileFiltered,
  tenant,
}: {
  open: OpenWork
  platform: boolean
  profileFiltered: boolean
  tenant: string | null
}) {
  const s = open.stats
  const counts = s?.status === 'ok' ? (platform ? s.data.platform_tasks_by_state : s.data.tasks_by_state) : undefined
  const n = (state: string) => counts?.[state] ?? 0
  const running = [...CONCURRENCY_STATES].reduce((sum, st) => sum + n(st), 0)
  const parkedTasks = open.parked?.status === 'ok' ? open.parked.data.tasks : open.parked?.status === 'empty' ? [] : null
  const reasons = new Map<string, number>()
  for (const t of parkedTasks ?? []) {
    const r = t.park_reason === null ? 'no reason recorded' : String(t.park_reason)
    reasons.set(r, (reasons.get(r) ?? 0) + 1)
  }
  const person = [...reasons.entries()].filter(([r]) => PARK_NEEDS_A_PERSON.has(r))
  const itself = [...reasons.entries()].filter(([r]) => !PARK_NEEDS_A_PERSON.has(r))
  const more = open.parked?.status === 'ok' && open.parked.data.next_page_token !== null && open.parked.data.next_page_token !== undefined
  return (
    <Card title="Not finished yet" note="now · span not applied" className="ol-open">
      {s === null ? (
        <p className="ol-line">
          <Mark kind="pending" say="The live state counts are still being read." /> reading
        </p>
      ) : s.status === 'error' || counts === undefined ? (
        <p className="ol-line">
          <Mark
            kind={s.status === 'error' ? 'unread' : 'absent'}
            say={s.status === 'error' ? `The state counts could not be read: ${s.message}` : 'The platform-wide counts are served to administrators only.'}
          />{' '}
          {s.status === 'error' ? 'state counts not read' : 'platform counts not served'}
        </p>
      ) : (
        <p className="ol-line ol-open-counts">
          {n('PARKED')} parked · {running} running · {n('QUEUED')} queued · {n('READY')} ready
          {profileFiltered && <span className="ol-q"> · not filtered by profile</span>}
        </p>
      )}
      {parkedTasks === null ? (
        open.parked?.status === 'error' ? (
          <p className="ol-line">
            <Mark kind="unread" say={`The parked list could not be read: ${open.parked.message}`} /> park reasons not read
          </p>
        ) : null
      ) : parkedTasks.length > 0 ? (
        <>
          {person.length > 0 && (
            <p className="ol-line ol-person is-warn">
              <span className="ol-warn-mark" aria-hidden>
                ▲
              </span>{' '}
              needs a person {person.map(([r, c]) => `${r} ${c}`).join(' · ')}
            </p>
          )}
          {itself.length > 0 && (
            <p className="ol-line">clears itself {itself.map(([r, c]) => `${r} ${c}`).join(' · ')}</p>
          )}
          {more && (
            <p className="ol-line">
              <Mark kind="partial" say={`Reasons are counted over the first ${parkedTasks.length} parked tasks; more exist.`} /> reasons for {parkedTasks.length} of {counts === undefined ? 'more' : n('PARKED')}
            </p>
          )}
          {platform && tenant !== null && <p className="ol-line ol-q">park reasons: tenant {tenant} only</p>}
        </>
      ) : null}
      <p className="ol-line">
        <a className="ctl-link" href={WAITING_HREF}>
          open in Agents →
        </a>
      </p>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// 8. Why tasks were cancelled
// ---------------------------------------------------------------------------

export function CancelCausesCard({ data, picked }: { data: Outcomes; picked: string | null }) {
  const t = data.totals.cancelled
  const max = Math.max(0, ...data.vocab.cancel_causes.map((c) => t[c.key]))
  const strips = data.buckets.length <= STRIP_MAX_BUCKETS
  const at = pickedIndex(data, picked)
  return (
    <Card title="Why tasks were cancelled" note={`${t.total} · by cause`} className="ol-cancels" foot={<span>a cancel is not a verdict: no hue</span>}>
      {data.vocab.cancel_causes.map((c) => (
        <Row
          key={c.key}
          name={c.label}
          count={t[c.key]}
          of={max}
          strip={
            strips ? (
              <Strip
                label={`${c.label}, by ${data.bucket}`}
                pickedAt={at}
                values={data.buckets.map((b) => (b.state === 'unread' ? null : (b.cancelled?.[c.key] ?? 0)))}
              />
            ) : undefined
          }
        />
      ))}
    </Card>
  )
}
