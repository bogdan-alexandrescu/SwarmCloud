import { useCallback, type CSSProperties, type ReactNode } from 'react'
import { loadAgentRun, type AgentRun } from './api'
import { num } from './fetch'
import { LivenessBadge } from './Liveness'
import { Screen, timeAgo } from './Shell'
import {
  GIB,
  TERMINAL_STATES,
  attemptOutcome,
  attemptRan,
  bytesLabel,
  checkpointsFor,
  elapsed,
  newestHeartbeat,
  restoredFrom,
  stateGlyph,
  stateTone,
  usageOf,
  whyAgent,
  type ArtifactRef,
  type AttemptRow,
  type GitSummary,
  type ResourceClassSpec,
  type ResultSummary,
  type Task,
  type TaskEvent,
  type Tone,
} from './types'

/**
 * ONE AGENT RUN, IN FULL.
 *
 * WHAT CHANGED AND WHY. The previous version of this screen showed the task's
 * `result_summary` -- which `finish()` writes ONCE, at terminal state -- as if
 * it described the run. It describes the LAST ATTEMPT. A task that failed
 * twice and succeeded on the third showed attempt three's artifacts, attempt
 * three's tokens and attempt three's exit code, and the two failures were a
 * row in a five-column table with no resources, no spend and no checkpoints.
 * Everything a person opens this page to find out about a retried run was the
 * part that was missing.
 *
 * So the unit of this screen is now the ATTEMPT. Each one carries its own
 * runtime, its own requested-vs-utilised, its own tokens and cost, its own
 * checkpoints and its own error, and the run-level panels say explicitly which
 * attempt they describe.
 *
 * THE THREE THINGS THIS SCREEN MUST NOT DO, in the order they were got wrong
 * before:
 *
 *  1. Render an absent measurement as a number. Peak RSS is written at the END
 *     of an attempt, tokens are null on every attempt that ran before the
 *     worker capture shipped, and a checkpoint's size lives on an event that
 *     may be off the page. All three are em dashes with a reason, never zeros.
 *  2. Draw a ceiling it does not have. `RESOURCE_CLASSES` is served by
 *     `/v1/resource-classes`; when that read fails the bars are hatched with no
 *     fill, because an unfilled plain track reads as "0% used".
 *  3. Invent what it cannot see. Checkpoint CONTENTS are not recorded anywhere
 *     -- only id, size and uri -- so this screen says that instead of drawing a
 *     file tree.
 */
export function AgentDetailScreen({ taskId, onClose }: { taskId: string; onClose: () => void }) {
  const load = useCallback(() => loadAgentRun(taskId), [taskId])

  return (
    // The drawer and its close button are drawn here AND by App.tsx's
    // `.ctl-drawer` wrapper, which flattens this one with two scoped CSS rules
    // so the panel is not nested twice. Both are kept: this component stays
    // usable on its own, and the rules become no-ops rather than breakage if
    // the wrapper ever goes away.
    <div className="drawer" role="dialog" aria-label={`Agent ${taskId}`}>
      <button className="drawer-close" onClick={onClose} aria-label="Close">
        ✕
      </button>
      <Screen
        // KEYED ON THE TASK, AND THIS IS NOT A DETAIL. `Screen` re-runs its
        // load effect on its retry nonce only (`[nonce]`, with the exhaustive
        // -deps rule disabled), so when the drawer stays mounted and `taskId`
        // changes -- clicking a second agent while this one is open -- the
        // effect never fires again. The heading updated and the body did not,
        // so the page showed ANOTHER task's attempts, checkpoints and spend
        // under this task's id: not stale data, wrong data, which is worse
        // than either. The key remounts `Screen`, which is the fix available
        // from inside this file; Shell.tsx belongs to another track and the
        // dependency list is theirs to widen.
        key={taskId}
        title={taskId}
        load={load}
        summary={(r) => (
          <>
            {r.task.runner_profile} · {r.task.resource_class} ·{' '}
            {r.attempts === null
              ? 'attempts unread'
              : `${r.attempts.length} attempt${r.attempts.length === 1 ? '' : 's'}`}
          </>
        )}
      >
        {(r) => <Run run={r} />}
      </Screen>
    </div>
  )
}

function Run({ run }: { run: AgentRun }) {
  const { task, events } = run
  const now = Date.now()

  return (
    <>
      <Headline run={run} now={now} />
      <Alerts task={task} />
      <RunMetrics run={run} now={now} />
      <Why task={task} />
      {task.last_error && <ErrorBanner text={task.last_error} />}
      <Attempts run={run} now={now} />
      <Output run={run} />
      <Input task={task} />
      <Timeline events={events} detail={run.eventsDetail} attempts={run.attempts} />
    </>
  )
}

// ---------------------------------------------------------------------------
// Primitives, built from the ctl-* vocabulary in styles.css
// ---------------------------------------------------------------------------

/**
 * `.ctl-chip` tones are ok / warn / bad / info / paused / unknown / live.
 * `Tone` from types.ts has `wait`, which has no chip class -- mapping it to
 * `warn` rather than letting it fall through keeps a THROTTLED-shaped state
 * from silently rendering in the default grey, which is the colour reserved
 * for "we do not know".
 */
function chipTone(tone: Tone | 'unknown'): string {
  return tone === 'wait' ? 'warn' : tone
}

function Chip({ tone, children }: { tone: Tone | 'unknown'; children: ReactNode }) {
  return (
    <span className={`ctl-chip is-${chipTone(tone)}`}>
      {/* Decoration only. The word beside it carries the meaning, because a
          colour-only chip fails in a greyscale incident screenshot. */}
      <i aria-hidden />
      {children}
    </span>
  )
}

/** The em dash, as a token rather than a bare character, so "not measured" is
 *  styleable as a class of thing and never mistaken for a digit. */
function Em() {
  return <span className="ctl-em">—</span>
}

/**
 * One fact with its units and what it excludes.
 *
 * `tone` carries the two absences the platform keeps confusing:
 *   absent  the platform did not record it. The value is a SENTENCE.
 *   unread  we failed to read it. The platform may well have the number.
 */
function Metric({
  label,
  value,
  unit,
  sub,
  foot,
  tone,
}: {
  label: string
  value: ReactNode
  unit?: string
  sub?: ReactNode
  foot?: string
  tone?: 'absent' | 'unread' | 'alert' | 'good'
}) {
  return (
    <div className={`ctl-metric${tone ? ` is-${tone}` : ''}`}>
      <span className="ctl-metric-label">{label}</span>
      <span className="ctl-metric-value">
        {value}
        {unit !== undefined && <span className="ctl-metric-unit">{unit}</span>}
      </span>
      {sub !== undefined && <span className="ctl-metric-sub">{sub}</span>}
      {foot !== undefined && <span className="ctl-metric-foot">{foot}</span>}
    </div>
  )
}

/**
 * used / ceiling, as a bar that stays honest when either side is missing.
 *
 * THREE TRACK STATES and they must not converge:
 *   known        a fill proportional to used/ceiling
 *   unknown      hatched with NO fill -- because an empty plain track reads as
 *                "0% used", which is a claim about a measurement we do not have
 *   over ceiling the excess is hatched in the failure colour rather than
 *                clipped at 100%, which would hide the only interesting part
 *
 * The warn/bad colouring at 75% and 90% is PRESENTATION. The claim about
 * whether an attempt came dangerously close to its ceiling is
 * `oom_near_miss`, which the worker sets from its own threshold against the
 * cgroup the kernel's OOM killer reads. This bar never contradicts that flag;
 * it just makes a tall bar visible before you get to it.
 */
function Util({
  label,
  used,
  ceiling,
  fmt,
  by,
}: {
  label: ReactNode
  /** null means NOT MEASURED. */
  used: number | null
  /** null means the ceiling is unknown -- the catalogue read failed. */
  ceiling: number | null
  fmt: (v: number) => string
  /** The right-hand column: where the ceiling came from, or why `used` is absent. */
  by: string
}) {
  const known = used !== null && ceiling !== null && ceiling > 0
  const ratio = known && ceiling !== null && used !== null ? used / ceiling : null
  const over = ratio !== null && ratio > 1
  // When over, the track represents `used` and the ceiling sits inside it.
  const fillPct = ratio === null ? 0 : over ? (1 / ratio) * 100 : ratio * 100
  const fillTone = ratio === null ? '' : ratio >= 0.9 ? ' is-bad' : ratio >= 0.75 ? ' is-warn' : ''

  return (
    <div className="ctl-util">
      <span className="ctl-util-name">{label}</span>
      <span className={`ctl-util-track${known ? '' : ' is-unknown'}`}>
        {known && <span className={`ctl-util-fill${fillTone}`} style={{ width: `${fillPct}%` }} />}
        {over && <span className="ctl-util-over" style={{ width: `${100 - fillPct}%` }} />}
      </span>
      <span className="ctl-util-figure">
        {used === null ? <Em /> : fmt(used)}
        <span className="ctl-util-of"> / {ceiling === null ? '?' : fmt(ceiling)}</span>
      </span>
      <span className="ctl-util-by" title={by}>
        {by}
      </span>
    </div>
  )
}

