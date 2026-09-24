import { useCallback, useMemo, useState } from 'react'
import { loadTaskWindow, loadTenants } from './api'
import { helpAnchor, type TopicId } from './help'
import { Id, Screen, timeAgo } from './Shell'
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
 * Where the sentences that used to be on this screen now live.
 *
 * Typed as `TopicId` rather than spelled into an href, so a renamed topic is a
 * compile error here instead of a `?` that lands on the top of the Help page
 * and answers nothing -- the same rule `Dock.tsx` states for its own link.
 */
const WINDOW_HELP: TopicId = 'partial-read'
const SPEND_HELP: TopicId = 'tokens-reported'
const SCOPE_HELP: TopicId = 'tenant-scope'
const ABSENCE_HELP: TopicId = 'absent-vs-zero'

/** `#help/<topic>`, as an href. */
function helpHref(topic: TopicId): string {
  return `#${helpAnchor(topic)}`
}

/**
 * Screen A1 -- the Timeline pane of Work (it was History's until the nav
 * collapsed to three sections; the route is `#work/timeline`).
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
      // "Timeline", not "Activity". "Activity" was this screen's own name when
      // it was a top-level nav item, and the redesign retired it there for
      // being a paraphrase of a question rather than a name for a thing; it
      // then survived as the heading, so clicking through to this pane landed
      // on a page headed with the name of a section that no longer existed.
      // What this renders IS a timeline: rows bucketed by hour, day, week or
      // month across the span they turned out to cover.
      //
      // THE SECTION ABOVE IT HAS NOW CHANGED TWICE AND THE HEADING HAS NOT,
      // which is the point of the rule: Activity became History > Timeline,
      // and History became Work > Timeline when the nav collapsed to three
      // sections. The pane is the same route, the same read and the same
      // heading through both; `#activity/timeline` and `#history/timeline`
      // both still open it (App.tsx, SECTION_ALIASES).
      title="Timeline"
      load={load}
      summary={(w) => <WindowSummary window={w} />}
      // ONE SENTENCE, and it is the one that distinguishes this from a failed
      // read -- which is the distinction the whole product is built on. The
      // second sentence restated it and is gone.
      empty={{
        heading: 'No tasks in this tenant',
        body: 'The read succeeded and returned nothing.',
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
      {/* A PARTIAL TOTAL IS NOT A TOTAL, and it is now drawn rather than
          narrated. `.ctl-mark.is-partial` is dashed on one edge only -- the
          side the missing part would have been on -- and the qualifier beside
          it names the window everything below is computed over. The 20-word
          sentence that said the same thing is `#help/partial-read`, and the
          full claim is the mark's accessible name, so a screen reader gets it
          at the mark instead of two lines away from it. */}
      {w.moreExist && (
        <p
          className="client-side wb-more"
          aria-label={`Older tasks exist beyond this window. Everything below describes these ${w.tasks.length} rows and the span above, not all time.`}
        >
          <i className="ctl-mark is-partial">partial</i>
          <span className="wb-more-note">
            these {w.tasks.length} rows only · older tasks exist
          </span>
          <a href={helpHref(WINDOW_HELP)}>Why &rarr;</a>
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
      {/* THE QUALIFIER, where 28 words of warning paragraph used to be. The
          fact -- these rows carry no `completed_at` and are counted as open
          rather than dropped -- is a fact about this chart, so it sits on the
          chart's own header line and cannot drift away from it. WHY it is a
          data bug (every writer that moves a task terminal sets the field) is
          the mark's accessible name and one click away. */}
      {anomalies > 0 && (
        <p
          className="ctl-panel-note"
          aria-label={`${anomalies} terminal tasks carry no completed_at. Every writer that moves a task terminal sets it, so this is a data bug; they are counted as still open rather than dropped.`}
        >
          <i className="ctl-mark is-unread">not read</i> {anomalies} with no{' '}
          <code>completed_at</code>, counted as open
          <a href={helpHref(ABSENCE_HELP)}>Why &rarr;</a>
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
      {/* THE LEGEND IS A LEGEND AGAIN. It carried a trailing clause explaining
          that the stacks bucket on `completed_at` and that submission time is
          a different question -- which is true, and is the axis's job to say.
          It is the axis's job now: the panel title names the bucket and the
          legend's own key says `by completed_at`, fused to the thing it
          qualifies rather than trailing it as a sentence. */}
      <p className="chart-legend">
        <span className="k succeeded" /> succeeded <span className="k failed" /> failed
        <span className="k cancelled" /> cancelled <span className="k open" /> still open
        <span className="cl-basis">
          by <code>completed_at</code>
        </span>
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
      {/* A TILE'S `sub` IS A QUALIFIER, NEVER A DEFINITION (§6.2). "a lease
          reclaimed before dispatch counts here" is the definition; "admissions,
          not runs" is the qualifier, and it is the half that changes how the
          figure is read. */}
      <Tile label="Attempts consumed" value={String(attempts)} sub="admissions, not runs" />
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
    // NEVER A NUMBER HERE. The words are still "not recorded" and the tile is
    // still on screen -- both are the invariant and neither moves. What moved
    // is the 39-word account of WHY (the capture read the wrong level of the
    // runner's result, it is fixed in the worker and not yet in an image, the
    // raw figures are in each transcript): that is a paragraph, it is the same
    // paragraph on every visit, and it is `#help/tokens-reported`. The dashed
    // border and the mark are what a reader sees instead, and they say the one
    // thing the paragraph was there to prevent -- this is not zero.
    return (
      <div
        className="tile blocked is-absent"
        aria-label="Tokens and spend were not recorded. Every task stored an empty usage block, so this is an absent measurement and not a spend of zero."
      >
        <span className="t-label">Tokens &amp; spend</span>
        <span className="t-value">not recorded</span>
        <span className="t-sub">
          <i className="ctl-mark is-absent">not measured</i>
          <a href={helpHref(SPEND_HELP)}>Why &rarr;</a>
        </span>
      </div>
    )
  }
  const pct = Math.round((withUsage / Math.max(1, total)) * 100)
  return (
    <div
      className="tile"
      aria-label={`${withUsage} of ${total} rows carry usage. The rest predate the capture, so any total over this window would understate.`}
    >
      <span className="t-label">Tokens &amp; spend</span>
      <span className="t-value">{withUsage} of {total}</span>
      <span className="t-sub">
        <span className="coverage">
          <i style={{ width: `${pct}%` }} />
        </span>
        {pct}% of rows carry usage
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
      {/* `is-stacked` — F6 OF `docs/audits/2026-09-23/overflow-inventory.md`,
          which measured this exact table at 390pt: `clientWidth: 358` against a
          `scrollWidth` of 512, so 30% of it was behind an `overflow-x: auto`
          that paints no scrollbar on this platform. The three columns hiding
          there are `Failed`, `Attempts` and `Last seen` — a per-engineer table
          showing tasks and successes and NOT failures reads as a clean record
          for everyone on it. Below 900px each row becomes a stacked record with
          its own key column (§B6.3 in `styles.css`); `data-label` is what
          supplies that key, as an attribute so the rendered-word budgets are
          unchanged, and the explicit `role`s keep the ARIA table that changing
          `display` would otherwise drop. */}
      <div className="table-wrap is-stacked">
        <table className="pools" role="table">
          <thead role="rowgroup">
            <tr role="row">
              <th role="columnheader" scope="col">Engineer</th>
              <th role="columnheader" scope="col" className="n">Tasks</th>
              <th role="columnheader" scope="col" className="n">Succeeded</th>
              <th role="columnheader" scope="col" className="n">Failed</th>
              <th role="columnheader" scope="col" className="n">Attempts</th>
              <th role="columnheader" scope="col">Last seen</th>
            </tr>
          </thead>
          <tbody role="rowgroup">
            {people.map(([who, rows]) => (
              <tr role="row" key={who}>
                <th role="rowheader" scope="row">{who}</th>
                <td role="cell" data-label="Tasks" className="n">{rows.length}</td>
                <td role="cell" data-label="Succeeded" className="n">{rows.filter((t) => t.state === 'SUCCEEDED').length}</td>
                <td role="cell" data-label="Failed" className="n">{rows.filter((t) => t.state === 'FAILED').length}</td>
                <td role="cell" data-label="Attempts" className="n">{rows.reduce((n, t) => n + t.attempt_count, 0)}</td>
                <td role="cell" data-label="Last seen">
                  {timeAgo(
                    rows.map((t) => t.updated_at).sort().slice(-1)[0] ?? rows[0]!.created_at,
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {/* THE PROVENANCE STRIP (§8.4.4): when, over what, from where. One line.
          The 44-word version also explained that there is no server-side index
          on `submitted_by` and that an engineer outside the window is absent
          rather than zero -- the first is an argument and belongs in
          `#help/tenant-scope`, the second is the invariant and is now the
          mark, which is attached to the table instead of sitting under it. */}
      <p
        className="ctl-panel-note"
        aria-label={`Grouped client-side over the ${w.tasks.length} rows in the window. There is no server-side filter or index on submitted_by, so an engineer whose work fell outside the window is absent here rather than shown as zero.`}
      >
        <i className="ctl-mark is-partial">partial</i>
        client-side over {w.tasks.length} rows in the window
        <a href={helpHref(SCOPE_HELP)}>Why &rarr;</a>
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
      // One sentence, and it is the one that separates a real zero from a
      // failed read. The provisioning argument is `docs/`.
      empty={{
        heading: 'No tenants',
        body: 'The read succeeded and returned nothing.',
      }}
    >
      {(d) => (
        <section className="section">
          {/* `is-stacked`, for the same reason as the People table above and
              more of it: eight columns is the widest table in this file, so at
              390pt everything from `Max active` rightwards — the two ceilings,
              the credentials, the service account and the enabled/disabled
              state — sat behind a scrollbar this platform does not paint. A
              tenant row whose visible part ends at `Principal` says nothing
              about whether that tenant can run anything at all. */}
          <div className="table-wrap is-stacked">
            <table className="pools" role="table">
              <thead role="rowgroup">
                <tr role="row">
                  <th role="columnheader" scope="col">Tenant</th>
                  <th role="columnheader" scope="col">Kind</th>
                  <th role="columnheader" scope="col">Principal</th>
                  <th role="columnheader" scope="col" className="n">Max active</th>
                  <th role="columnheader" scope="col" className="n">Units</th>
                  <th role="columnheader" scope="col">Credentials</th>
                  <th role="columnheader" scope="col">Identity</th>
                  <th role="columnheader" scope="col">Status</th>
                </tr>
              </thead>
              <tbody role="rowgroup">
                {d.tenants.map((t) => (
                  <tr role="row" key={t.tenant_id} className={t.enabled === false ? 'paused' : undefined}>
                    <th role="rowheader" scope="row">{t.tenant_id}</th>
                    <td role="cell" data-label="Kind">{t.kind}</td>
                    <td role="cell" data-label="Principal" className="mono">{t.principal}</td>
                    <td role="cell" data-label="Max active" className="n">{t.max_active}</td>
                    <td role="cell" data-label="Units" className="n">{t.capacity_units}</td>
                    <td role="cell" data-label="Credentials">
                      {t.credentials.length > 0 ? (
                        t.credentials.map((c) => (
                          // These are Secret Manager NAMES and `.tag`
                          // uppercases. An uppercased secret name is one
                          // nobody can look up, so the value is wrapped:
                          // `.id` beats the ancestor by inheritance.
                          <span className="tag" key={c}>
                            <Id>{c}</Id>
                          </span>
                        ))
                      ) : (
                        <span className="tag capped">none registered</span>
                      )}
                    </td>
                    <td role="cell" data-label="Identity" className="mono">
                      {/* null means NO IDENTITY, not an empty string. A blank
                          cell here reads as fine and it is the opposite. */}
                      {t.service_account ?? (
                        <span className="tag full">no service account</span>
                      )}
                    </td>
                    <td role="cell" data-label="Status">
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
          {/* WHY A COLUMN IS ABSENT IS STILL STATED, IN ONE LINE. A table with
              a column quietly missing is a table a reader completes from
              memory, so this cannot simply be deleted -- but the 56-word
              account of the 422, the missing cost-attribution source and the
              permanent null is an argument, and arguments live in `docs/`.
              What stays is the fact: there is no budget column, and the reason
              is one word long. */}
          <p
            className="ctl-panel-note"
            aria-label="monthly_budget_usd is not shown. PUT /v1/admin/tenants/{id}/limits rejects it with a 422 because there is no cost attribution source, so it is null for every tenant; rendering it would render a permanent blank that reads as no budget set."
          >
            <i className="ctl-mark is-absent">not measured</i>
            no <code>monthly_budget_usd</code> column · no cost attribution
            source
          </p>
        </section>
      )}
    </Screen>
  )
}
