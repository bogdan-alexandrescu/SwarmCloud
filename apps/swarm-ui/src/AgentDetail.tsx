import { useCallback, useState, type CSSProperties, type ReactNode } from 'react'
import { loadAgentRun, type AgentRun } from './api'
import { ArtifactViewer } from './ArtifactViewer'
import { DispatchFacts } from './Dispatch'
import { num } from './fetch'
import { LivenessBadge } from './Liveness'
import { Screen, timeAgo } from './Shell'
import { StopRun } from './StopRun'
import {
  GIB,
  REASON_COPY,
  TERMINAL_STATES,
  attemptOutcome,
  attemptRan,
  bytesLabel,
  checkpointsFor,
  dispatchOf,
  elapsed,
  newestHeartbeat,
  reasonCopy,
  restoredFrom,
  stateGlyph,
  stateTone,
  usageOf,
  whyAgent,
  type ArtifactRef,
  type AttemptRow,
  type DispatchRole,
  type DispatchStrategy,
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
  // `Screen` owns its own retry nonce and does not expose it to children, so
  // the stop control needs one of its own: bumping this re-keys `Screen`,
  // which remounts it and re-runs the load. It is in the key rather than in a
  // prop for the reason the comment below gives -- `Screen`'s effect depends
  // on its nonce alone, so nothing short of a remount refetches.
  const [reloads, setReloads] = useState(0)
  const reload = useCallback(() => setReloads((n) => n + 1), [])

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
        key={`${taskId}:${reloads}`}
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
        {(r) => <Run run={r} reload={reload} />}
      </Screen>
    </div>
  )
}