/**
 * The four shapes an absent panel can have. They are four different facts and
 * this platform's defining bug was drawing them alike.
 */
function Absent({
  kind,
  heading,
  children,
  foot,
}: {
  kind: 'zero' | 'failed' | 'partial' | 'admin'
  heading: string
  children: ReactNode
  foot?: string
}) {
  const cls = kind === 'zero' ? '' : ` is-${kind}`
  return (
    <div className={`ctl-empty${cls}`} role={kind === 'zero' ? undefined : 'status'}>
      <h3>{heading}</h3>
      <p>{children}</p>
      {foot !== undefined && <span className="ctl-empty-foot">{foot}</span>}
    </div>
  )
}

/**
 * THE ATTEMPT CARD'S BOX, INLINE ON PURPOSE.
 *
 * `.panel` in styles.css is a heading modifier -- `display:flex` on an h2 --
 * and draws no container at all, so three attempts ran together into one
 * column with nothing marking where the second began. styles.css belongs to
 * another track this pass and the `ctl-` block has no card primitive, so the
 * box is inline: an inline style adds no selector and therefore cannot restyle
 * another screen, which is the rule the prefix exists to enforce. Every value
 * is an existing token, so it still flips with the theme.
 */
const CARD: CSSProperties = {
  border: '1px solid var(--line)',
  borderRadius: 'var(--radius)',
  background: 'var(--surface)',
  padding: 'var(--ctl-s3)',
  marginBottom: 'var(--ctl-s3)',
}

/** Sub-blocks inside a card. `.section`'s own 28px is too much three deep. */
const SUB: CSSProperties = { marginBottom: 'var(--ctl-s4)' }

/** A sub-block that follows a `dl.kv`, which has no bottom margin of its own,
 *  so its heading would otherwise sit directly on the last value. */
const SUB_AFTER_KV: CSSProperties = { marginTop: 'var(--ctl-s5)', marginBottom: 'var(--ctl-s4)' }

/** "1 attempt" / "3 attempts". Never "3 attempt(s)". */
function plural(count: number, word: string): string {
  return `${count} ${word}${count === 1 ? '' : 's'}`
}

function usd(v: number | null | undefined): ReactNode {
  // NEVER $0.00 for an unmeasured run. A zero here reads as "this was free",
  // which is the single most expensive misreading available on this screen.
  if (typeof v !== 'number' || !Number.isFinite(v)) return <Em />
  return `$${v.toFixed(4)}`
}

function tokens(v: number | null | undefined): ReactNode {
  if (typeof v !== 'number' || !Number.isFinite(v)) return <Em />
  return v.toLocaleString()
}

// ---------------------------------------------------------------------------
// Head
// ---------------------------------------------------------------------------

function Headline({ run, now }: { run: AgentRun; now: number }) {
  const { task, events } = run
  const el = elapsed(task, now)

  return (
    <section className="section panel">
      <h2>
        {/* The state as a `.ctl-chip`, not the old `.st` span: `.st` is only
            coloured inside `.row`, so in a heading it silently rendered in the
            heading's own faint grey -- the one element on the page whose colour
            is load-bearing was the one with none. The glyph stays because
            colour is never the only signal. */}
        <Chip tone={stateTone(task.state)}>
          <span aria-hidden>{stateGlyph(task.state)}</span> {task.state}
        </Chip>
        <LivenessBadge task={task} events={events} now={now} />
      </h2>
      <dl className="kv">
        <dt>Elapsed</dt>
        <dd>{el.text}</dd>
        <dt>Tenant</dt>
        <dd className="mono">{task.tenant_id}</dd>
        <dt>Owner</dt>
        <dd>{task.submitted_by ?? <Em />}</dd>
        <dt>Created</dt>
        <dd>{timeAgo(task.created_at)}</dd>
        {task.workflow_id !== null && (
          <>
            <dt>Workflow</dt>
            <dd className="mono">
              {task.workflow_id}
              {task.step_id !== null && ` · step ${task.step_id}`}
            </dd>
          </>
        )}
      </dl>
    </section>
  )
}

/**
 * The run in six figures.
 *
 * Every one of these is a rollup over ATTEMPTS, and each says what it is a
 * rollup over. "1.8 GiB peak" across three attempts is the worst attempt, not
 * the run's total, and a tile that does not say so gets read as both.
 */
function RunMetrics({ run, now }: { run: AgentRun; now: number }) {
  const { task, attempts, classes } = run
  const el = elapsed(task, now)

  if (attempts === null) {
    return (
      <div className="ctl-metrics">
        <Metric label="Elapsed" value={el.text} sub="From the task document, which loaded." />
        <Metric
          label="Attempts"
          value={`${task.attempt_count} / ${task.max_attempts}`}
          sub="The task's own counter."
        />
        {/* No number may appear for anything that came from the attempt read.
            A reassuring zero on a failed read is the worst output available. */}
        <Metric label="Peak memory" value="read failed" tone="unread" sub="The attempt query did not answer." />
        <Metric label="Spend" value="read failed" tone="unread" sub="The attempt query did not answer." />
      </div>
    )
  }

  const cls = classes?.[task.resource_class] ?? null
  const measured = attempts.filter((a) => a.peak_rss_bytes !== null)
  const peak = measured.length === 0 ? null : Math.max(...measured.map((a) => a.peak_rss_bytes ?? 0))
  const spent = attempts.filter((a) => a.cost_usd !== null)
  const cost = spent.length === 0 ? null : spent.reduce((t, a) => t + (a.cost_usd ?? 0), 0)
  const counted = attempts.filter((a) => a.input_tokens !== null || a.output_tokens !== null)
  const tok =
    counted.length === 0
      ? null
      : counted.reduce((t, a) => t + (a.input_tokens ?? 0) + (a.output_tokens ?? 0), 0)
  const ckpts = attempts.reduce((t, a) => t + a.checkpoints.length, 0)
  const nearMiss = attempts.some((a) => a.oom_near_miss)

  return (
    <div className="ctl-metrics">
      <Metric
        label="Elapsed"
        value={el.text}
        sub={task.completed_at === null ? 'Still running.' : `Finished ${timeAgo(task.completed_at)}.`}
      />
      <Metric
        label="Attempts"
        value={`${attempts.length}`}
        unit={`/ ${task.max_attempts} allowed`}
        sub={
          attempts.length === task.attempt_count
            ? 'Every attempt the task counts has a document.'
            : `The task counts ${task.attempt_count}.`
        }
        tone={attempts.length === task.attempt_count ? undefined : 'unread'}
      />
      {peak === null ? (
        <Metric
          label="Peak memory"
          value="not recorded"
          tone="absent"
          sub="Written when an attempt ends. A running attempt has none."
        />
      ) : (
        <Metric
          label="Peak memory"
          value={bytesLabel(peak)}
          sub={
            cls === null
              ? 'The ceiling for this class could not be read.'
              : `of ${cls.memory_gib} GiB granted · worst of ${plural(measured.length, 'measured attempt')}`
          }
          foot={nearMiss ? 'OOM near miss on at least one attempt' : undefined}
          tone={nearMiss ? 'alert' : undefined}
        />
      )}
      {tok === null ? (
        <Metric
          label="Tokens"
          value="not reported"
          tone="absent"
          sub="No attempt reported a token count. Not the same as a run that used none."
        />
      ) : (
        <Metric
          label="Tokens"
          value={tok.toLocaleString()}
          unit="in + out"
          sub={`Summed over ${plural(counted.length, 'attempt')} of ${attempts.length} that reported.`}
        />
      )}
      {cost === null ? (
        <Metric
          label="Token cost"
          value="not reported"
          tone="absent"
          sub="No attempt reported a cost. This is an absent measurement, not $0.00."
        />
      ) : (
        <Metric
          label="Token cost"
          value={usd(cost)}
          sub={`Summed over ${plural(spent.length, 'attempt')} of ${attempts.length} that reported.`}
          foot="tokens only — no infrastructure cost exists"
        />
      )}
      <Metric
        label="Checkpoints"
        value={`${ckpts}`}
        sub={
          ckpts === 0
            ? 'No attempt document lists one.'
            : `Across ${plural(attempts.length, 'attempt')}. Contents are not recorded.`
        }
      />
    </div>
  )
}

