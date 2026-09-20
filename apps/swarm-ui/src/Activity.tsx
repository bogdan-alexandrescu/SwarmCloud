import { useCallback, useMemo, useState } from 'react'
import { loadTaskWindow, loadTenants } from './api'
import { Screen, timeAgo } from './Shell'
import {
  TERMINAL_STATES,
  bucketStart,
  hasUsage,
  type Bucket,
  type Task,
  type TaskWindow,
} from './types'

const BUCKETS: Bucket[] = ['hour', 'day', 'week', 'month']
const BUDGETS = [200, 500, 1000, 2000]

/**
 * Screen A1 -- Activity.
 *
 * THE ONE DESIGN RULE: bound by ROWS, label by the SPAN those rows covered.
 * The window control says "Last 500 tasks", never "Last 7 days" -- the latter
 * becomes a lie the moment the window truncates, which is exactly the class
 * of lie this platform keeps shipping. The header then reports the span the
 * rows turned out to cover, which may be six hours or three months.
 *
 * The bucket control GROUPS, it does not filter. Selecting Day re-buckets
 * rows already fetched; it cannot fetch a different range, because list_tasks
 * applies exactly one inequality and it comes from the page token. The label
 * says "Group by" so nobody files a bug against a control doing what it says.
 */
export function ActivityScreen() {
  const [budget, setBudget] = useState(500)
  const [bucket, setBucket] = useState<Bucket>('day')
  const load = useCallback(() => loadTaskWindow(budget), [budget])

  return (
    <Screen
      title="Activity"
      load={load}
      summary={(w) => <WindowSummary window={w} />}
      empty={{
        heading: 'No tasks in this tenant',
        body: 'The read succeeded and returned nothing. Nothing has ever been submitted under this tenant.',
      }}
    >
      {(w) => (
        <>
          <WindowBar
            window={w}
            budget={budget}
            setBudget={setBudget}
            bucket={bucket}
            setBucket={setBucket}
          />
          <Chart window={w} bucket={bucket} />
          <Tiles window={w} />
          <RunnerSplit window={w} />
          <People window={w} />
        </>
      )}
    </Screen>
  )
}

function WindowSummary({ window: w }: { window: TaskWindow }) {
  return (
    <>
      {w.tasks.length} tasks{w.moreExist && ' · older tasks exist'}
    </>
  )
}

/** The span these rows actually covered, in words. */
function spanOf(w: TaskWindow): string {
  if (!w.from || !w.to) return 'no span'
  const a = new Date(w.from)
  const b = new Date(w.to)
  const ms = b.getTime() - a.getTime()
  if (!Number.isFinite(ms)) return 'no span'
  const h = Math.round(ms / 3_600_000)
  const dur = h < 48 ? `${h}h` : `${Math.floor(h / 24)}d ${h % 24}h`
  const fmt = (d: Date) =>
    d.toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
  return `${fmt(a)} → ${fmt(b)} (${dur})`
}