function Run({ run, reload }: { run: AgentRun; reload: () => void }) {
  const { task, events } = run
  const now = Date.now()

  return (
    <>
      <Headline run={run} now={now} reload={reload} />
      <Alerts task={task} />
      <RunMetrics run={run} now={now} />
      <Why task={task} />
      {task.last_error && <ErrorBanner text={task.last_error} />}
      <Attempts run={run} now={now} />
      <DispatchPanel task={task} />
      <Output run={run} />
      <Input run={run} />
      <Timeline task={task} events={events} detail={run.eventsDetail} attempts={run.attempts} />
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

function Headline({
  run,
  now,
  reload,
}: {
  run: AgentRun
  now: number
  reload: () => void
}) {
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
        {/* B28. The route has existed and worked since it was written and
            nothing in this app called it, so an operator watching an agent
            spend on the wrong thing had to leave the console to stop it. The
            control confirms first and every claim the confirmation makes is
            pinned by tests/unit/control_plane/test_cancel_semantics.py. */}
        <span className="run-stop">
          <StopRun task={task} what="this agent" reload={reload} />
        </span>
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
        <Metric
          label="Elapsed"
          value={el.text}
          sub={elapsedNote(task)}
          foot="from the task document, which loaded"
        />
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
  const noCeiling = ceilingNote(run)
  const measured = attempts.filter((a) => a.peak_rss_bytes !== null)
  const peak = measured.length === 0 ? null : Math.max(...measured.map((a) => a.peak_rss_bytes ?? 0))
  const spent = attempts.filter((a) => a.cost_usd !== null)
  const cost = spent.length === 0 ? null : spent.reduce((t, a) => t + (a.cost_usd ?? 0), 0)
  // EACH HALF IS SUMMED SEPARATELY. `record_spend` writes only the keys the
  // runner actually reported, so an attempt can carry input tokens and no
  // output. Selecting on "either field is set" and then summing
  // `(input ?? 0) + (output ?? 0)` counted that missing half as a zero and
  // captioned the tile "in + out" -- the absent-measurement-as-zero rule
  // broken one level above the tiles that keep it. A half no attempt reported
  // is now left out of the total, and the caption says which halves are in it.
  const withIn = attempts.filter((a) => a.input_tokens !== null)
  const withOut = attempts.filter((a) => a.output_tokens !== null)
  const tokIn = withIn.length === 0 ? null : withIn.reduce((t, a) => t + (a.input_tokens ?? 0), 0)
  const tokOut = withOut.length === 0 ? null : withOut.reduce((t, a) => t + (a.output_tokens ?? 0), 0)
  const ckpts = attempts.reduce((t, a) => t + a.checkpoints.length, 0)
  const nearMiss = attempts.some((a) => a.oom_near_miss)

  return (
    <div className="ctl-metrics">
      <Metric label="Elapsed" value={el.text} sub={elapsedNote(task)} />
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
            noCeiling ??
            (cls === null
              ? 'The ceiling for this class is unknown.'
              : `of ${cls.memory_gib} GiB granted · worst of ${plural(measured.length, 'measured attempt')}`)
          }
          foot={nearMiss ? 'OOM near miss on at least one attempt' : undefined}
          tone={nearMiss ? 'alert' : undefined}
        />
      )}
      {tokIn === null && tokOut === null ? (
        <Metric
          label="Tokens"
          value="not reported"
          tone="absent"
          sub="No attempt reported a token count. Not the same as a run that used none."
        />
      ) : (
        <Metric
          label="Tokens"
          value={((tokIn ?? 0) + (tokOut ?? 0)).toLocaleString()}
          unit={
            tokIn !== null && tokOut !== null
              ? 'in + out'
              : tokIn !== null
                ? 'input only'
                : 'output only'
          }
          sub={tokenRollupNote(attempts.length, withIn.length, withOut.length)}
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

/**
 * Why there is no ceiling to read a measurement against -- or null when there
 * is one.
 *
 * `classes[task.resource_class]` is null for THREE different reasons and only
 * two of them are read failures: the catalogue read failed, this deployment
 * has no catalogue route at all, or the catalogue came back perfectly well and
 * does not contain the class this task names. The third is a class that was
 * renamed or retired after the task was submitted, and reporting it as a read
 * failure sends its reader to check an API that is answering correctly.
 * `AttemptResources` draws the same three as panels; this is the one-line form
 * a metric tile can carry.
 */
function ceilingNote(run: AgentRun): string | null {
  const { task, classes, classesRouteMissing } = run
  if (classes === null) {
    return classesRouteMissing
      ? 'This API does not serve the resource-class catalogue, so the ceiling is unknown.'
      : 'The resource-class catalogue could not be read, so the ceiling is unknown.'
  }
  if (classes[task.resource_class] === undefined) {
    return `The catalogue has no class called ${task.resource_class} — it was renamed or retired, so there is no ceiling to compare with.`
  }
  return null
}

/** Which halves of the token total were reported, and by how many attempts. */
function tokenRollupNote(total: number, withIn: number, withOut: number): string {
  if (withIn === total && withOut === total) {
    return `Both halves, summed over all ${plural(total, 'attempt')}.`
  }
  const head =
    withIn === 0
      ? 'No attempt reported input'
      : `Input from ${withIn} of ${plural(total, 'attempt')}`
  const tail = withOut === 0 ? 'none reported output' : `output from ${withOut}`
  return `${head}, ${tail}. A half no attempt reported is left out of this total rather than counted as zero.`
}

/**
 * What the elapsed figure is a measure of, which is not the same thing in
 * every state.
 *
 * "Still running" on a PARKED task was the first version of this and it is a
 * flat lie: a parked attempt released its capacity and nothing is executing.
 * `elapsed()` keeps counting wall time from `started_at` regardless -- that is
 * the Agents table's behaviour and is not changed here -- so the sentence
 * under the figure is what has to say which clock it is.
 */
function elapsedNote(task: Task): string {
  if (TERMINAL_STATES.has(task.state)) {
    if (task.completed_at !== null) return `Finished ${timeAgo(task.completed_at)}.`
    // NO completion time, and `elapsed()` does not stop for that: it falls
    // through to `now - started_at` and keeps counting to the current clock.
    // So the figure beside this sentence is not the length of the run, and it
    // grows on every render -- which the sentence has to say, because nothing
    // else on the tile can.
    return task.started_at === null
      ? 'Finished, with neither a start nor a completion time recorded — the figure counts from submission to now.'
      : 'Finished, with no completion time recorded — the figure counts to now, not to the end of the run, and keeps growing.'
  }
  if (task.state === 'PARKED') {
    return 'Parked — wall time, not work. Nothing is executing and no capacity is held.'
  }
  if (task.started_at === null) return 'Time spent waiting. Nothing has started.'
  return 'Still running.'
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
          <span className="blocker-copy">{reasonText(task.park_reason)}</span>
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
              {/* ONE table of copy, in types.ts, shared with the agents list
                  and the trouble board. The three cases hand-rolled here
                  covered three of the twelve BlockedReasons; the other nine --
                  PROVIDER_CONCURRENCY_LIMIT, RESOURCE_CLASS_LIMIT,
                  RUNNER_LIMIT and BACKEND_LIMIT among them -- rendered an
                  empty span in the banner whose entire job is "why is nothing
                  happening". */}
              <span className="blocker-copy">{reasonText(b.reason)}</span>
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

/**
 * The sentence for a park reason or a blocker reason.
 *
 * `REASON_COPY` is the single table, shared with the agents list and the
 * trouble board. `reasonCopy` falls back to the raw string, which is right in
 * a table cell and wrong here: the enum token is already printed in bold
 * beside this, so the fallback would print it twice. An unrecognised reason
 * therefore gets a sentence about THIS SCREEN not knowing it -- a fact about
 * the UI rather than an invented explanation of the platform's state.
 */
function reasonText(reason: string): string {
  return REASON_COPY[reason] !== undefined
    ? reasonCopy(reason)
    : 'This screen has no copy for that reason — it is printed above exactly as the platform recorded it.'
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
    // A REAL ZERO, and which real zero depends on the COUNTER, not on the
    // state. `create_attempt` runs at dispatch and `attempt_count` is
    // incremented inside the admission transaction, so ANY task whose counter
    // is above zero and whose query returned nothing has a hole in the record
    // -- a RUNNING one as much as a finished one. Gating this on `finished &&`
    // sent every live task with that hole into the branch below, which
    // interpolates its state into a sentence asserting the opposite.
    const finished = TERMINAL_STATES.has(task.state)
    return (
      <section className="section">
        <h2>Attempts</h2>
        {task.attempt_count > 0 ? (
          <Absent
            kind="partial"
            heading={
              finished
                ? 'This task finished, and no attempt document came back'
                : 'The task counts attempts, and no document came back'
            }
          >
            The task counts {plural(task.attempt_count, 'attempt')} and the query
            returned none. An attempt document is written when capacity is
            reserved, so these documents are missing, which is not the same as
            never having run.
            {!finished && (
              <>
                {' '}
                This task is <code>{task.state}</code>, so the attempt it counts
                should be readable right now.
              </>
            )}
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
  const end = attemptEnd(a, run.task, isLatest)

  // `attemptOutcome` reads the attempt document ALONE, and on the document
  // alone a reclaimed attempt is indistinguishable from a running one: both
  // have a start time and no finish time. This card can see two things the
  // document cannot -- whether a later attempt exists and what state the task
  // is in -- so the chip is corrected here rather than left saying "running"
  // directly above a paragraph that says the attempt is over. The correction
  // belongs in `attemptOutcome`; that lives in types.ts, which another track
  // owns, so it is reported rather than edited.
  const chip: { label: string; tone: Tone | 'unknown' } =
    end.over && out.label === 'running'
      ? {
          label: end.by === 'superseded' ? 'superseded' : 'ended, no end recorded',
          tone: 'wait',
        }
      : out

  return (
    <section className="section panel" style={CARD}>
      <h2>
        Attempt {ordinal}
        <span className="count-chip">gen {a.generation}</span>
        <Chip tone={chip.tone}>{chip.label}</Chip>
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
        {/* `attemptRan` measures from the start to NOW when there is no finish
            time, which is right for a running attempt and false for this one:
            an attempt that ended without its end being written stopped at some
            unknown moment, and "1h 40m so far" is a clock still running on a
            process that is gone -- printed, until this branch existed, next to
            the chip and the paragraph below that both say it is over. */}
        <dd>
          {end.over && a.completed_at === null && a.started_at !== null ? (
            <span className="muted">
              not recorded — it started {timeAgo(a.started_at)} and no finish
              time was ever written, so how long it ran is unknown
            </span>
          ) : (
            attemptRan(a, now)
          )}
        </dd>
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

      <AttemptResources a={a} run={run} isLatest={isLatest} />
      <AttemptSpend a={a} profile={run.task.runner_profile} />
      <AttemptCheckpoints a={a} run={run} />
    </section>
  )
}

/**
 * HAS THIS ATTEMPT ENDED, and on what evidence.
 *
 * `completed_at` is the obvious test and it is not sufficient. It is written
 * in exactly one place -- `control.record_attempt_end`, reachable only from
 * `finish()` -- so the two interruptions this screen most needs to describe
 * never set it. A SIGKILL kills the worker before `finish()` runs, and a
 * reconciler reclaim of a stale generation repairs the TASK document without
 * touching the attempt's at all (`repair_task_state` sets `completed_at` on
 * the task, not on the attempt). An attempt that ended either of those ways
 * keeps `completed_at: null` for ever -- the same shape a RUNNING attempt has
 * -- so a panel keyed on that field alone tells the reader of a reclaimed
 * attempt to wait for a final figure that nothing will ever write.
 *
 * Two further facts on this page settle it, and both are READ rather than
 * inferred:
 *
 *   - A LATER ATTEMPT DOCUMENT EXISTS. Attempts are fenced by generation and
 *     run one at a time, so an attempt that is not the newest is over.
 *   - THE TASK IS TERMINAL. Nothing runs for it again, so nothing writes to
 *     any of its attempts again.
 *
 * `by` is carried because the three read differently to a person and the
 * sentence under the bars names the one that applies. What is NOT claimed
 * anywhere is WHY the attempt stopped: this page cannot see a kill, a reclaim
 * or a crash -- only that it stopped, and that no final figure came with it.
 */
type AttemptEnd = { over: false } | { over: true; by: 'recorded' | 'superseded' | 'task-ended' }

function attemptEnd(a: AttemptRow, task: Task, isLatest: boolean): AttemptEnd {
  if (a.completed_at !== null) return { over: true, by: 'recorded' }
  if (!isLatest) return { over: true, by: 'superseded' }
  if (TERMINAL_STATES.has(task.state)) return { over: true, by: 'task-ended' }
  return { over: false }
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
function AttemptResources({
  a,
  run,
  isLatest,
}: {
  a: AttemptRow
  run: AgentRun
  /** From the attempt list: whether any later attempt document exists. */
  isLatest: boolean
}) {
  const { task, events, classes, classesDetail, classesRouteMissing } = run
  const cls: ResourceClassSpec | null = classes?.[task.resource_class] ?? null
  const hb = newestHeartbeat(a, events)

  // THE ATTEMPT HAS ENDED OR IT HAS NOT, and the same fallback figure means
  // two different things either way. A running attempt has no final peak YET.
  // An attempt that ended without `record_resource_usage` has none at all and
  // none is coming, and calling that figure "live" tells its reader to wait
  // for a write that has already not happened.
  //
  // `completed_at` alone does not separate the two -- it is not written on the
  // paths this distinction exists for. `attemptEnd` above is where the three
  // pieces of evidence this page actually holds are read.
  const end = attemptEnd(a, task, isLatest)
  const ended = end.over

  // WHAT IS KNOWN ABOUT THE END, in the words of what was read. This page
  // cannot see a kill, a reclaim or a crash; it can see a finish time, a later
  // attempt document and the task's state, so it says those and stops.
  const endedBecause: ReactNode =
    end.over === false ? null : end.by === 'recorded' ? (
      <>Its finish time is recorded, and no peak memory was written with it.</>
    ) : end.by === 'superseded' ? (
      <>A later attempt has replaced it, so nothing writes to this document again.</>
    ) : (
      <>
        The task is <code>{task.state}</code> and this attempt was never marked
        finished — the shape a kill, or a reconciler reclaim of a stale
        generation, leaves behind.
      </>
    )
  const liveRss = a.peak_rss_bytes === null ? (hb?.peakRssBytes ?? null) : null
  const rss = a.peak_rss_bytes ?? liveRss
  const rssBy =
    a.peak_rss_bytes !== null
      ? 'at exit'
      : liveRss !== null && hb !== null
        ? `${ended ? 'last' : 'latest'} heartbeat ${timeAgo(hb.at)}`
        : a.started_at === null
          ? 'never ran'
          : ended
            ? 'never written'
            : 'not yet written'

  const diskBy =
    a.peak_disk_bytes !== null
      ? 'at exit'
      : a.started_at === null
        ? 'never ran'
        : ended
          ? 'never written'
          : 'not yet written'

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
      {/* THE AGE GOES IN THE SENTENCE, not only in the `by` column: that
          column is display:none below 560px, so on a phone the sentence is the
          only thing left carrying it. */}
      {a.peak_rss_bytes === null && liveRss !== null && hb !== null && (
        <p className="muted small">
          {ended ? (
            <>
              This attempt is over, so the memory figure is the last heartbeat
              reading before it stopped ({timeAgo(hb.at)}) — not a live one.{' '}
              {endedBecause} The final high-water mark is written at the end of
              an attempt, and this one has already ended without it, so there
              is none and none is coming.
            </>
          ) : (
            <>
              The memory figure is a live reading from the newest heartbeat
              event on this page ({timeAgo(hb.at)}), not the final high-water
              mark — that is written when the attempt ends. The event page is
              oldest-first and capped, so on a long attempt the newest reading
              available here can be far older than the agent.
            </>
          )}
        </p>
      )}
      {a.peak_rss_bytes === null && liveRss === null && ended && a.started_at !== null && (
        <p className="muted small">
          This attempt ended with no peak memory recorded, and no heartbeat
          carrying one is on this page. {endedBecause} What it used is unknown —
          which is why the figure is an em dash rather than a zero, and no later
          write will fill it in.
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
 * size. So a row with an id and no location is not a broken checkpoint -- and
 * the table says per row WHICH of the two reasons it is: the event is off this
 * page, or the event read FAILED and nothing whatever is known about it. Those
 * two were one sentence, which reported a failed read as benign paging.
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
                      <span className="muted">
                        {eventsRead
                          ? '— its event is not on this page'
                          : '— unknown: the event read failed'}
                      </span>
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
              {!eventsRead
                ? ` The event read failed, so no size and no location could be attached to any of these ${rows.length} ids. They are unknown rather than missing, and nothing here says whether the checkpoints themselves are fine.`
                : missingLocation > 0
                  ? ` ${missingLocation} of ${rows.length} have no event on this page — the events route is oldest-first, capped, and returns no page token, so a long attempt's later checkpoints fall off the end.`
                  : ''}
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
  // WITH NO EVENTS THERE IS NOTHING TO MATCH AGAINST. A checkpoint's uri is
  // recorded only on its event, so a failed event read leaves every row with
  // `uri: null`, nothing matches, and this note fired for every task --
  // explaining a comparison that never happened with two causes that were not
  // what happened.
  if (run.events === null) {
    return (
      <p className="muted small">
        The task&apos;s restore pointer is{' '}
        <span className="mono uri">{latest}</span>. The event read failed and a
        checkpoint&apos;s uri is only ever recorded on its event, so nothing
        listed above can be compared with it. Whether it matches a checkpoint
        of these attempts is unknown.
      </p>
    )
  }
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
  // THE SAME CAUTION AS THE ARTIFACT LIST, for the same reason: an older
  // worker or a fixture can put a list -- or a string -- in `logs`. Silently
  // substituting `{}` made `Logs` print "no log stream was uploaded for this
  // attempt", a claim about the RUN, for a record that is merely malformed,
  // and dropped the stdout/stderr URIs the reader came for without a word.
  const logsRaw: unknown = summary?.logs
  const logsOk = typeof logsRaw === 'object' && logsRaw !== null && !Array.isArray(logsRaw)
  const logs: Record<string, unknown> = logsOk ? (logsRaw as Record<string, unknown>) : {}
  const logsMalformed = logsRaw !== undefined && !logsOk
  const terminal = TERMINAL_STATES.has(task.state)

  return (
    <section className="section panel">
      <h2>Output</h2>

      {/* THE SCOPE LINE IS NOT OPTIONAL. Everything in this panel comes from
          result_summary, which finish() writes ONCE at terminal state. On a
          retried task it is the last attempt's output and nothing else, and a
          panel that does not say so gets read as the run's. */}
      {/* THE FALLBACK MATTERS MORE THAN THE PRIMARY. With the attempt read
          failed this panel cannot count attempts -- which is precisely when
          the caveat is most needed -- and it used to vanish, leaving
          artifacts, git outcome and logs rendered unqualified as the run's
          output. `task.attempt_count` is the task's own counter, already shown
          above, and answers the question when the query does not. */}
      {(attempts !== null ? attempts.length > 1 : task.attempt_count > 1) && (
        <p className="muted small" style={{ marginTop: 0, marginBottom: 10 }}>
          Everything here describes the LAST attempt only. The result summary is
          written once, at terminal state; the earlier attempts&apos; output was
          never summarised anywhere.
          {attempts === null &&
            ` The attempt read failed, so the ${task.attempt_count} attempts are the task's own count rather than a document each.`}
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
          <GitOutcome git={summary.git} artifacts={artifacts} task={task} />
          <Artifacts
            artifacts={artifacts}
            summary={summary}
            malformed={artifactsMalformed}
            taskId={task.id}
          />
          <Logs logs={logs} malformed={logsMalformed} raw={logsRaw} />
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
  taskId,
}: {
  artifacts: ArtifactRef[]
  summary: ResultSummary
  /** `artifacts` was present but not an array. Not the same as none. */
  malformed: boolean
  /** B29: the viewer names an ARTIFACT on a TASK; it never sends a location. */
  taskId: string
}) {
  const skippedRaw = summary.artifacts_skipped
  const skipped = Array.isArray(skippedRaw) ? skippedRaw : []
  const [open, setOpen] = useState<string | null>(null)
  const showing = open === null ? null : artifacts.find((a) => a.name === open) ?? null

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
                <tr key={a.uri} className={a.name === open ? 'is-open' : undefined}>
                  <th scope="row">
                    {/* THE WAY IN. `GET /v1/tasks/{id}/artifacts/content` sends
                        the NAME as this row spells it and nothing else: the
                        server resolves the object from the task's own manifest,
                        which is why no path, key or uri from this table is ever
                        put in a request. */}
                    <button
                      type="button"
                      className="art-open"
                      aria-expanded={a.name === open}
                      onClick={() => setOpen((cur) => (cur === a.name ? null : a.name))}
                    >
                      {a.name}
                    </button>
                  </th>
                  <td className="is-num">{bytesLabel(a.bytes)}</td>
                  {/* Still passed by reference as well. The viewer serves a
                      bounded, redacted window; the uri is how someone reads the
                      whole object with their own credentials, which keeps that
                      half of the tenant boundary where IAM already enforces it. */}
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
      {showing !== null && (
        <ArtifactViewer taskId={taskId} artifact={showing} onClose={() => setOpen(null)} />
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

function Logs({
  logs,
  malformed,
  raw,
}: {
  logs: Record<string, unknown>
  /** `logs` was present and was not a map. Not the same as no stream. */
  malformed: boolean
  /** The value as recorded, shown when it is malformed so a uri inside it is
   *  not thrown away silently. */
  raw: unknown
}) {
  const entries = Object.entries(logs)

  if (malformed) {
    return (
      <div className="section" style={SUB}>
        <h2>Logs</h2>
        <Absent kind="partial" heading="The log list is not a map of streams">
          This result summary records a <code>logs</code> field that is not an
          object of stream name to uri, so no stream can be listed from it. The
          attempt may well have uploaded stdout and stderr — this is a
          malformed record, not a run without logs. The value is reproduced
          below so that a uri inside it is not lost.
        </Absent>
        <pre className="json" style={{ maxHeight: 200, overflowY: 'auto' }}>
          {JSON.stringify(raw, null, 2)}
        </pre>
        <LogsFoot />
      </div>
    )
  }

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
          {entries.map(([label, value]) => (
            <div key={label} style={{ display: 'contents' }}>
              <dt>{label}</dt>
              <dd className="mono uri">
                {typeof value === 'string' ? (
                  <>
                    {value}
                    <button
                      className="copy"
                      onClick={() => navigator.clipboard?.writeText(`gsutil cat ${value}`)}
                    >
                      copy gsutil
                    </button>
                  </>
                ) : (
                  /* An entry whose value is not a string is not a uri. React
                     would throw on an object child, and printing it as a
                     location would send someone to gsutil with a number. */
                  <span className="muted">{JSON.stringify(value)} — not a uri, so there is nothing to fetch</span>
                )}
              </dd>
            </div>
          ))}
        </dl>
      )}
      <LogsFoot />
    </div>
  )
}

/** The standing fact about logs on this platform, in both branches above. */
function LogsFoot() {
  return (
    <p className="muted small">
      These are the files uploaded when the attempt ended. Nothing streams while
      an agent is running: there is no log-tail read path on the API, so a
      running agent&apos;s output is not readable here at all.
    </p>
  )
}

/**
 * The untyped `result_summary.runner.usage`, shown when the typed per-attempt
 * fields have nothing -- or when nobody could read them.
 *
 * It is the OLD home for these numbers -- an untyped dict no index can reach.
 * The attempt fields replaced it, so preferring them and falling back here
 * means an attempt that predates the typed capture still shows its spend
 * instead of five em dashes.
 */
function SummaryUsage({ task, attempts }: { task: Task; attempts: AttemptRow[] | null }) {
  const usage = usageOf(task)
  if (usage === null) return null
  // A FAILED ATTEMPT READ IS NOT A NEGATIVE ANSWER. `attempts.some(...)` on a
  // null read is `false`, which had this panel leading with "No attempt
  // document carries a typed spend figure" -- a positive claim about documents
  // nobody read, when those documents may well carry `cost_usd`. The untyped
  // numbers below are real either way, so they stay; only the sentence that
  // frames them changes.
  const unread = attempts === null
  const typed = attempts !== null && attempts.some((a) => a.cost_usd !== null || a.input_tokens !== null)
  if (typed) return null

  const n = (k: string) => (typeof usage[k] === 'number' ? (usage[k] as number) : null)
  const models = usage['models']

  return (
    <div className="section">
      <h2>Spend, from the result summary</h2>
      <p className="muted">
        {unread
          ? 'The attempt read failed, so whether any attempt document carries a typed spend figure is unknown. What follows is the last attempt’s own result summary — an untyped dict no query can reach — and it describes that attempt only.'
          : 'No attempt document carries a typed spend figure, but the last attempt’s result summary does. These come from an untyped dict that no query can reach, and they describe the last attempt only.'}
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
 * WHAT THIS DISPATCH CHOSE -- above the Code panel, because it is the question
 * that panel keeps being asked.
 *
 * "Why did this run open a pull request and that one not?" has one answer and
 * it is not in the git summary: it is the `strategy` the caller picked at
 * submission. Without it an operator reads the publish outcome and reaches for
 * a cause -- a token scope, a forge outage, a push rejection -- when the cause
 * was a choice somebody made and the platform kept.
 *
 * It renders for EVERY task, not only finished ones. `Output` is empty until an
 * attempt finishes, and "what will this do when it gets there" is exactly what
 * is worth knowing while it is still running.
 */
function DispatchPanel({ task }: { task: Task }) {
  return (
    <section className="section panel">
      <h2>Dispatch</h2>
      <p className="muted" style={{ marginBottom: 10 }}>
        Chosen at submission and stored on the task. It decides what happens to
        the agent&apos;s work once the run finishes — nothing about how it runs.
      </p>
      <DispatchFacts task={task} />
    </section>
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

function GitOutcome({ git, artifacts, task }: { git: GitSummary | undefined; artifacts: ArtifactRef[]; task: Task }) {
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

      <PublishOutcome git={git} task={task} />

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

/**
 * The causes, each with the response it actually needs.
 *
 * TWO OF THESE USED TO BE DIAGNOSED WRONG, and both wrong answers sent someone
 * to do work that would have made things worse. Neither was a rendering bug:
 * the screen had no access to the dispatch and was matching free text, so the
 * two outcomes that are CORRECT BY REQUEST were indistinguishable from failures.
 *
 *  * `collect` -- the DEFAULT, so this was the common case -- produces
 *    `published: false` with a reason that matched none of the patterns below,
 *    and fell through to "the reason is git's own / a rejected push usually
 *    means something else moved the branch". Nothing was pushed and nothing was
 *    rejected: the caller asked for the patch to be harvested and it was.
 *  * an `integrate` CONTRIBUTOR produces `published: true` with no pull request,
 *    and read as "only the pull-request call did not complete, so opening one
 *    by hand from that branch is all that is left". Opening one by hand is the
 *    one thing that must not happen -- it is a second pull request against a
 *    strategy whose whole promise is exactly one, and the integrator is already
 *    going to merge that branch.
 *
 * So the two are keyed on the STRUCTURED dispatch now, not on prose. The
 * worker's own `strategy`/`role` fields in the git summary come first because
 * they record what the run did; the task's stored dispatch answers for a
 * summary written before those fields existed. The prose patterns stay as a
 * third fallback for a task whose dispatch cannot be read at all.
 */
function PublishOutcome({ git, task }: { git: GitSummary; task: Task }) {
  const reason = git.publish_reason ?? null
  const lower = (reason ?? '').toLowerCase()
  const stored = dispatchOf(task)
  // The run's own record wins over the task's, and neither is invented: an
  // unrecognised value falls through to null rather than being coerced.
  const strategy: DispatchStrategy | null =
    git.strategy === 'collect' || git.strategy === 'direct-pr' || git.strategy === 'integrate'
      ? git.strategy
      : (stored?.strategy ?? null)
  const role: DispatchRole | null =
    git.role === 'contributor' || git.role === 'integrator' ? git.role : (stored?.role ?? null)

  // THE PROSE IS THE THIRD FALLBACK, not the first, and it exists for one case:
  // a task whose dispatch this API did not report AND whose summary predates the
  // worker's structured fields. Both substrings are the worker's own, quoted
  // from `_publish` in agent_worker/lifecycle.py -- see the seam test in
  // tests/unit/control_plane/test_dispatch_ui_surface.py, which asserts these
  // still appear there verbatim.
  const isCollect = strategy === 'collect' || lower.includes("strategy is 'collect'")
  const isContributor = role === 'contributor' || lower.includes('this step is a contributor')

  // 1. There is a pull request. Nothing to explain.
  if (git.pull_request) return null

  // 2. A CONTRIBUTOR IN AN `integrate` WORKFLOW. Pushed, deliberately opened
  //    nothing, and must not be "finished off" by hand. Ahead of the general
  //    pushed-but-no-PR case below, which would tell you to do exactly that.
  if (isContributor && git.published === true) {
    return (
      <div className="ctl-empty" style={{ marginBottom: 10 }}>
        <h3>Pushed, and no pull request — by request</h3>
        <p>
          This step is a <code>contributor</code> in an <code>integrate</code>{' '}
          workflow. Its branch{' '}
          <span className="mono">{git.branch ?? 'below'}</span> is on the forge and
          the integrating step merges it into the single pull request the whole
          workflow opens. <strong>Do not open one from this branch</strong> — that
          is the second pull request this strategy exists to prevent.
        </p>
        {reason !== null && <span className="ctl-empty-foot">{reason}</span>}
      </div>
    )
  }

  // 3. PUSHED, NO PULL REQUEST. The branch is on the forge; only the final API
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

  // 4. `collect`: the caller asked for nothing to be pushed. Before the
  //    strategy check below the forge was never even contacted, so there is no
  //    can_push, no branch and no git error to report -- and this fell through
  //    to the "rejected push" ending, which described a push that never
  //    happened. It is checked ahead of can_push for that reason: on this path
  //    the worker returns before `probe_repository`, so every field those
  //    branches read is absent.
  if (isCollect) {
    return (
      <div className="ctl-empty" style={{ marginBottom: 10 }}>
        <h3>Nothing was pushed — this dispatch asked for <code>collect</code></h3>
        <p>
          <code>collect</code> is the default strategy: the agent&apos;s work is
          harvested into this task&apos;s patch and artifacts, and the repository
          is never written to. Nothing failed, and no token or forge setting
          changes this. Submit with <code>direct-pr</code> to have the agent open
          a pull request of its own, or run it as a step of an{' '}
          <code>integrate</code> workflow for one pull request across the steps.
        </p>
        {reason !== null && <span className="ctl-empty-foot">{reason}</span>}
      </div>
    )
  }

  // 5. The tenant's token cannot push. EXPECTED TODAY, and a fact rather than a
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

  // 6. No reason at all.
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

  // 7+. No structured signal beyond `published: false`, so the free-text
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
function Input({ run }: { run: AgentRun }) {
  const { task } = run
  // The sizing behind the class NAME. A caller picks `resource_class` and
  // `runner_profile` by name and nothing else -- which is the rule that keeps
  // arbitrary compute out of the API -- and then has no way to find out what
  // the name they picked is worth. The catalogue this screen already loaded
  // for the utilisation bars answers that for the class, so it is said here
  // rather than left implicit in a bar three panels up.
  const cls = run.classes?.[task.resource_class] ?? null
  const noCeiling = ceilingNote(run)
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
        <dd>
          {task.resource_class}
          <span className="muted small">
            {' '}
            ·{' '}
            {cls !== null
              ? `${cls.cpu} vCPU · ${cls.memory_gib} GiB memory, of which the workspace may take ${cls.disk_gib} GiB · ${plural(cls.units, 'capacity unit')}`
              : (noCeiling ?? 'the sizing behind this name is unknown')}
          </span>
        </dd>
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

      {/* WHAT THE PROFILE NAME MEANS IS NOT ON THIS PAGE, and saying so is the
          honest alternative to describing it from a client-side copy that
          would drift the first time the catalogue changed. */}
      <p className="muted small">
        What <code>{task.runner_profile}</code> runs — its image, command,
        timeout and checkpoint interval — is in the frozen runner-profile
        catalogue, and no route serves it, so this page can show the name only.{' '}
        <code>GET /v1/capacity</code> does publish each profile&apos;s resource
        class, backend and provider; this screen does not read it.
      </p>

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
  label: string
  events: TaskEvent[]
}

/**
 * The event each terminal transition writes -- `control.py`'s state-to-event
 * map, plus the scheduler's cascade cancel and the API's own. A terminal task
 * has one by construction, so a terminal task whose page carries none is proof
 * the page ends before the run did.
 */
const TERMINAL_EVENTS: ReadonlySet<string> = new Set([
  'succeeded',
  'failed',
  'cancelled',
  'dead_lettered',
])

/**
 * The event stream, grouped under the attempt that wrote it.
 *
 * A flat list interleaves three attempts into one column where only the
 * timestamps separate them, so what was different about the two that failed
 * cannot be read off it. Every event carries `attempt_id` (`_event_to_api`),
 * so the grouping is the API's own rather than a guess.
 */
function Timeline({
  task,
  events,
  detail,
  attempts,
}: {
  task: Task
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

  // THE CAP IS THE SERVER'S AND THIS PAGE DOES NOT KNOW IT. The endpoint
  // orders `at` ASCENDING, applies `paged_limit` and returns NO page token, so
  // the newest events of a long task are unreachable here. `events.length >=
  // 200` was the old test and it could never fire: this client sends no
  // `limit`, so the server falls back to `default_page_size` -- 50 unless a
  // deployment overrides it -- and nothing in the response says which. A
  // worker heartbeats every ~150s, so an attempt of more than about two hours
  // runs past that and the timeline simply stopped mid-run, with the banner
  // written to prevent exactly that silently disabled.
  //
  // Truncation is therefore DETECTED, not counted: a terminal task always
  // writes a terminal event, so a terminal task whose page carries none proves
  // the page ends before the run did, whatever the cap happens to be. Where
  // there is no proof, the caveat is stated as a limit of the route rather
  // than as a claim about this task -- a count of 50 is not evidence either
  // way, and pretending otherwise is how the 200 got here.
  const lastEvent = events[events.length - 1]
  const endMissing =
    TERMINAL_STATES.has(task.state) && !events.some((e) => TERMINAL_EVENTS.has(e.type))
  const groups = grouped(events, attempts)

  return (
    <section className="section panel">
      <h2>
        Timeline
        <span className="count-chip">{events.length} on this page</span>
      </h2>

      {endMissing ? (
        <div className="ctl-empty is-partial" role="status" style={{ marginBottom: 10 }}>
          <h3>This page stops before the end of the run</h3>
          <p>
            The task is <code>{task.state}</code> and a terminal task writes a
            terminal event — none is on this page. The route orders events
            oldest-first, caps the page server-side and returns no page token,
            so the newest events are not reachable from this screen at all. The
            end of this task&apos;s history is missing, not absent.
          </p>
          <span className="ctl-empty-foot">
            {lastEvent === undefined
              ? 'No event on this page.'
              : `The newest event here is ${lastEvent.type}, ${timeAgo(lastEvent.at)}.`}
          </span>
        </div>
      ) : (
        <p className="muted small" style={{ marginTop: 0, marginBottom: 10 }}>
          One page, oldest first. This screen asks for no page size, so the cap
          is whatever the deployment&apos;s default is, and the response says
          neither what it was nor how many events were left out — a page that
          looks complete is not evidence that it is.
        </p>
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
    groups.push({ key: '@preface', label: 'Before any attempt', events: preface })
  }
  const ordered = [...attempts].sort((x, y) => x.created_at.localeCompare(y.created_at))
  ordered.forEach((a, i) => {
    groups.push({
      key: a.attempt_id,
      label: `Attempt ${i + 1} · generation ${a.generation}`,
      events: byAttempt.get(a.attempt_id) ?? [],
    })
    byAttempt.delete(a.attempt_id)
  })
  // What is left names an attempt no document on this page describes. Folding
  // these into the preface would file real attempt events under "before any
  // attempt", which is a lie about when they happened.
  for (const [id, list] of byAttempt) {
    groups.push({ key: id, label: `Attempt ${id} · no attempt document`, events: list })
  }
  return groups
}