function Alerts({ task }: { task: Task }) {
  const live = !TERMINAL_STATES.has(task.state)

  return (
    <>
      {task.park_reason && (
        <div className="bar amber">
          <strong>{task.park_reason}</strong>
          {task.next_eligible_at && (
            <> — eligible again {new Date(task.next_eligible_at).toLocaleString()}</>
          )}
        </div>
      )}

      {/* NOT the same thing as park_reason, and this is the case a header that
          only renders park_reason gets wrong. record_blockers writes
          blocked_by while deliberately leaving the task READY -- the platform
          being busy is not a durable condition -- so READY with blockers and
          no park reason is the commonest "why is nothing happening", and it
          would otherwise show as a bare READY chip with no explanation. */}
      {task.blocked_by && task.blocked_by.length > 0 && (
        <div className="bar amber">
          {task.blocked_by.map((b, i) => (
            <div className="blocker" key={`${b.reason}-${i}`}>
              {b.pool && <code>{b.pool}</code>} <strong>{b.reason}</strong>
              {typeof b.active === 'number' && typeof b.limit === 'number' && (
                <>
                  {' '}
                  · {b.active} active / {b.limit} limit
                </>
              )}
              <span className="blocker-copy">
                {b.reason === 'TENANT_LIMIT'
                  ? 'Yours to raise.'
                  : b.reason === 'GLOBAL_CONCURRENCY_LIMIT'
                    ? 'The platform is full.'
                    : b.reason === 'MANUAL_PAUSE'
                      ? 'This pool was paused by an operator — a decision, not congestion.'
                      : ''}
              </span>
            </div>
          ))}
        </div>
      )}

      {task.cancel_requested && live && (
        <div className="bar red">
          Cancellation requested. The state will not change until the worker or
          the reconciler releases the lease — releasing it from the API would
          decrement a pool a live container still occupies.
        </div>
      )}
    </>
  )
}

function Why({ task }: { task: Task }) {
  const why = whyAgent(task)
  if (!why) return null
  return (
    <section className="section">
      <h2>Why</h2>
      <p className="why-full">{why}</p>
    </section>
  )
}

/**
 * The error banner. Full text, monospace, NEVER one-line-truncated -- it is
 * the reason the page was opened.
 *
 * Three flavours, distinguished by prefix because each means something
 * different about who decided the task had failed.
 */