function WindowBar({
  window: w,
  budget,
  setBudget,
  bucket,
  setBucket,
}: {
  window: TaskWindow
  budget: number
  setBudget: (n: number) => void
  bucket: Bucket
  setBucket: (b: Bucket) => void
}) {
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone
  const spanDays =
    w.from && w.to ? (new Date(w.to).getTime() - new Date(w.from).getTime()) / 86_400_000 : 0

  return (
    <div className="window-bar">
      <div className="wb-span">
        <strong>Last {budget} tasks</strong>
        <span className="wb-detail">
          {spanOf(w)} · {zone}
        </span>
      </div>
      <div className="wb-controls">
        <label>
          Group by
          <select value={bucket} onChange={(e) => setBucket(e.target.value as Bucket)}>
            {BUCKETS.map((b) => (
              // Month over a span under 60 days renders one lonely column and
              // reads as "we only have one month of data". Disabled with the
              // reason, rather than drawn.
              <option key={b} value={b} disabled={b === 'month' && spanDays < 60}>
                {b}
                {b === 'month' && spanDays < 60 ? ' (span too short)' : ''}
              </option>
            ))}
          </select>
        </label>
        <label>
          Rows
          <select value={budget} onChange={(e) => setBudget(Number(e.target.value))}>
            {BUDGETS.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
      </div>
      {w.moreExist && (
        <p className="client-side wb-more">
          Older tasks exist beyond this window — everything below describes
          these {w.tasks.length} rows and the span above, not all time.
        </p>
      )}
    </div>
  )
}

/**
 * Stacked columns, one per bucket.
 *
 * Terminal series bucket on `completed_at`; the submitted overlay buckets on
 * `created_at`. They are genuinely different questions and are never merged.
 *
 * `completed_at` is safe to bucket on because EVERY writer that moves a task
 * terminal sets it -- worker, API cancel, scheduler cancel, reconciler
 * reclaim. So a terminal row with a null completed_at is a data bug, and it
 * is counted in "still open" AND surfaced, rather than dropped.
 */
function Chart({ window: w, bucket }: { window: TaskWindow; bucket: Bucket }) {
  const { buckets, anomalies } = useMemo(() => {
    const map = new Map<
      number,
      { succeeded: number; failed: number; cancelled: number; open: number; submitted: number }
    >()
    let anomalies = 0
    const cell = (k: number) => {
      let c = map.get(k)
      if (!c) {
        c = { succeeded: 0, failed: 0, cancelled: 0, open: 0, submitted: 0 }
        map.set(k, c)
      }
      return c
    }

    for (const t of w.tasks) {
      const sub = bucketStart(t.created_at, bucket)
      if (sub !== null) cell(sub).submitted++

      const terminal = TERMINAL_STATES.has(t.state)
      const at = t.completed_at ? bucketStart(t.completed_at, bucket) : null

      if (terminal && at === null) {
        // A terminal task with no completed_at. Not dropped.
        anomalies++
        if (sub !== null) cell(sub).open++
        continue
      }
      if (!terminal) {
        if (sub !== null) cell(sub).open++
        continue
      }
      const c = cell(at as number)
      if (t.state === 'SUCCEEDED') c.succeeded++
      else if (t.state === 'FAILED' || t.state === 'DEAD_LETTERED') c.failed++
      else c.cancelled++
    }
    return {
      buckets: Array.from(map.entries()).sort(([a], [b]) => a - b),
      anomalies,
    }
  }, [w.tasks, bucket])

  const max = Math.max(1, ...buckets.map(([, c]) => c.succeeded + c.failed + c.cancelled + c.open))

  return (
    <section className="section">
      <h2>Outcomes by {bucket}</h2>
      {anomalies > 0 && (
        <p className="warn-text">
          {anomalies} terminal task{anomalies === 1 ? '' : 's'} carry no{' '}
          <code>completed_at</code>. Every writer that moves a task terminal sets
          it, so this is a data bug — they are counted as still open rather than
          dropped.
        </p>
      )}
      <div className="chart" role="img" aria-label={`Task outcomes by ${bucket}`}>
        {buckets.map(([k, c]) => {
          const total = c.succeeded + c.failed + c.cancelled + c.open
          return (
            <div className="col" key={k} title={`${new Date(k).toLocaleString()}\n${total} tasks`}>
              <div className="stackcol">
                <i className="open" style={{ height: `${(c.open / max) * 100}%` }} />
                <i className="cancelled" style={{ height: `${(c.cancelled / max) * 100}%` }} />
                <i className="failed" style={{ height: `${(c.failed / max) * 100}%` }} />
                <i className="succeeded" style={{ height: `${(c.succeeded / max) * 100}%` }} />
              </div>
              <span className="col-label">{labelFor(k, bucket)}</span>
            </div>
          )
        })}
      </div>
      <p className="chart-legend">
        <span className="k succeeded" /> succeeded <span className="k failed" /> failed
        <span className="k cancelled" /> cancelled <span className="k open" /> still open
        {' · '}
        stacks bucket on <code>completed_at</code>; a task&apos;s submission time is
        a different question
      </p>
    </section>
  )
}

function labelFor(ms: number, bucket: Bucket): string {
  const d = new Date(ms)
  if (bucket === 'hour') return d.toLocaleTimeString(undefined, { hour: '2-digit' })
  if (bucket === 'month') return d.toLocaleDateString(undefined, { month: 'short' })
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
}

function Tiles({ window: w }: { window: TaskWindow }) {
  const rows = w.tasks
  const completed = rows.filter((t) => t.completed_at !== null)
  const succeeded = completed.filter((t) => t.state === 'SUCCEEDED').length
  const attempts = rows.reduce((n, t) => n + t.attempt_count, 0)
  const withUsage = rows.filter(hasUsage).length

  return (
    <div className="tiles">
      <Tile label="Submitted" value={String(rows.length)} sub={spanOf(w)} />
      <Tile
        label="Completed"
        value={String(completed.length)}
        sub={
          completed.length > 0
            ? `${Math.round((succeeded / completed.length) * 100)}% succeeded`
            : 'none completed in window'
        }
      />
      <Tile
        label="Attempts consumed"
        value={String(attempts)}
        sub="admissions, not runs — a lease reclaimed before dispatch counts here"
      />
      <SpendTile total={rows.length} withUsage={withUsage} />
    </div>
  )
}

function Tile({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <div className="tile">
      <span className="t-label">{label}</span>
      <span className="t-value">{value}</span>
      <span className="t-sub">{sub}</span>
    </div>
  )
}

/**
 * Tokens and spend.
 *
 * This tile STAYS on the screen when there is nothing to show, because tokens
 * were explicitly asked for and an absent tile answers nothing. It renders the
 * words "not recorded" — never a number, never ~$0.00. A zero here reads as
 * "this engineer spent nothing", which is worse than omitting it.
 *
 * The coverage bar is the honest form once numbers start arriving: it says
 * what fraction of rows actually carry usage, rather than summing the ones
 * that do and presenting it as a total.
 */
function SpendTile({ total, withUsage }: { total: number; withUsage: number }) {
  if (withUsage === 0) {
    return (
      <div className="tile blocked">
        <span className="t-label">Tokens &amp; spend</span>
        <span className="t-value">not recorded</span>
        <span className="t-sub">
          The capture read the wrong level of the runner&apos;s result, so every
          task stored an empty usage block. Fixed in the worker, not yet shipped
          in an image — and tasks that already ran will never have it. The raw
          numbers are still in each attempt&apos;s transcript in GCS.
        </span>
      </div>
    )
  }
  const pct = Math.round((withUsage / Math.max(1, total)) * 100)
  return (
    <div className="tile">
      <span className="t-label">Tokens &amp; spend</span>
      <span className="t-value">{withUsage} of {total}</span>
      <span className="t-sub">
        <span className="coverage">
          <i style={{ width: `${pct}%` }} />
        </span>
        {pct}% of rows carry usage — the rest predate the capture, so any total
        would understate.
      </span>
    </div>
  )
}

function RunnerSplit({ window: w }: { window: TaskWindow }) {
  const counts = new Map<string, number>()
  for (const t of w.tasks) counts.set(t.runner_profile, (counts.get(t.runner_profile) ?? 0) + 1)
  const entries = Array.from(counts.entries()).sort((a, b) => b[1] - a[1])
  const max = Math.max(1, ...entries.map(([, n]) => n))

  return (
    <section className="section">
      <h2>By runner profile</h2>
      {/* Whatever string arrives, never a hardcoded list: the catalogue is
          frozen contract data and can gain entries. */}
      {entries.map(([name, n]) => (
        <div className="split-row" key={name}>
          <span className="sr-name">{name}</span>
          <span className="sr-bar">
            <i style={{ width: `${(n / max) * 100}%` }} />
          </span>
          <span className="sr-n">{n}</span>
        </div>
      ))}
    </section>
  )
}

/**
 * Screen A2 -- People. Derived from A1's rows at zero extra reads.
 *
 * There is no server-side grouping or filtering by owner: list_tasks accepts
 * only state, workflow_id and runner_profile, and no index covers
 * submitted_by. So this is client-side over the window and says so.
 */
function People({ window: w }: { window: TaskWindow }) {
  const people = useMemo(() => {
    const m = new Map<string, Task[]>()
    for (const t of w.tasks) {
      const who = t.submitted_by ?? 'unattributed'
      const list = m.get(who)
      if (list) list.push(t)
      else m.set(who, [t])
    }
    return Array.from(m.entries()).sort((a, b) => b[1].length - a[1].length)
  }, [w.tasks])

  return (
    <section className="section">
      <h2>People</h2>
      <div className="table-wrap">
        <table className="pools">
          <thead>
            <tr>
              <th scope="col">Engineer</th>
              <th scope="col" className="n">Tasks</th>
              <th scope="col" className="n">Succeeded</th>
              <th scope="col" className="n">Failed</th>
              <th scope="col" className="n">Attempts</th>
              <th scope="col">Last seen</th>
            </tr>
          </thead>
          <tbody>
            {people.map(([who, rows]) => (
              <tr key={who}>
                <th scope="row">{who}</th>
                <td className="n">{rows.length}</td>
                <td className="n">{rows.filter((t) => t.state === 'SUCCEEDED').length}</td>
                <td className="n">{rows.filter((t) => t.state === 'FAILED').length}</td>
                <td className="n">{rows.reduce((n, t) => n + t.attempt_count, 0)}</td>
                <td>
                  {timeAgo(
                    rows.map((t) => t.updated_at).sort().slice(-1)[0] ?? rows[0]!.created_at,
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted small">
        Grouped client-side over the {w.tasks.length} rows in the window. There
        is no server-side filter or index on <code>submitted_by</code>, so this
        cannot be a per-engineer query — and an engineer whose work fell outside
        the window is absent rather than shown as zero.
      </p>
    </section>
  )
}

/**
 * Screen A4 -- Tenants (admin only), and the platform-wide state counts.
 *
 * Both are admin-gated, and a 403 renders as information rather than a red
 * failure: a non-admin genuinely cannot read these, and styling that as an
 * error makes a working page look broken.
 */
export function TenantsScreen() {
  return (
    <Screen
      title="Tenants"
      load={loadTenants}
      summary={(d) => `${d.tenants.length} tenants`}
      empty={{
        heading: 'No tenants',
        body: 'The read succeeded and returned nothing. Tenants are created at provisioning time, so an environment with none has not been fully applied.',
      }}
    >
      {(d) => (
        <section className="section">
          <div className="table-wrap">
            <table className="pools">
              <thead>
                <tr>
                  <th scope="col">Tenant</th>
                  <th scope="col">Kind</th>
                  <th scope="col">Principal</th>
                  <th scope="col" className="n">Max active</th>
                  <th scope="col" className="n">Units</th>
                  <th scope="col">Credentials</th>
                  <th scope="col">Identity</th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {d.tenants.map((t) => (
                  <tr key={t.tenant_id} className={t.enabled === false ? 'paused' : undefined}>
                    <th scope="row">{t.tenant_id}</th>
                    <td>{t.kind}</td>
                    <td className="mono">{t.principal}</td>
                    <td className="n">{t.max_active}</td>
                    <td className="n">{t.capacity_units}</td>
                    <td>
                      {t.credentials.length > 0 ? (
                        t.credentials.map((c) => (
                          <span className="tag" key={c}>
                            {c}
                          </span>
                        ))
                      ) : (
                        <span className="tag capped">none registered</span>
                      )}
                    </td>
                    <td className="mono">
                      {/* null means NO IDENTITY, not an empty string. A blank
                          cell here reads as fine and it is the opposite. */}
                      {t.service_account ?? (
                        <span className="tag full">no service account</span>
                      )}
                    </td>
                    <td>
                      {t.enabled === false ? (
                        <span className="tag paused">disabled</span>
                      ) : (
                        <span className="tag ok">enabled</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="muted small">
            <code>monthly_budget_usd</code> is deliberately not shown. The field
            exists on the model and is returned by the API, but{' '}
            <code>PUT /v1/admin/tenants/&#123;id&#125;/limits</code> rejects it with a 422
            — there is no cost attribution source — so it is <code>null</code> for
            every tenant. Rendering it would be rendering a permanent blank that
            reads as &ldquo;no budget set&rdquo;.
          </p>
        </section>
      )}
    </Screen>
  )
}