function ErrorBanner({ text }: { text: string }) {
  const reconciled = text.startsWith('reconciled:')
  // A dispatch failure is written as `<STABLE_CODE> (attempt att_...)`.
  const dispatch = /^[A-Z][A-Z0-9_]+ \(attempt /.test(text)

  return (
    <section className="section">
      <h2>Error</h2>
      <div className="state failed">
        <p className="err-origin">
          {reconciled
            ? 'Written by the reconciler, which found this attempt in a state it could not repair.'
            : dispatch
              ? 'A dispatch failure, recorded by the scheduler. The full message is in the operator logs — this field is truncated at 1000 characters.'
              : "The agent's own error at finish, or the cancellation reason if it was cancelled before it held capacity."}
        </p>
        <pre className="err full">{text}</pre>
      </div>
    </section>
  )
}

// ---------------------------------------------------------------------------
// Attempts -- the unit of this screen
// ---------------------------------------------------------------------------

function Attempts({ run, now }: { run: AgentRun; now: number }) {
  const { task, attempts, attemptsDetail } = run

  if (attempts === null) {
    return (
      <section className="section">
        <h2>Attempts</h2>
        <Absent kind="failed" heading="The attempt history could not be read">
          This is a failed read, not a task that never ran. Nothing below may be
          concluded from its absence. {attemptsDetail}
        </Absent>
      </section>
    )
  }

  if (attempts.length === 0) {
    // A REAL ZERO, and which real zero depends on the state. A task waiting
    // for capacity has never been admitted and correctly has no attempt
    // document; a FINISHED task with none is a hole in the record.
    const finished = TERMINAL_STATES.has(task.state)
    return (
      <section className="section">
        <h2>Attempts</h2>
        {finished && task.attempt_count > 0 ? (
          <Absent kind="partial" heading="This task finished, and no attempt document came back">
            The task counts {plural(task.attempt_count, 'attempt')} and the query
            returned none. The documents are missing, which is not the same as
            never having run.
          </Absent>
        ) : (
          <Absent kind="zero" heading="Nothing has been admitted yet">
            The query succeeded and returned nothing. An attempt document is
            written when capacity is reserved, so a task that is{' '}
            <code>{task.state}</code> genuinely has none. This is a real zero,
            not a failed read.
          </Absent>
        )}
      </section>
    )
  }

  // Oldest first. A retried run only parses in the order it happened, and the
  // route hands them back newest-first. Sorted on `created_at` (ISO-8601 UTC,
  // so lexicographic) rather than on `generation`, which fences an attempt and
  // is not promised to be dense.
  const ordered = [...attempts].sort((x, y) => x.created_at.localeCompare(y.created_at))
  const latest = ordered[ordered.length - 1]

  return (
    <section className="section panel">
      <h2>
        Attempts
        <span className="count-chip">{plural(ordered.length, 'document')}</span>
      </h2>

      {ordered.length < task.attempt_count && (
        <div className="ctl-empty is-partial" role="status">
          <h3>Fewer documents than the task counts</h3>
          <p>
            The task records {plural(task.attempt_count, 'attempt')} and{' '}
            {plural(ordered.length, 'document')} came back. The rest are missing,
            not absent — every figure below describes only the attempts shown.
          </p>
        </div>
      )}

      {ordered.map((a, i) => (
        <AttemptCard
          key={a.attempt_id}
          a={a}
          ordinal={i + 1}
          isLatest={latest !== undefined && a.attempt_id === latest.attempt_id}
          run={run}
          now={now}
        />
      ))}

      <LatestCheckpointNote run={run} attempts={ordered} />
      <AttemptLegend />
    </section>
  )
}

/**
 * The standing rules behind every figure on the attempt cards, ONCE.
 *
 * Each of these was originally a paragraph under the panel it applied to,
 * which meant a three-attempt run repeated the same four explanations three
 * times and none of them got read. They are invariants of the platform rather
 * than facts about this run, so they belong here -- the same shape
 * `QuotaDetail.tsx` uses for the six provider states.
 */
function AttemptLegend() {
  return (
    <section className="section legend">
      <h2>Reading these cards</h2>
      <dl>
        <dt>The requested figure is a ceiling, not a target</dt>
        <dd>
          <code>requests == limits</code> platform-wide — there is no bursting,
          so nothing absorbs an overshoot. A bar at 90% is not &ldquo;well
          utilised&rdquo;; it is one chatty prompt from an OOM kill.
        </dd>
        <dt>The workspace is a slice OF memory</dt>
        <dd>
          Not capacity on top of it. The workspace is a memory-backed tmpfs,
          because the Terraform google provider cannot express Cloud Run&apos;s
          disk-backed one (<code>empty_dir.medium</code> accepts only{' '}
          <code>MEMORY</code>), so workspace bytes come out of the memory
          ceiling above them.
        </dd>
        <dt>
          <code>oom_near_miss</code> is the claim; the colour is not
        </dt>
        <dd>
          The flag is set by the worker against the cgroup counters the
          kernel&apos;s OOM killer itself reads. The warn and bad colours on the
          bars are presentation, and exist only so a tall bar is visible before
          you reach the flag.
        </dd>
        <dt>CPU is never sampled</dt>
        <dd>
          The sampler measures memory and disk. A cpu bar is drawn with its
          request and a hatched track rather than omitted, because a panel
          headed &ldquo;requested vs utilised&rdquo; that silently drops a third
          of the envelope reads as if cpu were known to be fine.
        </dd>
        <dt>A null spend figure is not a zero</dt>
        <dd>
          Only the CLI runners report tokens at all, and every attempt that ran
          before the worker capture shipped carries null for all five fields. A
          mock task costs nothing on purpose; an unparsed result cost an unknown
          amount. Rendering both as <code>$0.00</code> would lie about one.
        </dd>
        <dt>Token cost is the only cost that exists</dt>
        <dd>
          There is no billing integration of any kind, so the Cloud Run,
          Firestore and GCS cost of a run is not recorded anywhere this page can
          read. It is missing, not zero.
        </dd>
        <dt>What is INSIDE a checkpoint is not recorded</dt>
        <dd>
          Only the id, the size and the uri. Nothing writes a manifest of an
          archive&apos;s contents, so no screen can list its files — that needs
          a new route and a read out of GCS. The uri is what to fetch.
        </dd>
      </dl>
    </section>
  )
}

function AttemptCard({
  a,
  ordinal,
  isLatest,
  run,
  now,
}: {
  a: AttemptRow
  ordinal: number
  isLatest: boolean
  run: AgentRun
  now: number
}) {
  const out = attemptOutcome(a)

  return (
    <section className="section panel" style={CARD}>
      <h2>
        Attempt {ordinal}
        <span className="count-chip">gen {a.generation}</span>
        <Chip tone={out.tone}>{out.label}</Chip>
        {isLatest && <span className="tag wait">latest</span>}
        {a.oom_near_miss && (
          <span className="tag full" title="Peak memory came close to the ceiling this attempt was given">
            OOM near miss
          </span>
        )}
      </h2>

      <dl className="kv">
        <dt>Started</dt>
        <dd>{a.started_at === null ? <Em /> : timeAgo(a.started_at)}</dd>
        <dt>Finished</dt>
        {/* Null is three different things and the chip above says which: still
            running, never started, or ended without the field being written. */}
        <dd>{a.completed_at === null ? <Em /> : timeAgo(a.completed_at)}</dd>
        <dt>Duration</dt>
        <dd>{attemptRan(a, now)}</dd>
        <dt>Backend</dt>
        <dd>{a.backend}</dd>
        <dt>Execution</dt>
        <dd className="mono uri">
          {a.execution_name === null ? (
            <span className="muted">
              — never dispatched, so no execution was ever named
            </span>
          ) : (
            a.execution_name
          )}
        </dd>
        <dt>Attempt</dt>
        <dd className="mono uri">{a.attempt_id}</dd>
        <dt>Lease</dt>
        <dd className="mono uri">{a.lease_id}</dd>
      </dl>

      {a.error !== null && (
        <div style={{ marginTop: 10 }}>
          <pre className="err full">{a.error}</pre>
        </div>
      )}

      <AttemptResources a={a} run={run} />
      <AttemptSpend a={a} profile={run.task.runner_profile} />
      <AttemptCheckpoints a={a} run={run} />
    </section>
  )
}

/**
 * REQUESTED vs UTILISED, for one attempt.
 *
 * The requested side is `RESOURCE_CLASSES` from the frozen catalogue, served
 * by `/v1/resource-classes`. It is a route rather than a table in this
 * repository because `check-contract-parity.sh` does not cover TypeScript, so
 * a hand copy would drift the first time a class is resized and nothing would
 * notice.
 *
 * THE COMPARISON ONLY MEANS ANYTHING BECAUSE `requests == limits`. There is no
 * bursting on this platform, so the requested figure is also the ceiling: 90%
 * of it is not "well utilised", it is one chatty prompt from an OOM kill.
 *
 * THE LIVE CASE. `peak_rss_bytes` is written by `record_resource_usage` at the
 * end of an attempt, so a RUNNING agent has none and this panel would be
 * entirely em dashes for exactly the agent someone is watching because they
 * are worried about it. Every fifth heartbeat event carries a real reading of
 * the live process, so that is used when there is no final figure -- labelled
 * with its age, never presented as the final number.
 */
function AttemptResources({ a, run }: { a: AttemptRow; run: AgentRun }) {
  const { task, events, classes, classesDetail, classesRouteMissing } = run
  const cls: ResourceClassSpec | null = classes?.[task.resource_class] ?? null
  const hb = newestHeartbeat(a, events)

  const liveRss = a.peak_rss_bytes === null ? (hb?.peakRssBytes ?? null) : null
  const rss = a.peak_rss_bytes ?? liveRss
  const rssBy =
    a.peak_rss_bytes !== null
      ? 'at exit'
      : liveRss !== null && hb !== null
        ? `heartbeat ${timeAgo(hb.at)}`
        : a.started_at === null
          ? 'never ran'
          : 'not yet written'

  const diskBy =
    a.peak_disk_bytes !== null ? 'at exit' : a.started_at === null ? 'never ran' : 'not yet written'

  return (
    <div className="section" style={SUB}>
      <h2>Requested vs utilised</h2>

      {classes === null && (
        <div className="ctl-empty is-partial" role="status" style={{ marginBottom: 10 }}>
          <h3>
            {classesRouteMissing
              ? 'This API does not serve the resource-class catalogue'
              : 'The resource-class catalogue could not be read'}
          </h3>
          <p>
            {classesRouteMissing
              ? 'The deployment answering this UI predates GET /v1/resource-classes, so the ceiling these measurements were taken under is unknown to this page. The measured side below is unaffected and is real.'
              : `The measurements below are real; only the ceiling they should be read against is missing. ${classesDetail ?? ''}`}
          </p>
          <span className="ctl-empty-foot">
            The bars are hatched with no fill rather than drawn empty — an empty
            bar would claim 0% used.
          </span>
        </div>
      )}

      {classes !== null && cls === null && (
        <div className="ctl-empty is-partial" role="status" style={{ marginBottom: 10 }}>
          <h3>
            The catalogue has no class called <code>{task.resource_class}</code>
          </h3>
          <p>
            The task names a resource class the platform no longer publishes, so
            there is nothing to compare against. The class was renamed or
            removed after this task was submitted.
          </p>
        </div>
      )}

      <Util
        label={
          <>
            <b>memory</b> peak RSS
          </>
        }
        used={rss}
        ceiling={cls === null ? null : cls.memory_gib * GIB}
        fmt={bytesLabel}
        by={rssBy}
      />
      <Util
        label={
          <>
            <b>workspace</b> peak
          </>
        }
        used={a.peak_disk_bytes}
        ceiling={cls === null ? null : cls.disk_gib * GIB}
        fmt={bytesLabel}
        by={diskBy}
      />
      {/* CPU HAS NO UTILISED SIDE AT ALL. The sampler measures memory and disk
          only, so a cpu bar here would be a hatched track next to a request --
          which is the honest picture and is why it is drawn rather than left
          out: a panel headed "requested vs utilised" that silently omits a
          third of the envelope reads as if cpu were fine. */}
      <Util
        label={
          <>
            <b>cpu</b> not sampled
          </>
        }
        used={null}
        ceiling={cls === null ? null : cls.cpu}
        fmt={(v) => `${v} vCPU`}
        by="never measured"
      />

      {/* The standing rules -- requests == limits, the tmpfs workspace, what
          oom_near_miss actually asserts -- live ONCE in the legend at the end
          of this section. Four lines of them repeated under every attempt made
          a three-attempt run unreadable, and a note nobody reads is not a
          note. Only what VARIES per attempt stays here. */}
      {hb !== null && a.peak_rss_bytes === null && (
        <p className="muted small">
          The memory figure is a live reading from the newest heartbeat event on
          this page ({timeAgo(hb.at)}), not the final high-water mark — that is
          written when the attempt ends.
        </p>
      )}
    </div>
  )
}

/**
 * What this attempt SPENT.
 *
 * These five fields are typed on the attempt document precisely so a question
 * like "spend per tenant last week" can be a query rather than a scan. They
 * are null on every attempt that ran before the worker capture shipped, and
 * NULL IS NOT ZERO -- a mock task costs nothing on purpose, and an attempt
 * whose result could not be parsed cost an unknown amount. Rendering both as
 * "$0.00" lies about one of them.
 *
 * The columns are shown WITH their em dashes rather than omitted. Omitting
 * them, which the previous attempts table did, also hides the numbers on the
 * new attempts that do carry them.
 */
function AttemptSpend({ a, profile }: { a: AttemptRow; profile: string }) {
  const anything =
    a.input_tokens !== null ||
    a.output_tokens !== null ||
    a.cache_read_input_tokens !== null ||
    a.cache_creation_input_tokens !== null ||
    a.cost_usd !== null
  // Only the CLI agents go through `cliagent.run_cli_agent`, which is what
  // parses a usage block out of the runner's own output. The others report
  // nothing by construction, which is a different sentence from "this one
  // should have reported and did not".
  const reports = profile === 'claude-code' || profile === 'codex'

  if (!anything) {
    // THREE DIFFERENT REASONS, and they need three different sentences. The
    // one that was collapsed first is the middle one: a running attempt has
    // nothing yet BECAUSE IT HAS NOT FINISHED, and telling its owner the
    // numbers are missing because of an old image sends them to rebuild an
    // image over an attempt that is working correctly.
    const running = a.completed_at === null && a.started_at !== null
    return (
      <div className="section" style={SUB}>
        <h2>Tokens and cost</h2>
        <p className="muted">
          {!reports ? (
            <>
              The <code>{profile}</code> runner does not report tokens or cost at
              all. That is an absence of measurement, not a run that cost
              nothing.
            </>
          ) : running ? (
            <>
              This attempt has not finished. Spend is parsed out of the
              runner&apos;s result and written when the attempt ends, so there is
              nothing yet — nothing is wrong and nothing needs doing.
            </>
          ) : a.started_at === null ? (
            <>
              This attempt never started, so it consumed no tokens. That is a
              fact about the attempt rather than a missing measurement.
            </>
          ) : (
            <>
              This attempt finished and recorded no usage. Attempts that ran
              before the worker capture shipped in an{' '}
              <code>agent-runtime-base</code> image carry null for every field —
              an absent measurement, not a free run.
            </>
          )}
        </p>
      </div>
    )
  }

  return (
    <div className="section" style={SUB}>
      <h2>Tokens and cost</h2>
      <dl className="kv">
        <dt>Input</dt>
        <dd>{tokens(a.input_tokens)}</dd>
        <dt>Output</dt>
        <dd>{tokens(a.output_tokens)}</dd>
        <dt>Cache read</dt>
        <dd>{tokens(a.cache_read_input_tokens)}</dd>
        <dt>Cache write</dt>
        <dd>{tokens(a.cache_creation_input_tokens)}</dd>
        <dt>Cost</dt>
        <dd>{usd(a.cost_usd)}</dd>
      </dl>
    </div>
  )
}

/**
 * Every checkpoint this attempt wrote.
 *
 * TWO RECORDS, NEITHER COMPLETE. `attempt.checkpoints` is `list[str]` -- ids
 * and nothing else. The `checkpoint_completed` event carries the uri and the
 * size. So a row with an id and no location is the ordinary paging case, not a
 * broken checkpoint, and the table says so per row instead of drawing a blank.
 *
 * CONTENTS ARE NOT RECORDED ANYWHERE. Nothing writes a manifest of what is in
 * a checkpoint archive, so this panel cannot list files and does not pretend
 * to. Reporting that as a gap is the honest move; a placeholder file tree
 * would be the dishonest one.
 */
function AttemptCheckpoints({ a, run }: { a: AttemptRow; run: AgentRun }) {
  const rows = checkpointsFor(a, run.events)
  const restored = restoredFrom(a, run.events)
  const eventsRead = run.events !== null
  const missingLocation = rows.filter((r) => r.uri === null).length

  return (
    <div className="section" style={{ ...SUB, marginBottom: 0 }}>
      <h2>
        Checkpoints
        <span className="count-chip">{rows.length}</span>
      </h2>

      {restored !== null && (
        <p className="muted">
          Resumed from <code>{restored.checkpoint_id}</code>
          {restored.from_attempt !== null && (
            <>
              , written by attempt <span className="mono">{restored.from_attempt}</span>
            </>
          )}
          {restored.files !== null && <> · {plural(restored.files, 'file')} restored</>}
          {restored.bytes !== null && <> · {bytesLabel(restored.bytes)}</>}. This attempt did not
          start from an empty workspace.
        </p>
      )}

      {rows.length === 0 ? (
        <p className="muted">
          This attempt&apos;s document lists no checkpoint.{' '}
          {a.started_at === null
            ? 'It never started, so there was nothing to checkpoint.'
            : 'Checkpointing is periodic, so an attempt shorter than one interval legitimately writes none.'}
          {!eventsRead && ' The event read failed, so a checkpoint recorded only in an event would not be visible here either.'}
        </p>
      ) : (
        <div className="ctl-table">
          <table>
            <thead>
              <tr>
                <th scope="col">Checkpoint</th>
                <th scope="col" className="is-num">Size</th>
                <th scope="col">Location</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.checkpoint_id}>
                  <th scope="row">
                    {r.checkpoint_id}
                    {r.at !== null && <span className="ctl-sub">{timeAgo(r.at)}</span>}
                    {r.eventOnly && (
                      <span className="ctl-sub">not on the attempt document</span>
                    )}
                  </th>
                  <td className="is-num">{r.bytes === null ? <Em /> : bytesLabel(r.bytes)}</td>
                  <td>
                    {r.uri === null ? (
                      <span className="muted">— its event is not on this page</span>
                    ) : (
                      <>
                        <span className="mono uri">{r.uri}</span>
                        {/* No signed URL is minted here, by design. The reader
                            uses their own credentials against GCS, which keeps
                            the tenant boundary in one place. */}
                        <button
                          className="copy"
                          onClick={() => navigator.clipboard?.writeText(`gsutil cp ${r.uri} .`)}
                        >
                          copy gsutil
                        </button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
            <caption>
              Ids come from the attempt document; size and location come from
              each checkpoint&apos;s own event.
              {missingLocation > 0 &&
                ` ${missingLocation} of ${rows.length} have no event on this page — the events route is oldest-first, capped, and returns no page token, so a long attempt's later checkpoints fall off the end.`}
            </caption>
          </table>
        </div>
      )}
    </div>
  )
}

/**
 * `task.latest_checkpoint` is a URI on the task, updated by every checkpoint.
 * When it does not match anything the attempt documents list, that is worth
 * saying: it is the pointer a resumed attempt would restore from, and a
 * mismatch means the record it points at is not on this page.
 */
function LatestCheckpointNote({ run, attempts }: { run: AgentRun; attempts: AttemptRow[] }) {
  const latest = run.task.latest_checkpoint
  if (latest === null) return null
  const known = attempts.some((a) =>
    checkpointsFor(a, run.events).some((r) => r.uri === latest),
  )
  if (known) return null
  return (
    <p className="muted small">
      The task&apos;s restore pointer is{' '}
      <span className="mono uri">{latest}</span>, which no checkpoint listed
      above matches. Either its event is off this page or it was written by an
      attempt whose document did not come back — it is not evidence the
      checkpoint is gone.
    </p>
  )
}

// ---------------------------------------------------------------------------
// Output
// ---------------------------------------------------------------------------

function Output({ run }: { run: AgentRun }) {
  const { task, attempts } = run
  const summary = (task.result_summary ?? null) as ResultSummary | null
  // `result_summary` is an untyped Firestore dict: `ResultSummary` describes
  // what the worker writes, not what the field is guaranteed to hold. A
  // document written by an older worker -- or by a test fixture -- can carry
  // `artifacts: 1`, and `(x ?? []) as ArtifactRef[]` would hand that straight
  // to `.map` and blank the page with a runtime error. Shape is CHECKED, and
  // a value of the wrong shape is reported rather than silently read as none.
  const artifactsRaw = summary?.artifacts
  const artifacts: ArtifactRef[] = Array.isArray(artifactsRaw) ? (artifactsRaw as ArtifactRef[]) : []
  const artifactsMalformed = artifactsRaw !== undefined && !Array.isArray(artifactsRaw)
  const logsRaw = summary?.logs
  const logs: Record<string, string> =
    typeof logsRaw === 'object' && logsRaw !== null && !Array.isArray(logsRaw)
      ? (logsRaw as Record<string, string>)
      : {}
  const terminal = TERMINAL_STATES.has(task.state)

  return (
    <section className="section panel">
      <h2>Output</h2>

      {/* THE SCOPE LINE IS NOT OPTIONAL. Everything in this panel comes from
          result_summary, which finish() writes ONCE at terminal state. On a
          retried task it is the last attempt's output and nothing else, and a
          panel that does not say so gets read as the run's. */}
      {attempts !== null && attempts.length > 1 && (
        <p className="muted small" style={{ marginTop: 0, marginBottom: 10 }}>
          Everything here describes the LAST attempt only. The result summary is
          written once, at terminal state; the earlier attempts&apos; output was
          never summarised anywhere.
        </p>
      )}

      {!terminal ? (
        <Absent kind="zero" heading="Nothing has been written yet">
          A result summary is written only when an attempt finishes. This agent
          has not finished, so there is nothing here — which is not the same as
          producing nothing.
        </Absent>
      ) : summary === null ? (
        <Absent kind="partial" heading="Finished with no result summary">
          This agent reached <code>{task.state}</code> without one. A parked
          attempt puts its summary in the event detail and in{' '}
          <code>blocked_by</code> instead, so the timeline below is where to
          look.
        </Absent>
      ) : (
        <>
          <GitOutcome git={summary.git} artifacts={artifacts} />
          <Artifacts artifacts={artifacts} summary={summary} malformed={artifactsMalformed} />
          <Logs logs={logs} />
          <SummaryUsage task={task} attempts={attempts} />
        </>
      )}
    </section>
  )
}

function Artifacts({
  artifacts,
  summary,
  malformed,
}: {
  artifacts: ArtifactRef[]
  summary: ResultSummary
  /** `artifacts` was present but not an array. Not the same as none. */
  malformed: boolean
}) {
  const skippedRaw = summary.artifacts_skipped
  const skipped = Array.isArray(skippedRaw) ? skippedRaw : []

  if (malformed) {
    return (
      <div className="section">
        <h2>Artifacts</h2>
        <Absent kind="partial" heading="The artifact list is not a list">
          This result summary records an <code>artifacts</code> field that is not
          an array, so nothing here can be listed. The attempt may well have
          uploaded files — this is a malformed record, not an empty one.
        </Absent>
      </div>
    )
  }

  return (
    <div className="section" style={SUB}>
      <h2>
        Artifacts
        <span className="count-chip">{plural(artifacts.length, 'file')}</span>
      </h2>
      {artifacts.length === 0 ? (
        <p className="muted">
          This attempt uploaded no artifacts. The read succeeded — the list is
          empty, not missing.
        </p>
      ) : (
        <div className="ctl-table">
          <table>
            <thead>
              <tr>
                <th scope="col">Artifact</th>
                <th scope="col" className="is-num">Size</th>
                <th scope="col">Location</th>
              </tr>
            </thead>
            <tbody>
              {artifacts.map((a) => (
                <tr key={a.uri}>
                  <th scope="row">{a.name}</th>
                  <td className="is-num">{bytesLabel(a.bytes)}</td>
                  {/* Passed by reference. No download URL is minted here -- the
                      reader uses their own credentials against GCS, which keeps
                      the tenant boundary in one place. */}
                  <td>
                    <span className="mono uri">{a.uri}</span>
                    <button
                      className="copy"
                      onClick={() => navigator.clipboard?.writeText(`gsutil cp ${a.uri} .`)}
                    >
                      copy gsutil
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {skipped.length > 0 && (
        <p className="warn-text">
          {plural(skipped.length, 'artifact')} {skipped.length === 1 ? 'was' : 'were'} skipped for
          exceeding the size cap, so this list is incomplete: {skipped.join(', ')}
        </p>
      )}
    </div>
  )
}

function Logs({ logs }: { logs: Record<string, string> }) {
  const entries = Object.entries(logs)

  return (
    <div className="section" style={SUB}>
      <h2>Logs</h2>
      {entries.length === 0 ? (
        <p className="muted">
          No log stream was uploaded for this attempt. A stream that failed to
          upload is absent from this list rather than recorded as empty.
        </p>
      ) : (
        <dl className="kv">
          {entries.map(([label, uri]) => (
            <div key={label} style={{ display: 'contents' }}>
              <dt>{label}</dt>
              <dd className="mono uri">
                {uri}
                <button
                  className="copy"
                  onClick={() => navigator.clipboard?.writeText(`gsutil cat ${uri}`)}
                >
                  copy gsutil
                </button>
              </dd>
            </div>
          ))}
        </dl>
      )}
      <p className="muted small">
        These are the files uploaded when the attempt ended. Nothing streams
        while an agent is running: there is no log-tail read path on the API,
        so a running agent&apos;s output is not readable here at all.
      </p>
    </div>
  )
}

/**
 * The untyped `result_summary.runner.usage`, shown only when the typed
 * per-attempt fields have nothing.
 *
 * It is the OLD home for these numbers -- an untyped dict no index can reach.
 * The attempt fields replaced it, so preferring them and falling back here
 * means an attempt that predates the typed capture still shows its spend
 * instead of five em dashes.
 */
function SummaryUsage({ task, attempts }: { task: Task; attempts: AttemptRow[] | null }) {
  const usage = usageOf(task)
  const typed = attempts !== null && attempts.some((a) => a.cost_usd !== null || a.input_tokens !== null)
  if (usage === null || typed) return null

  const n = (k: string) => (typeof usage[k] === 'number' ? (usage[k] as number) : null)
  const models = usage['models']

  return (
    <div className="section">
      <h2>Spend, from the result summary</h2>
      <p className="muted">
        No attempt document carries a typed spend figure, but the last
        attempt&apos;s result summary does. These come from an untyped dict that
        no query can reach, and they describe the last attempt only.
      </p>
      <dl className="kv">
        <dt>Input</dt>
        <dd>{tokens(n('input_tokens'))}</dd>
        <dt>Output</dt>
        <dd>{tokens(n('output_tokens'))}</dd>
        <dt>Cache read</dt>
        <dd>{tokens(n('cache_read_input_tokens'))}</dd>
        <dt>Cost</dt>
        <dd>{usd(n('total_cost_usd'))}</dd>
        {Array.isArray(models) && (
          <>
            <dt>Models</dt>
            <dd>{(models as string[]).join(', ')}</dd>
          </>
        )}
      </dl>
    </div>
  )
}

/**
 * What happened to the code, if the task had any.
 *
 * WHY THIS PANEL LEADS WITH A REASON RATHER THAN A LINK. A pull request is
 * absent for at least six different causes, and they need completely different
 * responses from the person reading this:
 *
 *   the tenant's token has no push permission   -> grant write scope
 *   the attempt parked                          -> wait; it is not finished
 *   the agent changed nothing                   -> the run did nothing useful
 *   the host is not a forge we can publish to   -> apply the patch by hand
 *   the push was rejected                       -> something else moved the branch
 *   the pull request call failed                -> the branch IS pushed; retry the PR
 *
 * The last one is the one most worth separating: `published: true` with no
 * `pull_request` means the work reached the forge and only the final API call
 * did not, so the response is to open the PR from a branch that already
 * exists. Collapsing it into "no pull request" sends someone to re-run an
 * agent whose output is already pushed.
 *
 * THE CLASSIFIER KEYS ON STRUCTURED FIELDS -- `published`, `can_push`,
 * `pull_request`, `branch` -- and only falls back to matching the free-text
 * `publish_reason` for the two causes that have no structured signal. An
 * unrecognised reason is NOT forced into a bucket: it is printed verbatim and
 * labelled as one this screen cannot advise on, the same discipline
 * `fetch.ts::classify` uses for an unrecognised 403.
 *
 * DIRTY FILES ARE SHOWN EVEN WHEN THERE ARE NO COMMITS, because that is the
 * common case: most agents edit files and never run `git commit`. A panel that
 * only counted commits would report "no changes" for the majority of real runs.
 */
function GitOutcome({ git, artifacts }: { git: GitSummary | undefined; artifacts: ArtifactRef[] }) {
  // Same untyped-dict caution as the artifact list: `GitSummary` describes what
  // `_harvest_git` writes, and the field is whatever is in Firestore.
  if (typeof git !== 'object' || git === null || Array.isArray(git)) return null

  const commits = Array.isArray(git.commits) ? git.commits : []
  const dirty = Array.isArray(git.dirty) ? git.dirty : []
  const pr = git.pull_request
  // The patch is an ordinary artifact; matching by name is what turns the
  // recorded name into the GCS uri without minting a second copy of it.
  const patch = git.patch ? artifacts.find((a) => a.name === git.patch) : undefined

  return (
    <div className="section panel git-outcome">
      <h2>Code</h2>

      {git.error ? (
        <p className="warn-text">
          The change could not be read from the workspace: {git.error}. This says
          nothing about whether the agent did work — only that git could not be
          asked.
        </p>
      ) : null}

      <PublishOutcome git={git} />

      <dl className="kv">
        <dt>Commits</dt>
        <dd>
          {commits.length === 0 ? (
            <span className="muted">
              none
              {dirty.length > 0 && ' — the agent edited files without committing'}
            </span>
          ) : (
            <>
              {git.commit_count ?? commits.length} on top of{' '}
              <span className="mono">{git.base ? git.base.slice(0, 10) : '—'}</span>
              {typeof git.insertions === 'number' && typeof git.deletions === 'number' && (
                <span className="muted small">
                  {' '}
                  · +{git.insertions} −{git.deletions}
                </span>
              )}
            </>
          )}
        </dd>

        {dirty.length > 0 && (
          <>
            <dt>Uncommitted</dt>
            <dd>
              {plural(git.dirty_count ?? dirty.length, 'file')}
              {git.dirty_truncated && <span className="muted small"> · list truncated</span>}
            </dd>
          </>
        )}

        <dt>Patch</dt>
        <dd>
          {patch ? (
            <>
              <span className="mono uri">{patch.uri}</span>
              <button
                className="copy"
                onClick={() => navigator.clipboard?.writeText(`gsutil cat ${patch.uri} | git apply -`)}
              >
                copy apply
              </button>
            </>
          ) : git.patch_omitted ? (
            <span className="warn-text">
              discarded at {num(git.patch_bytes)} bytes — over the cap. It was not
              truncated: a truncated patch applies cleanly and silently drops the
              rest of the change.
            </span>
          ) : (
            <span className="muted">none — nothing differed from the clone</span>
          )}
        </dd>

        {git.branch && (
          <>
            <dt>Branch</dt>
            <dd className="mono">
              {git.branch}
              {git.pushed_head && (
                <span className="muted small"> · {git.pushed_head.slice(0, 10)}</span>
              )}
            </dd>
          </>
        )}

        {git.repository && (
          <>
            <dt>Repository</dt>
            <dd className="mono uri">{git.repository}</dd>
          </>
        )}
      </dl>

      {pr && (
        <p className="muted" style={{ marginTop: 10 }}>
          <a href={pr.url} target="_blank" rel="noreferrer">
            #{pr.number}
          </a>{' '}
          <span className={`tag ${pr.state === 'open' ? 'ok' : 'wait'}`}>{pr.state}</span>
          {pr.created === false && <span className="muted small"> · already existed, reused</span>}
        </p>
      )}

      {git.auto_committed && (
        <p className="muted small">
          One commit on this branch was made by the worker, not the agent: the
          agent left changes uncommitted and they would otherwise not have
          reached the branch at all.
        </p>
      )}

      {commits.length > 0 && (
        <div className="ctl-table" style={{ marginTop: 10 }}>
          <table>
            <thead>
              <tr>
                <th scope="col">Commit</th>
                <th scope="col">Subject</th>
                <th scope="col" className="is-num">Files</th>
                <th scope="col" className="is-num">+/−</th>
              </tr>
            </thead>
            <tbody>
              {commits.map((c) => (
                <tr key={c.sha}>
                  <th scope="row" className="mono">
                    {c.sha.slice(0, 10)}
                  </th>
                  <td>{c.subject}</td>
                  <td className="is-num">
                    {c.files_changed}
                    {/* git prints `-` for both counts on a binary change; those
                        are counted separately rather than folded into a 0/0
                        line count that would read as "changed nothing". */}
                    {c.binary_files > 0 && (
                      <span className="ctl-sub">{c.binary_files} binary</span>
                    )}
                  </td>
                  <td className="is-num">
                    +{c.insertions} −{c.deletions}
                  </td>
                </tr>
              ))}
            </tbody>
            {(git.commit_count ?? 0) > commits.length && (
              <caption>
                {git.commit_count} commits were made; the newest {commits.length} are listed. The
                patch above carries all of them.
              </caption>
            )}
          </table>
        </div>
      )}
    </div>
  )
}

/** The six causes, each with the response it actually needs. */
function PublishOutcome({ git }: { git: GitSummary }) {
  const reason = git.publish_reason ?? null
  const lower = (reason ?? '').toLowerCase()

  // 1. There is a pull request. Nothing to explain.
  if (git.pull_request) return null

  // 2. PUSHED, NO PULL REQUEST. The branch is on the forge; only the final API
  //    call did not land. Re-running the agent would be the wrong response.
  if (git.published === true) {
    return (
      <div className="ctl-empty is-partial" role="status" style={{ marginBottom: 10 }}>
        <h3>The branch is pushed; no pull request was opened</h3>
        <p>
          The work reached the forge on{' '}
          <span className="mono">{git.branch ?? 'the branch below'}</span>. Only
          the pull-request call did not complete, so opening one by hand from
          that branch is all that is left — re-running this agent would duplicate
          work that already exists.
        </p>
        {reason !== null && <span className="ctl-empty-foot">{reason}</span>}
      </div>
    )
  }

  // 3. The tenant's token cannot push. EXPECTED TODAY, and a fact rather than a
  //    failure: the secret holds a clone token, the forge was asked and said
  //    no. The patch is the deliverable.
  if (git.can_push === false) {
    return (
      <div className="ctl-empty" style={{ marginBottom: 10 }}>
        <h3>Not published: this token cannot write to the repository</h3>
        <p>
          The forge was asked and refused write access, so the patch below is the
          deliverable. Nothing failed — granting the tenant&apos;s credential
          write scope is what changes this.
        </p>
        {reason !== null && <span className="ctl-empty-foot">{reason}</span>}
      </div>
    )
  }

  if (reason === null) {
    return (
      <div className="ctl-empty is-partial" role="status" style={{ marginBottom: 10 }}>
        <h3>No pull request, and no reason was recorded</h3>
        <p>
          The worker writes <code>publish_reason</code> on every path, including
          the successful ones, so its absence means this summary was written by
          something other than the publish step.
        </p>
      </div>
    )
  }

  // 4-6. No structured signal beyond `published: false`, so the free-text
  //      reason is matched -- carefully. An unrecognised reason is printed
  //      verbatim and labelled as one, never forced into a bucket.
  const known: { match: string; heading: string; body: ReactNode } | undefined = [
    {
      match: 'changed nothing',
      heading: 'Not published: the agent changed nothing',
      body: (
        <>
          The workspace was identical to the clone, so there was nothing to push.
          This is a statement about the run, not about publishing — the agent did
          no work on the repository.
        </>
      ),
    },
    {
      match: 'parked',
      heading: 'Not published: this attempt parked',
      body: (
        <>
          Publishing waits for the run to finish, and this attempt did not finish
          — it checkpointed and released its capacity. The next attempt resumes
          from the checkpoint and publishes then. Nothing is lost and nothing
          needs doing.
        </>
      ),
    },
    {
      match: 'not on a forge',
      heading: 'Not published: this host is not a forge we can publish to',
      body: (
        <>
          The repository is not on a forge this worker knows how to open a pull
          request against, so the patch below is the deliverable — apply it by
          hand.
        </>
      ),
    },
    {
      match: 'could not reach the forge',
      heading: 'Not published: the forge did not answer',
      body: (
        <>
          The forge could not be reached at all, so nothing is known about
          whether publishing would have worked. This says nothing about the
          agent&apos;s work, which is in the patch below.
        </>
      ),
    },
    {
      match: 'publishing is disabled',
      heading: 'Not published: publishing is off for this worker',
      body: <>A configuration decision, not a failure. The patch below is the deliverable.</>,
    },
    {
      match: 'repository url is unknown',
      heading: 'Not published: the repository URL is unknown',
      body: (
        <>
          The attempt had no repository to publish to. If the task was meant to
          have one, it was submitted without <code>repository_url</code>.
        </>
      ),
    },
  ].find((c) => lower.includes(c.match))

  if (known !== undefined) {
    return (
      <div className="ctl-empty" style={{ marginBottom: 10 }}>
        <h3>{known.heading}</h3>
        <p>{known.body}</p>
        <span className="ctl-empty-foot">{reason}</span>
      </div>
    )
  }

  // The push itself was rejected -- a GitError, so the reason is git's own
  // message. This is the "something else moved the branch" case and it is the
  // one that needs a person to look at the branch.
  return (
    <div className="ctl-empty is-partial" role="status" style={{ marginBottom: 10 }}>
      <h3>Not published, and the reason is git&apos;s own</h3>
      <p>
        The push did not land. A rejected push usually means something else
        moved the branch — this screen does not recognise the message well
        enough to say which, so it is reproduced exactly as the worker recorded
        it.
      </p>
      <span className="ctl-empty-foot">{reason}</span>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Input
// ---------------------------------------------------------------------------

/**
 * What this run was asked to do.
 *
 * `input` is a free-form dict: the API accepts it whole and each runner reads
 * what it needs. `input.prompt` is the CONVENTION the CLI agents and the mock
 * runner use, not a schema field, so it is surfaced as the prompt when it is a
 * non-empty string and the whole object is still shown underneath. A screen
 * that only rendered `prompt` would show nothing for a runner that names it
 * differently.
 */
function Input({ task }: { task: Task }) {
  const input = task.input
  const record = typeof input === 'object' && input !== null && !Array.isArray(input)
    ? (input as Record<string, unknown>)
    : null
  const promptValue = record?.['prompt']
  const prompt = typeof promptValue === 'string' && promptValue.trim() !== '' ? promptValue : null
  const metadata = task.metadata

  return (
    <section className="section">
      <h2>Input</h2>

      <dl className="kv">
        <dt>Runner profile</dt>
        <dd>{task.runner_profile}</dd>
        <dt>Resource class</dt>
        <dd>{task.resource_class}</dd>
        <dt>Provider</dt>
        <dd>{task.provider ?? <Em />}</dd>
        <dt>Model</dt>
        {/* Recorded for attribution. It selects NOTHING about the container --
            image, command and resource spec all come from the profile. */}
        <dd>{task.model ?? <Em />}</dd>
        <dt>Priority</dt>
        <dd>{task.priority}</dd>
        <dt>Repository</dt>
        <dd className="uri">
          {task.repository_url ?? <Em />}
          {task.repository_ref && ` @ ${task.repository_ref}`}
        </dd>
        <dt>Timeout</dt>
        <dd>{task.timeout_seconds !== null ? `${task.timeout_seconds}s` : <Em />}</dd>
      </dl>

      <div className="section" style={SUB_AFTER_KV}>
        <h2>Prompt</h2>
        {prompt === null ? (
          <p className="muted">
            This task&apos;s input has no <code>prompt</code> string.{' '}
            {record === null
              ? 'Its input is not an object at all.'
              : 'That is legitimate — the field is a convention of the CLI and mock runners, not part of the submission schema. The full input is below.'}
          </p>
        ) : (
          <pre className="json" style={{ maxHeight: 320, overflowY: 'auto' }}>
            {prompt}
          </pre>
        )}
      </div>

      <div className="section" style={SUB}>
        <h2>Metadata</h2>
        {metadata === null ? (
          <p className="muted">
            The task document carried no metadata field at all — which is
            different from being submitted with none.
          </p>
        ) : Object.keys(metadata).length === 0 ? (
          <p className="muted">
            Submitted with no metadata. The read succeeded and the object is
            empty — a real zero.
          </p>
        ) : (
          <div className="ctl-table">
            <table>
              <thead>
                <tr>
                  <th scope="col">Key</th>
                  <th scope="col">Value</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(metadata).map(([k, v]) => (
                  <tr key={k}>
                    <th scope="row" className="mono">{k}</th>
                    {/* Metadata is arbitrary, so a nested object is stringified
                        rather than rendered -- React would throw on an object
                        child, and "[object Object]" is worse than the JSON. */}
                    <td>{typeof v === 'string' ? v : JSON.stringify(v)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {input !== null && input !== undefined && (
        <div className="section" style={SUB}>
          <h2>Full input</h2>
          <pre className="json" style={{ maxHeight: 320, overflowY: 'auto' }}>
            {JSON.stringify(input, null, 2)}
          </pre>
        </div>
      )}
    </section>
  )
}

// ---------------------------------------------------------------------------
// Timeline
// ---------------------------------------------------------------------------

interface Group {
  key: string
  /** null for the pre-admission group, and for events naming an unknown attempt. */
  attempt: AttemptRow | null
  label: string
  events: TaskEvent[]
}

/**
 * The event stream, grouped under the attempt that wrote it.
 *
 * A flat list interleaves three attempts into one column where only the
 * timestamps separate them, so what was different about the two that failed
 * cannot be read off it. Every event carries `attempt_id` (`_event_to_api`),
 * so the grouping is the API's own rather than a guess.
 */
function Timeline({
  events,
  detail,
  attempts,
}: {
  events: TaskEvent[] | null
  detail: string | null
  attempts: AttemptRow[] | null
}) {
  if (events === null) {
    return (
      <section className="section">
        <h2>Timeline</h2>
        <Absent kind="failed" heading="The event history could not be read">
          This is a failed read, not an empty history. {detail}
        </Absent>
      </section>
    )
  }

  if (events.length === 0) {
    // THERE IS NO LEGITIMATE EMPTY STATE HERE, and this is where the whole
    // rule pays for itself. create_tasks writes the task document and its
    // `submitted` event in the SAME BATCH, explicitly so a partially written
    // submission cannot leave a task with no event trail. So a task that
    // exists has at least one event, and a 200 with zero rows is a failed
    // query wearing a success code.
    return (
      <section className="section">
        <h2>Timeline</h2>
        <Absent kind="failed" heading="Timeline unavailable">
          This task must have at least a <code>submitted</code> event — it is
          written in the same batch as the task itself — and the query returned
          none. This is a failed read, not an empty history.
        </Absent>
      </section>
    )
  }

  // The endpoint orders `at` ASCENDING, applies the page limit and returns NO
  // page token, so a task with more than 200 events hands back the OLDEST 200
  // and the newest are unreachable. That is not a corner case: the worker
  // heartbeats throughout, so a long attempt writes its way past the cap and
  // the end of the timeline -- the part the page was opened for -- is exactly
  // the part the API drops.
  const truncated = events.length >= 200
  const groups = grouped(events, attempts)

  return (
    <section className="section panel">
      <h2>
        Timeline
        <span className="count-chip">{plural(events.length, 'event')}</span>
      </h2>

      {truncated && (
        <div className="ctl-empty is-partial" role="status" style={{ marginBottom: 10 }}>
          <h3>Showing the oldest 200 events</h3>
          <p>
            Newer events are not reachable through this endpoint yet — it orders
            oldest-first, caps the page and returns no page token. The end of
            this task&apos;s history is missing, not absent.
          </p>
        </div>
      )}

      {groups.map((g) => (
        <div className="section panel" key={g.key}>
          <h2>
            {g.label}
            <span className="count-chip">{plural(g.events.length, 'event')}</span>
          </h2>
          {g.events.length === 0 ? (
            <p className="muted">
              No event on this page belongs here — the page ends before them, or
              none were written.
            </p>
          ) : (
            <ol className="timeline">
              {g.events.map((e) => (
                <li key={e.event_id}>
                  <span className="ev-type">{e.type}</span>
                  <span className="ev-at">{timeAgo(e.at)}</span>
                  {/* The fencing generation the event was written under. A
                      stale worker's events carry the OLD one -- that is how a
                      reclaim reads here. */}
                  {e.generation !== null && (
                    <span className="ev-gen" title="Fencing generation for this event">
                      gen {e.generation}
                    </span>
                  )}
                  {/* Only the reconciler labels itself. THE TEST IS THE VALUE,
                      NOT THE KEY: the worker's quota_exhausted events also
                      carry a detail.source, describing where the quota signal
                      came from, so a presence check badges them as reconciler
                      work. */}
                  {e.detail?.['source'] === 'reconciler' && (
                    <span className="ev-badge">reconciler</span>
                  )}
                  {e.detail && Object.keys(e.detail).length > 0 && (
                    <pre className="ev-detail">{JSON.stringify(e.detail, null, 2)}</pre>
                  )}
                </li>
              ))}
            </ol>
          )}
        </div>
      ))}
    </section>
  )
}

/**
 * Events under their attempt, oldest attempt first.
 *
 * `attempts === null` means the attempt read failed, so nothing can be grouped
 * by attempt without inventing the grouping — the events are shown as one
 * stream and the heading says why.
 */
function grouped(events: TaskEvent[], attempts: AttemptRow[] | null): Group[] {
  if (attempts === null) {
    return [
      {
        key: '@ungrouped',
        attempt: null,
        label: 'Every event (attempts unread, so they cannot be grouped)',
        events,
      },
    ]
  }

  const byAttempt = new Map<string, TaskEvent[]>()
  const preface: TaskEvent[] = []
  for (const e of events) {
    if (e.attempt_id === null) preface.push(e)
    else {
      const list = byAttempt.get(e.attempt_id)
      if (list) list.push(e)
      else byAttempt.set(e.attempt_id, [e])
    }
  }

  const groups: Group[] = []
  if (preface.length > 0) {
    groups.push({ key: '@preface', attempt: null, label: 'Before any attempt', events: preface })
  }
  const ordered = [...attempts].sort((x, y) => x.created_at.localeCompare(y.created_at))
  ordered.forEach((a, i) => {
    groups.push({
      key: a.attempt_id,
      attempt: a,
      label: `Attempt ${i + 1} · generation ${a.generation}`,
      events: byAttempt.get(a.attempt_id) ?? [],
    })
    byAttempt.delete(a.attempt_id)
  })
  // What is left names an attempt no document on this page describes. Folding
  // these into the preface would file real attempt events under "before any
  // attempt", which is a lie about when they happened.
  for (const [id, list] of byAttempt) {
    groups.push({ key: id, attempt: null, label: `Attempt ${id} · no attempt document`, events: list })
  }
  return groups
}
