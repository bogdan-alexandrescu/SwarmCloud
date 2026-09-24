import { useCallback, useState, type CSSProperties, type ReactNode } from 'react'
import { loadAgentRun, type AgentRun } from './api'
import { ArtifactViewer } from './ArtifactViewer'
import { AttemptDurations } from './charts/AttemptPhases'
import { CheckpointStrip } from './charts/CheckpointStrip'
import { DiffstatChart } from './charts/Diffstat'
import { PeakMemoryChart } from './charts/PeakMemory'
import { TokenSpendChart } from './charts/TokenSpend'
import { DispatchFacts } from './Dispatch'
import { attemptEnd } from './duration'
import { num } from './fetch'
import { HELP, type TopicId } from './help'
import { HelpCard } from './HelpCard'
import { LivenessBadge } from './Liveness'
import { Absent, Mark, Metric, UtilRow, type MarkKind } from './primitives'
import { RunFiles } from './RunFiles'
import { Screen, timeAgo } from './Shell'
import { StagedInputs } from './StagedInputs'
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

/**
 * The screen's body, once the load has resolved.
 *
 * EXPORTED FOR ONE REASON. `tests/agentdetail.test.tsx` is the acceptance test
 * for B7.1 -- it renders this screen with a cost that was never reported and
 * asserts that the surface, with every help card CLOSED, still tells absent
 * from zero. `AgentDetailScreen` above wraps this in `Screen`, whose load runs
 * in an effect, so rendering that statically yields a skeleton and would make
 * the acceptance test assert nothing at all. The test drives the real
 * component with a real `AgentRun` instead.
 *
 * `reload` is OPTIONAL for the same reason. The stop control needs it to
 * refresh after a cancel, and the screen always passes it -- but requiring it
 * would force the acceptance test to invent a stub, and a test that has to
 * fabricate a callback in order to assert on STATIC markup is asserting on its
 * own scaffolding. Absent, the stop control simply has nothing to call.
 */
export function Run({ run, reload }: { run: AgentRun; reload?: () => void }) {
  const { task, events } = run
  const now = Date.now()

  return (
    /* ONE SUBJECT, SO ONE STACK — AND THE ORDER IS THE OPERATOR'S, NOT THE
       DATA MODEL'S.
       -----------------------------------------------------------------------
       TWO THINGS CHANGED HERE AND BOTH ARE STRUCTURAL.

       1. WHY AND THE ERROR MOVED ABOVE THE FIGURES. An agent's run panel is
          opened for one of two reasons: to watch it, or to find out why it
          stopped. The reason it stopped used to sit BELOW six boxed figures --
          elapsed, attempts, peak memory, tokens, cost, checkpoints -- none of
          which answers that question, so the one fact the reader came for was
          under the six that were merely available. `Why` renders nothing when
          there is nothing to say, so this costs a healthy run no space at all.

       2. `.run-stack` IS WHAT STOPS FIFTEEN HAIRLINES. Measured on this
          drawer before the change: 15 elements drawing `.section + .section`'s
          top rule down a 5,155px scroller. See the rule in styles.css for the
          argument; the wrapper is here because the rule needs something to
          scope to, and it is one element rather than a class change on the
          twenty-eight `.section`s this file renders. */
    <div className="run-stack">
      <Headline run={run} now={now} reload={reload} />
      <Alerts task={task} />
      <Why task={task} />
      {task.last_error && <ErrorBanner text={task.last_error} />}
      <RunMetrics run={run} now={now} />
      <Attempts run={run} now={now} />
      <DispatchPanel task={task} />
      <Output run={run} />
      {/* The checkpoint and log READ ROUTES, which no screen had ever called.
          They are a separate component because they are separate reads with
          their own failure states: a failed checkpoint listing must not blank
          this page, and it must not render as "this task has no checkpoints"
          either. See RunFiles.tsx. */}
      <RunFiles task={task} />
      <Input run={run} />
      <Timeline task={task} events={events} detail={run.eventsDetail} attempts={run.attempts} />
    </div>
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
export function chipTone(tone: ChipTone): string {
  return tone === 'wait' ? 'warn' : tone
}

/**
 * The tones a chip may take. `Tone` from types.ts is the derived one; the
 * other three are states a chip can be in that no task ever is -- an outcome
 * nobody recorded (`unknown`), a fact rather than a verdict (`info`), and work
 * an operator has held (`paused`).
 */
export type ChipTone = Tone | 'unknown' | 'info' | 'paused'

/**
 * A MARK AND A WORD, and the mark is the ONLY shape in it.
 *
 * design-system.md §6.6 rebuilt this primitive and §11.2 names the one thing
 * the screens still had to do: `{stateGlyph(state)} {state}` inside the chip
 * is a SECOND shape encoding of the fact the `<i>` already carries, and the
 * pill was the only reason it did not read as two dots. The pill is gone, so
 * the duplicate is gone with it -- here, in Agents.tsx and in AttemptTimeline.
 *
 * NOTHING THAT CARRIED INFORMATION LEFT. The shape channel is a TONE channel
 * (§6.6's table is ok/warn/bad/info/paused/unknown/live, not a state table)
 * and the `<i>` still carries all of it; the WORD, which §6.6 makes mandatory,
 * is what separates PARKED from READY from QUEUED and it is now at full ink
 * rather than in a mid-tone hue. The glyph was a third copy of a fact already
 * drawn twice.
 *
 * EXPORTED, because there were four state chips in this group and §9.3 asked
 * for one. Agents draws it forty times a screen; AttemptTimeline draws the
 * attempt outcome with it; this file draws the run's own state.
 */
export function Chip({ tone, children }: { tone: ChipTone; children: ReactNode }) {
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
 *  styleable as a class of thing and never mistaken for a digit.
 *
 *  EXPORTED because the other three screens of this group -- Agents,
 *  AttemptTimeline and ArtifactViewer -- draw the same absence and were each
 *  drawing it their own way. One definition, four screens. */
export function Em() {
  return <span className="ctl-em">—</span>
}

/**
 * THE SIX KINDS OF NOTHING, AS A SHAPE RATHER THAN A SENTENCE.
 *
 * `Mark` lives in ./primitives.tsx with the other primitives this file used to
 * define (`Metric`, the utilisation track, `Absent`). It is RE-EXPORTED here
 * because Agents, AttemptTimeline, ArtifactViewer and CheckpointBrowser import
 * it from this module, and those are other lanes' files: the re-export keeps
 * one definition without editing four call sites that did nothing wrong.
 *
 * The REASONING -- why a figure can be absent, what would have written it --
 * is neither the mark nor its sentence. It is `#help/<topic>`: published at
 * the label by `explain`, indexed in the card foot, and on this screen behind
 * exactly one `?` rather than the twenty-two it had before B7.4.
 */
export { Mark, type MarkKind } from './primitives'

/**
 * THE TILE, THE TRACK AND THE EMPTY STATE ARE THE SHARED PRIMITIVES.
 *
 * `Metric` and `Absent` are imported from ./primitives.tsx and called here
 * under the names this file always used. This file defined its own of each,
 * beside Overview's, and the two had already diverged (design-system.md
 * §9.4): Overview's track drew the measured-zero baseline tick and this one
 * did not, so a workspace that peaked at 0 B read here as a widget that
 * failed to paint. The tick is now drawn on both, by the one track.
 *
 * WHAT STAYS HERE IS THIS SCREEN'S POLICY, and it is unchanged:
 *
 *   - an absent tile's value is a PHRASE ("not recorded", "not reported") on
 *     the `is-absent` treatment, where the landing strip draws a mark. Both
 *     are the design system's; `tests/agentdetail.test.tsx` renders this
 *     screen with every help card CLOSED and asserts the phrase.
 *   - `explain` publishes a topic's short form at the label and draws no `?`
 *     (B7.4). The tile already tells an absence from a zero on the surface.
 */

/**
 * used / ceiling, as one `.ctl-util` row that stays honest when either side
 * is missing.
 *
 * The TRACK is the shared `UtilTrack`, so its four states are the product's:
 * unknown (no measurement, or no ceiling to measure it against) is hatched
 * with no fill; a measured zero draws the baseline tick; over the ceiling, the
 * track stands for what was used and the excess is hatched in the failure
 * colour rather than clipped; anything else is a fill.
 *
 * What this row decides is the VERDICT and the units. The warn/bad colouring
 * at 75% and 90% is PRESENTATION. The claim about whether an attempt came
 * dangerously close to its ceiling is `oom_near_miss`, which the worker sets
 * from its own threshold against the cgroup the kernel's OOM killer reads.
 * This bar never contradicts that flag; it just makes a tall bar visible
 * before you get to it.
 */
function CeilingRow({
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

  return (
    <UtilRow
      name={label}
      track={{
        pct: ratio === null ? null : ratio * 100,
        tone: ratio === null ? undefined : ratio >= 0.9 ? 'is-bad' : ratio >= 0.75 ? 'is-warn' : undefined,
      }}
      figure={
        <>
          {used === null ? <Em /> : fmt(used)}
          <span className="ctl-util-of"> / {ceiling === null ? '?' : fmt(ceiling)}</span>
        </>
      }
      byTitle={by}
      by={by}
    />
  )
}

/**
 * THE ATTEMPT CARD'S BOX IS NO LONGER INLINE.
 *
 * It used to be, and the comment that stood here said why: "`.panel` is a
 * heading modifier and draws no container at all, so three attempts ran
 * together into one column with nothing marking where the second began; the
 * `ctl-` block has no card primitive, so the box is inline." That was true and
 * it is the exact observation design-system.md §6.1 quotes as its reason for
 * existing. The primitive exists now, so the inline object is gone and the
 * card is `.ctl-card` / `.ctl-card-head` / `.ctl-card-body` / `.ctl-card-foot`
 * like every other card in the product.
 *
 * `SUB` stays: it is a sub-BLOCK inside a card, not a card, and `.section`'s
 * own 28px break is too much three deep.
 */
const SUB: CSSProperties = { marginBottom: 'var(--ctl-s3)' }

/**
 * `plural` IS GONE, and its absence is the prose rule in one function.
 *
 * It existed so this screen could write "3 attempts" and "1 attempt" rather
 * than "3 attempt(s)" -- correct English, and every call site of it was a
 * figure wearing a noun. A count strip reads `3` under the key `att`, a chip
 * reads `3`, and the noun is the column head or the key beside it, said once.
 * Fourteen calls to this helper were fourteen repetitions of a word the reader
 * had already read.
 */

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
  // Optional, threaded from Run -- see the note there. Absent only when the
  // acceptance test renders the body statically, where there is nothing to
  // reload and no stop control to press.
  reload?: () => void
}) {
  const { task, events } = run
  const el = elapsed(task, now)

  return (
    <section className="section panel">
      <h2>
        {/* The state as a `.ctl-chip`, not the old `.st` span: `.st` is only
            coloured inside `.row`, so in a heading it silently rendered in the
            heading's own faint grey -- the one element on the page whose colour
            is load-bearing was the one with none.

            THE GLYPH IS GONE (§11.2, §6.6). It sat between the chip's own `<i>`
            and the word, and once the pill stopped hiding it, it was plainly a
            second dot beside the first -- the owner's "decorative double dot",
            here and in Agents.tsx. Colour is still not the only signal: the
            `<i>` carries the silhouette and the WORD carries the state. */}
        <Chip tone={stateTone(task.state)}>{task.state}</Chip>
        {/* THIS SCREEN'S ONE `?` (B7.4), on the state chip it qualifies.
            The agent detail carried twenty-two help anchors, the most in the
            console -- one on almost every metric tile and empty state, each
            opening a general sentence beside a mark whose own accessible name
            said the same thing about THIS run with this run's figures in it.
            Those are `explain` now: published at the label for a screen reader,
            drawn nowhere, indexed in the card foot below.
            What stays is the one thing the chip cannot say. A state word tells
            a reader WHICH state this is; it cannot tell them that only four of
            the twelve hold a pool slot, so a task sitting at a state that looks
            idle may be the one costing capacity and a task that looks stuck may
            be costing nothing. That is invariant 1, it is the first question
            anyone opens this screen with, and no chip can carry it.
            `tests/agentdetail.test.tsx` asserts a `?` renders on this surface
            with every card closed. */}
        <HelpCard topic="capacity" />
        <LivenessBadge task={task} events={events} now={now} />
        {/* B28. The route has existed and worked since it was written and
            nothing in this app called it, so an operator watching an agent
            spend on the wrong thing had to leave the console to stop it. The
            control confirms first and every claim the confirmation makes is
            pinned by tests/unit/control_plane/test_cancel_semantics.py. */}
        <span className="run-stop">
          {reload && <StopRun task={task} what="this agent" reload={reload} />}
        </span>
      </h2>
      {/* A FACTS STRIP, NOT A DEFINITION LIST. Five `<dt>/<dd>` pairs down a
          96px column is five rows of chrome for five short values; the strip
          is one wrapping line, the key is two or three mono characters in the
          label treatment, and the value is the sans face at full strength. A
          fact whose value was not read KEEPS ITS SLOT AND ITS KEY -- a missing
          row is indistinguishable from a row that was never going to be
          there. */}
      <ul className="ctl-facts">
        <li className="ctl-fact">
          <b>run</b>
          {el.text}
        </li>
        <li className="ctl-fact">
          <b>age</b>
          {timeAgo(task.created_at)}
        </li>
        <li className={`ctl-fact${task.submitted_by === null ? ' is-absent' : ''}`}>
          <b>by</b>
          {task.submitted_by ?? <Em />}
        </li>
        <li className="ctl-fact">
          <b>tenant</b>
          <span className="mono">{task.tenant_id}</span>
        </li>
        {task.workflow_id !== null && (
          <li className="ctl-fact">
            <b>wf</b>
            <span className="mono">
              {task.workflow_id}
              {task.step_id !== null && ` · ${task.step_id}`}
            </span>
          </li>
        )}
      </ul>
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
        {/* THE SUBS ARE QUALIFIERS NOW, NOT SENTENCES. Each one names the
            SOURCE of the figure above it in two or three words -- which is the
            only thing the sentence was doing that a reader needed on the
            surface. Why the attempt read can fail, and what may not be
            concluded from one, is `#help/read-failed`. */}
        <Metric label="Elapsed" value={el.text} sub={elapsedNote(task)} foot="task document" />
        <Metric
          label="Attempts"
          value={`${task.attempt_count} / ${task.max_attempts}`}
          sub="task counter"
        />
        {/* No number may appear for anything that came from the attempt read.
            A reassuring zero on a failed read is the worst output available. */}
        <Metric label="Peak memory" value="read failed" tone="unread" sub="attempt query" explain="read-failed" />
        <Metric label="Spend" value="read failed" tone="unread" sub="attempt query" explain="read-failed" />
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
        unit={`/ ${task.max_attempts}`}
        // THE TWO NUMBERS ARE THE FACT. "Every attempt the task counts has a
        // document" was a sentence restating an equality the reader can see;
        // `task counts 3` beside `2` is the same fact and is the shape of the
        // discrepancy rather than a description of it.
        sub={
          attempts.length === task.attempt_count
            ? 'documents · counter agrees'
            : `task counts ${task.attempt_count}`
        }
        tone={attempts.length === task.attempt_count ? undefined : 'unread'}
      />
      {peak === null ? (
        // THE VALUE IS THE FACT AND IT STAYS. "not recorded" on a dashed
        // tile, in the faint colour, beside neighbours that are digits --
        // three signals, none of them hover, none of them colour alone.
        // The sentence that used to sit under it explained WHY it can be
        // absent, which `explain` publishes at the label and the card foot
        // links; B7.4 took the glyph, not the sentence.
        <Metric label="Peak memory" value="not recorded" tone="absent" explain="peak-memory" />
      ) : (
        <Metric
          label="Peak memory"
          value={bytesLabel(peak)}
          sub={
            noCeiling ??
            (cls === null
              ? 'ceiling unknown'
              : `of ${cls.memory_gib} GiB · worst of ${measured.length}`)
          }
          foot={nearMiss ? 'OOM near miss' : undefined}
          tone={nearMiss ? 'alert' : undefined}
          explain="oom-near-miss"
        />
      )}
      {tokIn === null && tokOut === null ? (
        <Metric label="Tokens" value="not reported" tone="absent" explain="tokens-reported" />
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
        <Metric label="Token cost" value="not reported" tone="absent" explain="token-cost" />
      ) : (
        <Metric
          label="Token cost"
          value={usd(cost)}
          // The COVERAGE stays, as the two figures rather than as a sentence
          // about them: "2 of 3 reported" is a fact about this run and the
          // reason the total may not be the whole of it. That there is no
          // infrastructure cost to add is a fact about the platform, and moved.
          sub={`${spent.length} of ${attempts.length} reported`}
          tone={spent.length === attempts.length ? undefined : 'unread'}
          explain="token-cost"
        />
      )}
      {/* A DIGIT, INCLUDING WHEN IT IS 0, AND THAT IS THE POINT. This zero was
          MEASURED -- the attempt documents were read and none lists a
          checkpoint -- so it renders as a figure on a solid tile while its
          unmeasured neighbours render as phrases on dashed ones. The contrast
          between the two shapes IS the honesty rule, and it is visible with
          every card shut. */}
      <Metric
        label="Checkpoints"
        value={`${ckpts}`}
        sub={ckpts === 0 ? undefined : `across ${attempts.length}`}
        explain="checkpoints"
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
  // THREE WORDS, NOT THREE SENTENCES. Each one still names which of the three
  // causes it is -- which is the whole reason this helper exists -- and the
  // consequence they all shared ("so the ceiling is unknown") is drawn by the
  // hatched track rather than restated in every tile that reads this.
  if (classes === null) return classesRouteMissing ? 'no catalogue route' : 'catalogue unread'
  if (classes[task.resource_class] === undefined) return `no class ${task.resource_class}`
  return null
}

/**
 * Which halves of the token total were reported, and by how many attempts.
 *
 * THE COVERAGE IS THE FACT AND IT IS TWO FRACTIONS. The sentence this replaces
 * ended "A half no attempt reported is left out of this total rather than
 * counted as zero" -- a rule of the platform, true of every run, which is
 * exactly the material `#help/tokens-reported` holds. What varies per run, and
 * therefore stays on the glass, is `in 2/3 · out 0/3`.
 */
function tokenRollupNote(total: number, withIn: number, withOut: number): string {
  return `in ${withIn}/${total} · out ${withOut}/${total}`
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
    if (task.completed_at !== null) return `finished ${timeAgo(task.completed_at)}`
    // NO completion time, and `elapsed()` does not stop for that: it falls
    // through to `now - started_at` and keeps counting to the current clock.
    // So the figure beside this qualifier is not the length of the run and it
    // grows on every render. `still counting` is what says that -- the tile
    // cannot say it any other way, and it is three words rather than a
    // paragraph because the WHY is `#help/read-failed`'s neighbour topic.
    return task.started_at === null
      ? 'no start, no finish · still counting'
      : 'no finish recorded · still counting'
  }
  // "Parked -- wall time, not work. Nothing is executing and no capacity is
  // held" was the first version, and the fact in it is the first three words:
  // the figure is a clock, not a measure of work.
  if (task.state === 'PARKED') return 'wall time, not work'
  if (task.started_at === null) return 'waiting · nothing started'
  return 'running'
}

function Alerts({ task }: { task: Task }) {
  const live = !TERMINAL_STATES.has(task.state)

  return (
    <>
      {/* THE REASON TOKEN IS THE FACT; THE SENTENCE UNDER IT WAS NOT.
          `REASON_COPY` is a table of platform invariants -- what
          QUOTA_EXHAUSTED means is true of every parked task there has ever
          been -- so it is `#help/park-on-missing-credential` and the rest of
          the help index now, and the banner carries the enum the platform
          recorded plus the one figure that varies: when it is eligible again.
          `reasonText` remains the fallback for a reason this screen does not
          recognise, because "we have no copy for that" is a fact about THIS
          SCREEN and cannot live in a help topic keyed on the reason. */}
      {task.park_reason && (
        <div className="bar amber">
          <strong>{task.park_reason}</strong>
          {task.next_eligible_at && (
            <> · eligible {new Date(task.next_eligible_at).toLocaleString()}</>
          )}
          {REASON_COPY[task.park_reason] === undefined && (
            <span className="blocker-copy">{reasonText(task.park_reason)}</span>
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
              {/* ONE table of copy, in types.ts, shared with the agents list
                  and the trouble board -- and now drawn only for a reason this
                  screen has no copy for. The twelve recognised reasons carry
                  their meaning in `#help/blockers-at-an-instant`; an
                  UNRECOGNISED one has nowhere else to say that it is
                  unrecognised, so it still says it here. */}
              {REASON_COPY[b.reason] === undefined && (
                <span className="blocker-copy">{reasonText(b.reason)}</span>
              )}
            </div>
          ))}
        </div>
      )}

      {/* WHY THE STATE DOES NOT CHANGE was two sentences about the lease and
          the pool; it is `#help/lease-and-pool-are-two-records`. What is on
          the glass is that a cancellation has been asked for. */}
      {task.cancel_requested && live && (
        <div className="bar red">
          Cancellation requested
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

/**
 * THE ONE SENTENCE THIS SCREEN KEEPS, and it keeps it deliberately.
 *
 * `whyAgent` is the answer to the question that brings people here: why has
 * this agent not moved. §8.5 of design-system.md allows a name, a unit, a
 * control's own label, a bare count and a qualifier -- and this is none of
 * them, it is running text. It stays because it is a fact about THIS RUN, it
 * is derived rather than looked up, and there is no encoding for it: a shape
 * cannot say "the pool it wants is at its ceiling and three agents are ahead
 * of it". The heading went, because a one-line panel headed "Why" is a word of
 * chrome per word of content.
 */
function Why({ task }: { task: Task }) {
  const why = whyAgent(task)
  if (!why) return null
  return (
    <section className="section">
      <p className="why-full">{why}</p>
    </section>
  )
}

/**
 * The error banner. Full text, monospace, NEVER one-line-truncated -- it is
 * the reason the page was opened.
 *
 * WHO WROTE IT IS THE FACT, AND IT IS ONE WORD. Three flavours, distinguished
 * by prefix because each means something different about who decided the task
 * had failed -- and each used to carry a sentence saying what that writer is.
 * The writer's NAME is the part that varies and the part a reader acts on; an
 * eyebrow carries it in the same treatment every other section label on this
 * screen uses. What a reconciler is, and that the dispatch field is capped at
 * 1000 characters, are platform invariants and live in `#help/read-failed`'s
 * neighbourhood rather than above every error.
 */
function ErrorBanner({ text }: { text: string }) {
  const reconciled = text.startsWith('reconciled:')
  // A dispatch failure is written as `<STABLE_CODE> (attempt att_...)`.
  const dispatch = /^[A-Z][A-Z0-9_]+ \(attempt /.test(text)
  const origin = reconciled ? 'reconciler' : dispatch ? 'scheduler · dispatch' : 'agent, at finish'

  return (
    <section className="section">
      <div className="ctl-empty is-failed" role="status">
        <span className="ctl-eyebrow">error · {origin}</span>
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
        <span className="ctl-eyebrow">attempts</span>
        <Absent
          kind="failed"
          heading="Attempt history"
          say="The attempt history could not be read. This is a failed read, not a task with no attempts."
          explain="read-failed"
        >
          {/* The DETAIL is the fact -- which read failed and how -- and §8.4
              keeps it as the empty state's one allowed sentence. What may not
              be concluded from a failed read is a standing rule, and moved. A
              null detail passes no child at all rather than padding the slot
              with a sentence about having nothing to say. */}
          {attemptsDetail ?? undefined}
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
        <span className="ctl-eyebrow">attempts</span>
        {task.attempt_count > 0 ? (
          // THE TWO NUMBERS ARE THE FACT, and they are now the heading rather
          // than a sentence under one: what the task counts against what came
          // back. A reader needs no help card to see the gap, and the `PARTIAL`
          // mark beside it says which kind of nothing this is.
          <Absent
            kind="partial"
            heading={`counts ${task.attempt_count} · returned 0`}
            say={`This task counts ${task.attempt_count} attempts and the query returned none, so there is a hole in the record.${finished ? '' : ` The task is ${task.state}.`}`}
            foot={finished ? undefined : task.state}
            explain="attempt-documents"
          />
        ) : (
          // "REAL ZERO" is the mark, always rendered, in words -- so the one
          // thing a reader must not have to hover for is the one thing they
          // cannot miss. The heading is the state the measurement was taken in.
          <Absent
            kind="zero"
            heading={`no attempt yet · ${task.state}`}
            say="The attempt query succeeded and returned nothing. Nothing has been admitted for this task yet, so this is a real zero rather than a failed read."
            explain="attempt-documents"
          />
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
        <span className="count-chip">{ordered.length}</span>
        {ordered.length < task.attempt_count && (
          // THE GAP, IN THE HEADING IT QUALIFIES. This was a full partial
          // panel with a two-sentence body; the figures are the fact and the
          // consequence ("every figure below describes only the attempts
          // shown") is what the mark means.
          <Mark
            kind="partial"
            say={`The task records ${task.attempt_count} attempts and ${ordered.length} documents came back. The rest are missing, not absent, so every figure below describes only the attempts shown.`}
          />
        )}
      </h2>

      {/* SPEND OVER THE RUN, before the cards rather than after them: with
          three or more attempts the question "did the retries cost anything"
          is asked of the run, and answering it requires reading three cards
          and doing the arithmetic -- over a series where some attempts have
          no figure at all, which is where that arithmetic goes wrong. The
          chart states the coverage with the total, always. */}
      <TokenSpendChart attempts={ordered} profile={task.runner_profile} />

      {/* WHERE THE WALL CLOCK WENT, attempt by attempt, and the summed agent
          work under it (redesign-v2 §4 viz #3 and #15). The cards below still
          carry each attempt's start, end and `ran` as facts; this is what
          they cannot show -- that a run spent three minutes waiting for a
          container and eighteen seconds working, and how the retries compare.
          Each phase is drawn between two RECORDED instants and an interval
          with no recorded end is drawn OPEN, never closed at "now". */}
      <AttemptDurations task={task} attempts={ordered} events={run.events} />

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
 * THE STANDING RULES, NO LONGER PRINTED HERE.
 *
 * This was a `<dl>` of seven `<dt>/<dd>` pairs -- roughly a screen of prose
 * under every agent, on every visit, whether or not the reader had ever
 * wondered. It was already an improvement on what came before: each of those
 * had once been a paragraph under the panel it applied to, so a three-attempt
 * run repeated the same four explanations three times and none got read.
 *
 * The owner directive finishes the job. Not one of the seven is a fact about
 * THIS run -- they are invariants of the platform, true of every run there has
 * ever been, which is exactly the material the Help section exists to hold. So
 * they live in `help.ts` now, and what stands here is the footer link §B7.3
 * asks every card to carry.
 *
 * NOTHING MEASURED LEFT WITH THEM, and that is the test. Every figure these
 * rules were qualifying still qualifies itself on the surface: an unmeasured
 * one is a phrase on a dashed tile, a track with no ceiling to read is hatched
 * rather than empty, and a panel that is a real zero says "real zero" in its
 * corner. The prose explained why those shapes mean what they mean. It never
 * carried the meaning itself.
 *
 * Seven pairs, six links: the two that were "a null spend figure is not a
 * zero" and "token cost is the only cost that exists" were always one topic
 * argued twice, and are one topic here.
 */
function AttemptLegend() {
  // B7.4 ADDED THE LAST THREE, and they are the ones this screen's deleted `?`
  // glyphs were carrying: what a dispatch strategy publishes, when an attempt
  // document exists at all, and that a missing credential parks rather than
  // fails. Each was drawn beside a figure that already states its own case;
  // as an entry here each is stated once, for the whole screen, in a row the
  // reader passes in their own reading order rather than one they have to
  // hover to find. The other ten topics this screen names are published at
  // their own labels by `explain` and live in the rail's Help section --
  // putting all thirteen here would rebuild the essay the row replaced.
  const topics: TopicId[] = [
    'requests-are-ceilings',
    'workspace-memory',
    'oom-near-miss',
    'cpu-not-sampled',
    'token-cost',
    'checkpoints',
    'attempt-documents',
    'dispatch-strategies',
    'park-on-missing-credential',
  ]
  // IT IS THE CARD FOOT NOW, not a hand-built inline row. `.ctl-card-foot` is
  // the provenance strip design-system.md §6.1 gives every card, and it draws
  // the same thing the inline object drew -- mono, faint, separated by a
  // surface step rather than by a rule, which is what the spacing probe's
  // separator/boundary test wants from a header or footer edge with no twin.
  return (
    <p className="ctl-card-foot att-legend">
      <span>reading these cards:</span>
      {topics.map((id) => (
        <a key={id} href={`#${HELP[id].anchor}`}>
          {HELP[id].title}
        </a>
      ))}
    </p>
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
  // alone a reclaimed or parked attempt is indistinguishable from a running
  // one: each has a start time and no finish time. This card can see what the
  // document cannot -- whether a later attempt exists, what state the task is
  // in and whose lease it holds -- so the chip is corrected here rather than
  // left saying "running" directly above a paragraph that says the attempt is
  // over. The correction
  // belongs in `attemptOutcome`; that lives in types.ts, which another track
  // owns, so it is reported rather than edited.
  const chip: { label: string; tone: Tone | 'unknown' } =
    end.over && out.label === 'running'
      ? {
          label: end.by === 'superseded' ? 'superseded' : 'ended, no end recorded',
          tone: 'wait',
        }
      : out

  // THE DURATION, WHEN IT CANNOT BE COMPUTED. `attemptRan` measures from the
  // start to NOW when there is no finish time, which is right for a running
  // attempt and false for this one: an attempt that ended without its end
  // being written stopped at some unknown moment, and "1h 40m so far" is a
  // clock still running on a process that is gone. The sentence that said so
  // is the mark's accessible name; what is on the glass is the em dash and
  // `FINISH_NOT_RECORDED`'s own two words, which `measure.ts` already owns.
  const lostEnd = end.over && a.completed_at === null && a.started_at !== null

  return (
    <section className="ctl-card att-card">
      <div className="ctl-card-head">
        <h2 className="ctl-card-title">
          Attempt {ordinal}
          <span className="count-chip">gen {a.generation}</span>
          <Chip tone={chip.tone}>{chip.label}</Chip>
          {/* `latest` IS METADATA, NOT A STATE, and it is the one place on
              these four screens where a hairline box is the right answer --
              Koyeb's rule, quoted in §6.6: their one pill-shaped element is
              metadata in a grey outline, which is how they keep a pill from
              meaning "status". It stays a `.tag`; what changed is that the two
              STATES beside it stopped being one. */}
          {isLatest && <span className="tag">latest</span>}
          {a.oom_near_miss && <Chip tone="bad">OOM near miss</Chip>}
        </h2>
        {/* THE BACKEND IS A STRING THE API CHOSE, SO IT IS BROUGHT INTO THIS
            CONSOLE'S REGISTER RATHER THAN LEFT SHOUTING. `a.backend` arrives
            as `CLOUD_RUN_JOB` and was printed verbatim, two inches from a
            state chip that lowercases the API's `RUNNING` to `running` -- two
            machine tokens of the same kind, on the same line, in two different
            cases. §13.2 of design-system.md allows `text-transform` for
            exactly this and allows it in exactly this direction: quieter, and
            only on a string we did not author. */}
        <span className="ctl-card-note att-backend">{a.backend}</span>
      </div>

      <div className="ctl-card-body">
        {/* EIGHT `<dt>/<dd>` PAIRS BECAME ONE STRIP. The values are short and
            the keys are shorter; a 96px label column down the side of eight
            single-line values is chrome outweighing content, three cards
            deep. An absent value keeps its key and its slot. */}
        <ul className="ctl-facts">
          <li className={`ctl-fact${a.started_at === null ? ' is-absent' : ''}`}>
            <b>start</b>
            {a.started_at === null ? <Em /> : timeAgo(a.started_at)}
          </li>
          {/* Null is three different things and the chip above says which:
              still running, never started, or ended without the field being
              written. */}
          <li className={`ctl-fact${a.completed_at === null ? ' is-absent' : ''}`}>
            <b>end</b>
            {a.completed_at === null ? <Em /> : timeAgo(a.completed_at)}
          </li>
          <li className={`ctl-fact${lostEnd ? ' is-absent' : ''}`}>
            <b>ran</b>
            {lostEnd ? (
              <>
                <Em />{' '}
                <Mark
                  kind="absent"
                  say={`This attempt started ${timeAgo(a.started_at ?? '')} and no finish time was ever written, so how long it ran is unknown.`}
                />
              </>
            ) : (
              attemptRan(a, now)
            )}
          </li>
          <li className={`ctl-fact${a.execution_name === null ? ' is-absent' : ''}`}>
            <b>exec</b>
            {a.execution_name === null ? (
              <>
                <Em />{' '}
                <Mark
                  kind="zero"
                  say="This attempt was never dispatched, so no execution was ever named. That is a fact about the attempt, not a missing record."
                />
              </>
            ) : (
              <span className="mono uri">{a.execution_name}</span>
            )}
          </li>
          <li className="ctl-fact">
            <b>att</b>
            <span className="mono uri">{a.attempt_id}</span>
          </li>
          <li className="ctl-fact">
            <b>lease</b>
            <span className="mono uri">{a.lease_id}</span>
          </li>
        </ul>

        {a.error !== null && <pre className="err full">{a.error}</pre>}

        <AttemptResources a={a} run={run} isLatest={isLatest} />
        <AttemptSpend a={a} profile={run.task.runner_profile} />
        <AttemptCheckpoints a={a} run={run} />
      </div>
    </section>
  )
}

// HAS THIS ATTEMPT ENDED, and on what evidence: `attemptEnd` in duration.ts.
//
// It lived here until the phase chart needed the same answer and grew its own
// copy, and the copy is where the defect was: both read `completed_at`, a
// later attempt and a terminal task, and neither read the task's lease. A
// quota park, a failed dispatch and a reconciler reclaim each leave the task
// non-terminal and the attempt's `completed_at` null, so the card called a
// parked attempt "running" and measured its `ran` against the clock. One
// predicate now, read by the card, the resources panel and the chart, so
// they cannot disagree about the same attempt on the same screen.

/**
 * REQUESTED vs UTILISED, for one attempt.
 *
 * The requested side is `RESOURCE_CLASSES` from the frozen catalogue, served
 * by `/v1/resource-classes`. It is a route rather than a table because a
 * served value cannot drift at all -- `check-contract-parity.sh` now has a
 * TypeScript section, but an asserted copy still has to be edited in two
 * places every time a class is resized, and a route has to be edited in none.
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
  // Which of the three reasons there is no ceiling, as three words. One
  // helper, shared with the metric strip, so the tile and the bars cannot
  // disagree about why the same catalogue is missing.
  const noCeiling = ceilingNote(run)

  // THE ATTEMPT HAS ENDED OR IT HAS NOT, and the same fallback figure means
  // two different things either way. A running attempt has no final peak YET.
  // An attempt that ended without `record_resource_usage` has none at all and
  // none is coming, and calling that figure "live" tells its reader to wait
  // for a write that has already not happened.
  //
  // `completed_at` alone does not separate the two -- it is not written on the
  // paths this distinction exists for. `attemptEnd` (duration.ts) is where
  // the four pieces of evidence this page actually holds are read.
  const end = attemptEnd(a, task, isLatest)
  const ended = end.over

  // WHAT IS KNOWN ABOUT THE END, in the words of what was read. This page
  // cannot see a kill, a reclaim, a park or a crash; it can see a finish
  // time, a later attempt document, the task's state and whose lease the task
  // holds, so it says those and stops.
  //
  // IT IS A SENTENCE STILL, AND IT IS NOT ON THE GLASS. Two `<p className=
  // "muted small">` blocks carried it under every attempt's bars -- around
  // ninety words on a three-attempt run, repeated three times, saying the same
  // thing about the same platform each time. The sentence is now the accessible
  // name of the mark beside the figure it is about, which is where it can never
  // be read as describing a different figure.
  const endedBecause: string =
    end.over === false
      ? ''
      : end.by === 'recorded'
        ? 'Its finish time is recorded, and no peak memory was written with it.'
        : end.by === 'superseded'
          ? 'A later attempt has replaced it, so nothing writes to this document again.'
          : end.by === 'released'
            ? `The task is ${task.state} and no longer holds this attempt’s lease, and this attempt was never marked finished — the shape a quota park, a failed dispatch or a reconciler reclaim leaves behind.`
            : `The task is ${task.state} and this attempt was never marked finished — the shape a kill, or a reconciler reclaim of a stale generation, leaves behind.`
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
      <div className="ctl-toolbar att-sub-head">
        <span className="ctl-eyebrow">requested vs utilised</span>
        {/* THE CEILING'S ABSENCE, AS A MARK RATHER THAN A PANEL. Two
            `.ctl-empty.is-partial` boxes with a heading, a paragraph and a
            foot stood here -- and the foot said "the bars are hatched with no
            fill rather than drawn empty", which is a caption for a thing the
            reader is looking at. The bars below ARE hatched with no fill;
            saying so is the one kind of sentence design-system.md §8.5
            forbids outright. The three causes stay apart, because they send a
            reader to three different places, and they stay apart in the WORD:
            `no catalogue route` / `catalogue unread` / `no class <name>`. */}
        {noCeiling !== null && (
          <span className="is-end ctl-card-note">
            {noCeiling}{' '}
            <Mark
              kind={classes === null ? 'unread' : 'absent'}
              say={
                classesRouteMissing
                  ? 'The deployment answering this UI predates GET /v1/resource-classes, so the ceiling these measurements were taken under is unknown to this page. The measured side is unaffected and is real.'
                  : classes === null
                    ? `The resource-class catalogue could not be read, so only the ceiling is missing; the measurements are real. ${classesDetail ?? ''}`
                    : `The catalogue has no class called ${task.resource_class}. It was renamed or retired after this task was submitted, so there is nothing to compare against.`
              }
            />
          </span>
        )}
      </div>

      <CeilingRow
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
      <CeilingRow
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
      <CeilingRow
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
          oom_near_miss actually asserts -- live ONCE in the card foot at the
          end of this section. Four lines of them repeated under every attempt
          made a three-attempt run unreadable, and a note nobody reads is not a
          note.

          WHAT VARIES PER ATTEMPT IS A MARK NOW. Two paragraphs stood here --
          "the memory figure is the last heartbeat reading before it stopped",
          "this attempt ended with no peak memory recorded" -- roughly sixty
          words under every attempt card. Both are one strip: the mark says
          which kind of nothing, the `by` column beside the bar says where the
          figure came from and how old it is, and the sentence is the mark's
          accessible name. The AGE still reaches a phone, because it is in the
          strip and not only in the `by` column, which is display:none below
          560px. */}
      {a.peak_rss_bytes === null && (liveRss !== null || (ended && a.started_at !== null)) && (
        <p className="att-rss-note">
          {liveRss !== null && hb !== null ? (
            <>
              <Mark
                kind={ended ? 'partial' : 'pending'}
                say={
                  ended
                    ? `This attempt is over, so the memory figure is the last heartbeat reading before it stopped, ${timeAgo(hb.at)} — not a live one. ${endedBecause} The final high-water mark is written at the end of an attempt and this one has already ended without it, so there is none and none is coming.`
                    : `The memory figure is a live reading from the newest heartbeat event on this page, ${timeAgo(hb.at)}, not the final high-water mark — that is written when the attempt ends. The event page is oldest-first and capped, so on a long attempt the newest reading available here can be far older than the agent.`
                }
              />{' '}
              {ended ? 'last' : 'live'} heartbeat {timeAgo(hb.at)}
            </>
          ) : (
            <>
              <Mark
                kind="absent"
                say={`This attempt ended with no peak memory recorded, and no heartbeat carrying one is on this page. ${endedBecause} What it used is unknown, which is why the figure is an em dash rather than a zero, and no later write will fill it in.`}
              />{' '}
              {end.over && end.by === 'superseded'
                ? 'superseded'
                : end.over && end.by === 'task-ended'
                  ? task.state
                  : 'ended'}
            </>
          )}
        </p>
      )}

      {/* THE BAR ABOVE IS THE FIGURE; THIS IS ITS HISTORY. The bar keeps the
          one number and where it came from. The step line shows how the peak
          was reached, from every heartbeat of this attempt on the page -- and
          its title says "peak reached by", because the reading is a running
          maximum and would lie if it were read as memory in use now. */}
      <PeakMemoryChart attempt={a} events={events} />
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
    // THREE DIFFERENT REASONS, and they still need three different answers --
    // but the answer is a MARK plus two or three words, not a paragraph. The
    // one that was collapsed first is the middle one: a running attempt has
    // nothing yet BECAUSE IT HAS NOT FINISHED, and telling its owner the
    // numbers are missing because of an old image sends them to rebuild an
    // image over an attempt that is working correctly. `pending` is the mark
    // for that and it is a different silhouette from `not measured`, which is
    // the distinction design-system.md §8.7.1 says must never collapse: still
    // reading is not nothing reported.
    const running = a.completed_at === null && a.started_at !== null
    const kind: MarkKind = !reports ? 'absent' : running ? 'pending' : a.started_at === null ? 'zero' : 'absent'
    const word = !reports
      ? `${profile} reports none`
      : running
        ? 'written at exit'
        : a.started_at === null
          ? 'never started'
          : 'none recorded'
    const say = !reports
      ? `The ${profile} runner does not report tokens or cost at all. That is an absence of measurement, not a run that cost nothing.`
      : running
        ? "This attempt has not finished. Spend is parsed out of the runner's result and written when the attempt ends, so there is nothing yet — nothing is wrong and nothing needs doing."
        : a.started_at === null
          ? 'This attempt never started, so it consumed no tokens. That is a fact about the attempt rather than a missing measurement.'
          : 'This attempt finished and recorded no usage. Attempts that ran before the worker capture shipped in an agent-runtime-base image carry null for every field — an absent measurement, not a free run.'
    return (
      <div className="section" style={SUB}>
        <p className="att-spend-none">
          <span className="ctl-eyebrow">tokens and cost</span>
          <Mark kind={kind} say={say} /> {word}
        </p>
      </div>
    )
  }

  return (
    <div className="section" style={SUB}>
      <span className="ctl-eyebrow">tokens and cost</span>
      {/* The columns are shown WITH their em dashes rather than omitted:
          omitting them, which the previous attempts table did, also hides the
          numbers on the new attempts that do carry them. */}
      <ul className="ctl-facts">
        <li className={`ctl-fact${a.input_tokens === null ? ' is-absent' : ''}`}>
          <b>in</b>
          {tokens(a.input_tokens)}
        </li>
        <li className={`ctl-fact${a.output_tokens === null ? ' is-absent' : ''}`}>
          <b>out</b>
          {tokens(a.output_tokens)}
        </li>
        <li className={`ctl-fact${a.cache_read_input_tokens === null ? ' is-absent' : ''}`}>
          <b>cache r</b>
          {tokens(a.cache_read_input_tokens)}
        </li>
        <li className={`ctl-fact${a.cache_creation_input_tokens === null ? ' is-absent' : ''}`}>
          <b>cache w</b>
          {tokens(a.cache_creation_input_tokens)}
        </li>
        <li className={`ctl-fact${a.cost_usd === null ? ' is-absent' : ''}`}>
          <b>cost</b>
          {usd(a.cost_usd)}
        </li>
      </ul>
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
      <div className="ctl-toolbar att-sub-head">
        <span className="ctl-eyebrow">checkpoints</span>
        <span className="count-chip">{rows.length}</span>
        {/* RESUMED FROM, AS FACTS. "This attempt did not start from an empty
            workspace" was the sentence; the checkpoint id IS that fact, and
            the file count and byte total beside it are what a reader checks. */}
        {restored !== null && (
          <span className="is-end ctl-card-note">
            resumed {restored.checkpoint_id}
            {restored.from_attempt !== null && ` · from ${restored.from_attempt}`}
            {restored.files !== null && ` · ${restored.files} files`}
            {restored.bytes !== null && ` · ${bytesLabel(restored.bytes)}`}
          </span>
        )}
      </div>

      {rows.length === 0 ? (
        // A REAL ZERO OR AN UNKNOWN, AND THEY ARE DIFFERENT MARKS. The
        // document lists none; whether that is the whole story depends on
        // whether the events were read, because a checkpoint can be recorded
        // in an event alone. Why periodic checkpointing legitimately writes
        // none on a short attempt is `#help/checkpoints`.
        <p className="att-ckpt-none">
          <Mark
            kind={eventsRead ? 'zero' : 'partial'}
            say={
              !eventsRead
                ? "This attempt's document lists no checkpoint, and the event read failed — so a checkpoint recorded only in an event would not be visible here either. Whether this attempt wrote one is unknown."
                : a.started_at === null
                  ? "This attempt's document lists no checkpoint. It never started, so there was nothing to checkpoint."
                  : "This attempt's document lists no checkpoint. Checkpointing is periodic, so an attempt shorter than one interval legitimately writes none."
            }
          />{' '}
          {a.started_at === null ? 'never started' : 'none written'}
        </p>
      ) : (
        /* `is-stacked` — §2 OF `docs/audits/2026-09-23/overflow-inventory.md`
           names this table by name: with the drawer open it hid 57% of itself
           behind an `overflow-x: auto` that paints no scrollbar here, and what
           was hidden is `Size` and `Location`. A checkpoint list showing only
           checkpoint ids is a list that cannot answer the question anybody
           opens it with — whether the thing was actually written and where.
           Below 900px each row becomes a stacked record (§B6.3 in
           `styles.css`); `data-label` supplies the key, as an attribute so the
           rendered-word budgets count the same screen, and the explicit
           `role`s keep the ARIA table that changing `display` drops.

           A PLAIN BLOCK COMMENT, NOT A BRACED JSX ONE. This arm of the ternary
           is an expression position, not JSX children, so a leading brace opens
           an object literal and the file stops parsing — three of these in this
           file, and `tsc` said `TS1005: ')' expected`. Caught by CI on the
           first push of this change, which is the only place it could have
           been caught.

           THE STRIP GOES ABOVE THE TABLE AND DOES NOT REPLACE IT. The table
           is the fact -- every id, size and location as text a reader can
           copy. The strip is the cadence the table cannot show: whether this
           attempt checkpointed steadily or stopped partway. A checkpoint
           whose event is off this page has no instant, so the strip puts it
           in a tray beside the axis rather than at a time nobody recorded. */
        <>
        <CheckpointStrip attempt={a} events={run.events} />
        <div className="ctl-table is-stacked">
          <table role="table">
            <thead role="rowgroup">
              <tr role="row">
                <th role="columnheader" scope="col">Checkpoint</th>
                <th role="columnheader" scope="col" className="is-num">Size</th>
                <th role="columnheader" scope="col">Location</th>
              </tr>
            </thead>
            <tbody role="rowgroup">
              {rows.map((r) => (
                <tr role="row" key={r.checkpoint_id}>
                  <th role="rowheader" scope="row">
                    {r.checkpoint_id}
                    {r.at !== null && <span className="ctl-sub">{timeAgo(r.at)}</span>}
                    {r.eventOnly && (
                      <span className="ctl-sub">not on the attempt document</span>
                    )}
                  </th>
                  <td role="cell" data-label="Size" className="is-num">{r.bytes === null ? <Em /> : bytesLabel(r.bytes)}</td>
                  <td role="cell" data-label="Location">
                    {r.uri === null ? (
                      <>
                        <Em />{' '}
                        <Mark
                          kind={eventsRead ? 'partial' : 'unread'}
                          say={
                            eventsRead
                              ? "This checkpoint's event is not on this page, so its location is unknown. The events route is oldest-first, capped, and returns no page token."
                              : 'The event read failed, so this checkpoint has no location attached. It is unknown rather than missing, and nothing here says whether the checkpoint itself is fine.'
                          }
                        />
                      </>
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
            {/* THE COVERAGE IS A FRACTION, NOT A PARAGRAPH. Where the ids and
                the locations come from is `#help/checkpoints`; what this table
                has to say for itself is how many of its rows have a location,
                and each of those rows already carries its own mark. */}
            {(!eventsRead || missingLocation > 0) && (
              <caption>
                {eventsRead ? rows.length - missingLocation : 0} of {rows.length} located
              </caption>
            )}
          </table>
        </div>
        </>
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
      <p className="att-restore">
        <b>restore</b>
        <span className="mono uri">{latest}</span>{' '}
        <Mark
          kind="unread"
          say="The event read failed, and a checkpoint's uri is only ever recorded on its event — so nothing listed above can be compared with this pointer. Whether it matches a checkpoint of these attempts is unknown."
        />
      </p>
    )
  }
  const known = attempts.some((a) =>
    checkpointsFor(a, run.events).some((r) => r.uri === latest),
  )
  if (known) return null
  return (
    <p className="att-restore">
      <b>restore</b>
      <span className="mono uri">{latest}</span>{' '}
      <Mark
        kind="partial"
        say="No checkpoint listed above matches the task's restore pointer. Either its event is off this page or it was written by an attempt whose document did not come back — it is not evidence the checkpoint is gone."
      />
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

  const retried = attempts !== null ? attempts.length > 1 : task.attempt_count > 1

  return (
    <section className="section panel">
      <div className="ctl-toolbar">
        <h2>Output</h2>
        {/* THE SCOPE LINE IS NOT OPTIONAL, AND IT IS NOW A QUALIFIER.
            Everything in this panel comes from result_summary, which finish()
            writes ONCE at terminal state. On a retried task it is the last
            attempt's output and nothing else, and a panel that does not say so
            gets read as the run's. Three words plus the mark carry that; the
            two sentences explaining that the earlier attempts' output was
            never summarised anywhere are `#help/attempt-documents`.

            THE FALLBACK MATTERS MORE THAN THE PRIMARY. With the attempt read
            failed this panel cannot count attempts -- which is precisely when
            the caveat is most needed -- and it used to vanish, leaving
            artifacts, git outcome and logs rendered unqualified as the run's
            output. `task.attempt_count` is the task's own counter and answers
            the question when the query does not. */}
        {retried && (
          <span className="is-end ctl-card-note">
            <Mark
              kind="partial"
              say={`Everything in this panel describes the LAST attempt only. The result summary is written once, at terminal state; the earlier attempts' output was never summarised anywhere.${attempts === null ? ` The attempt read failed, so the ${task.attempt_count} attempts are the task's own count rather than a document each.` : ''}`}
            />{' '}
            last attempt of {attempts !== null ? attempts.length : task.attempt_count}
          </span>
        )}
      </div>

      {!terminal ? (
        <Absent
          kind="zero"
          heading={`nothing written yet · ${task.state}`}
          say="A result summary is written only when an attempt finishes. This agent has not finished, so there is nothing here — which is not the same as producing nothing."
          explain="attempt-documents"
        />
      ) : summary === null ? (
        <Absent
          kind="partial"
          heading={`finished with no summary · ${task.state}`}
          say="This agent reached a terminal state without a result summary. A parked attempt puts its summary in the event detail and in blocked_by instead, so the timeline below is where to look."
          explain="attempt-documents"
        />
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
        <span className="ctl-eyebrow">artifacts</span>
        <Absent
          kind="partial"
          heading="artifacts is not a list"
          say="This result summary records an artifacts field that is not an array, so nothing here can be listed. The attempt may well have uploaded files — this is a malformed record, not an empty one."
        />
      </div>
    )
  }

  return (
    <div className="section" style={SUB}>
      <div className="ctl-toolbar att-sub-head">
        <span className="ctl-eyebrow">artifacts</span>
        <span className="count-chip">{artifacts.length}</span>
        {skipped.length > 0 && (
          // THE CAP, AS A FRACTION AND A MARK. "N artifacts were skipped for
          // exceeding the size cap, so this list is incomplete" plus the names
          // was a `warn-text` paragraph under the table; the names are data
          // and stay, the sentence is the mark's accessible name.
          <span className="is-end ctl-card-note">
            <Mark
              kind="partial"
              say={`${skipped.length} artifact${skipped.length === 1 ? ' was' : 's were'} skipped for exceeding the size cap, so this list is incomplete: ${skipped.join(', ')}`}
            />{' '}
            {skipped.length} over cap
          </span>
        )}
      </div>
      {artifacts.length === 0 ? (
        <p className="att-none">
          <Mark
            kind="zero"
            say="This attempt uploaded no artifacts. The read succeeded and the list is empty, not missing."
          />{' '}
          none uploaded
        </p>
      ) : (
        /* `is-stacked` (F6), the same three-column shape as the checkpoint
           table above and in the same drawer, so it hides the same two columns
           at the same width. `Location` is the one that matters here: it
           carries the uri and the `copy gsutil` control, and a row whose
           visible part is a name and nothing else offers no way out of the
           console at all. (Unbraced, for the reason the checkpoint table's
           comment gives.) */
        <div className="ctl-table is-stacked">
          <table role="table">
            <thead role="rowgroup">
              <tr role="row">
                <th role="columnheader" scope="col">Artifact</th>
                <th role="columnheader" scope="col" className="is-num">Size</th>
                <th role="columnheader" scope="col">Location</th>
              </tr>
            </thead>
            <tbody role="rowgroup">
              {artifacts.map((a) => (
                <tr role="row" key={a.uri} className={a.name === open ? 'is-open' : undefined}>
                  <th role="rowheader" scope="row">
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
                  <td role="cell" data-label="Size" className="is-num">{bytesLabel(a.bytes)}</td>
                  {/* Still passed by reference as well. The viewer serves a
                      bounded, redacted window; the uri is how someone reads the
                      whole object with their own credentials, which keeps that
                      half of the tenant boundary where IAM already enforces it. */}
                  <td role="cell" data-label="Location">
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
        <span className="ctl-eyebrow">logs</span>
        <Absent
          kind="partial"
          heading="logs is not a map of streams"
          say="This result summary records a logs field that is not an object of stream name to uri, so no stream can be listed from it. The attempt may well have uploaded stdout and stderr — this is a malformed record, not a run without logs. The value is reproduced below so a uri inside it is not lost."
        />
        <pre className="json" style={{ maxHeight: 200, overflowY: 'auto' }}>
          {JSON.stringify(raw, null, 2)}
        </pre>
        <LogsFoot />
      </div>
    )
  }

  return (
    <div className="section" style={SUB}>
      <span className="ctl-eyebrow">logs</span>
      {entries.length === 0 ? (
        <p className="att-none">
          <Mark
            kind="absent"
            say="No log stream was uploaded for this attempt. A stream that failed to upload is absent from this list rather than recorded as empty."
          />{' '}
          none uploaded
        </p>
      ) : (
        <ul className="ctl-facts">
          {entries.map(([label, value]) => (
            <li className="ctl-fact" key={label}>
              <b>{label}</b>
              {typeof value === 'string' ? (
                <>
                  <span className="mono uri">{value}</span>
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
                <>
                  <span className="mono">{JSON.stringify(value)}</span>{' '}
                  <Mark
                    kind="absent"
                    say="This log entry's value is not a string, so it is not a uri and there is nothing to fetch from it."
                  />
                </>
              )}
            </li>
          ))}
        </ul>
      )}
      <LogsFoot />
    </div>
  )
}

/**
 * The standing fact about logs on this platform, in both branches above.
 *
 * THIS USED TO SAY "there is no log-tail read path on the API, so a running
 * agent's output is not readable here at all". That was true when it was
 * written and had stopped being true: `GET /v1/tasks/{id}/logs` serves a byte
 * window of either the final object or the live tail, redacted at read time.
 * Nothing had called it, so the screen went on stating the old constraint --
 * which is worse than a missing feature, because it tells a reader not to look.
 * The "Output, as the agent wrote it" panel below reads it.
 */
function LogsFoot() {
  // WHAT IT SAYS NOW, and it is a pointer rather than an explanation. These
  // are object LOCATIONS from the result summary; the text window, including
  // the live tail, is read by a different route and drawn by `RunFiles` below.
  // A reader who needs to know which is which follows the link.
  return (
    <p className="ctl-card-foot">
      <span>locations only · text window below</span>
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
      <div className="ctl-toolbar att-sub-head">
        <span className="ctl-eyebrow">spend · result summary</span>
        <span className="is-end ctl-card-note">
          <Mark
            kind={unread ? 'unread' : 'partial'}
            say={
              unread
                ? 'The attempt read failed, so whether any attempt document carries a typed spend figure is unknown. What follows is the last attempt’s own result summary — an untyped dict no query can reach — and it describes that attempt only.'
                : 'No attempt document carries a typed spend figure, but the last attempt’s result summary does. These come from an untyped dict that no query can reach, and they describe the last attempt only.'
            }
          />{' '}
          last attempt only
        </span>
      </div>
      <ul className="ctl-facts">
        <li className={`ctl-fact${n('input_tokens') === null ? ' is-absent' : ''}`}>
          <b>in</b>
          {tokens(n('input_tokens'))}
        </li>
        <li className={`ctl-fact${n('output_tokens') === null ? ' is-absent' : ''}`}>
          <b>out</b>
          {tokens(n('output_tokens'))}
        </li>
        <li className={`ctl-fact${n('cache_read_input_tokens') === null ? ' is-absent' : ''}`}>
          <b>cache r</b>
          {tokens(n('cache_read_input_tokens'))}
        </li>
        <li className={`ctl-fact${n('total_cost_usd') === null ? ' is-absent' : ''}`}>
          <b>cost</b>
          {usd(n('total_cost_usd'))}
        </li>
        {Array.isArray(models) && (
          <li className="ctl-fact">
            <b>models</b>
            {(models as string[]).join(', ')}
          </li>
        )}
      </ul>
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
  // WHAT A DISPATCH IS -- chosen at submission, decides what happens to the
  // work rather than how it runs -- is true of every task there has ever been
  // and is `#help/dispatch-strategies`, in the card foot below. The panel shows
  // what THIS task chose, and B7.4 took the `?` off this heading: the facts
  // under it name the strategy and print what it published, which is the topic
  // instantiated for this run rather than described in general.
  return (
    <section className="section panel">
      <div className="ctl-toolbar">
        <h2>Dispatch</h2>
      </div>
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
          <Mark
            kind="unread"
            say="The change could not be read from the workspace. This says nothing about whether the agent did work — only that git could not be asked."
          />{' '}
          {git.error}
        </p>
      ) : null}

      <PublishOutcome git={git} task={task} />

      <ul className="ctl-facts">
        <li className={`ctl-fact${commits.length === 0 ? ' is-absent' : ''}`}>
          <b>commits</b>
          {commits.length === 0 ? (
            <>
              0
              {dirty.length > 0 && (
                <>
                  {' '}
                  <Mark
                    kind="zero"
                    say="The agent edited files and never ran git commit, which is the common case. The changes are in the patch and in the uncommitted count beside this."
                  />
                </>
              )}
            </>
          ) : (
            <>
              {git.commit_count ?? commits.length} on{' '}
              <span className="mono">{git.base ? git.base.slice(0, 10) : '—'}</span>
              {typeof git.insertions === 'number' && typeof git.deletions === 'number' && (
                <> · +{git.insertions} −{git.deletions}</>
              )}
            </>
          )}
        </li>

        {dirty.length > 0 && (
          <li className="ctl-fact">
            <b>dirty</b>
            {git.dirty_count ?? dirty.length}
            {git.dirty_truncated && ' · truncated'}
          </li>
        )}

        <li className={`ctl-fact${patch ? '' : ' is-absent'}`}>
          <b>patch</b>
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
            <>
              <Mark
                kind="partial"
                say="The patch was discarded for exceeding the size cap. It was NOT truncated: a truncated patch applies cleanly and silently drops the rest of the change."
              />{' '}
              discarded at {num(git.patch_bytes)} bytes
            </>
          ) : (
            <>
              <Em />{' '}
              <Mark
                kind="zero"
                say="Nothing differed from the clone, so there is no patch. This is a real zero rather than a patch that failed to upload."
              />
            </>
          )}
        </li>

        {git.branch && (
          <li className="ctl-fact">
            <b>branch</b>
            <span className="mono">
              {git.branch}
              {git.pushed_head && ` · ${git.pushed_head.slice(0, 10)}`}
            </span>
          </li>
        )}

        {git.repository && (
          <li className="ctl-fact">
            <b>repo</b>
            <span className="mono uri">{git.repository}</span>
          </li>
        )}

        {pr && (
          <li className="ctl-fact">
            <b>pr</b>
            <a href={pr.url} target="_blank" rel="noreferrer">
              #{pr.number}
            </a>{' '}
            {/* The pull request's own state, from the same vocabulary as every
                other state on this screen. An open PR is `info` rather than
                `ok`: it is a FACT about the branch, not a verdict that the run
                went well (§1.3 -- the accent is a fact or a link, never a
                verdict), and `is-info`'s flat bar is the mark that says so. */}
            <Chip tone={pr.state === 'open' ? 'info' : 'ok'}>{pr.state}</Chip>
            {pr.created === false && ' · reused'}
          </li>
        )}

        {git.auto_committed && (
          <li className="ctl-fact">
            <b>by</b>
            worker
            <Mark
              kind="partial"
              say="One commit on this branch was made by the worker, not the agent: the agent left changes uncommitted and they would otherwise not have reached the branch at all."
            />
          </li>
        )}
      </ul>

      {/* THE SHAPE OF THE CHANGE, ABOVE THE TABLE THAT STATES IT. The table
          keeps every subject and exact count as text; the diverging bars show
          at a glance which commit carried the work and which only deleted.
          A binary change gets a diamond, never a 0/0 row -- `binary_files`
          exists so it does not read as "changed nothing". */}
      {commits.length > 0 && <DiffstatChart commits={commits} commitCount={git.commit_count} />}

      {commits.length > 0 && (
        /* `is-stacked` (F6), and this is the widest of the drawer's tables:
           four columns in a panel that is 413px at its default and 390 at a
           phone. The subject is the long one and it is the column a reader is
           here for, so squeezing all four is the worst of the options —
           stacked, the sha leads the record and the subject gets the full width
           under it. (Unbraced, for the reason the checkpoint table's comment
           gives.) */
        <div className="ctl-table is-stacked" style={{ marginTop: 10 }}>
          <table role="table">
            <thead role="rowgroup">
              <tr role="row">
                <th role="columnheader" scope="col">Commit</th>
                <th role="columnheader" scope="col">Subject</th>
                <th role="columnheader" scope="col" className="is-num">Files</th>
                <th role="columnheader" scope="col" className="is-num">+/−</th>
              </tr>
            </thead>
            <tbody role="rowgroup">
              {commits.map((c) => (
                <tr role="row" key={c.sha}>
                  <th role="rowheader" scope="row" className="mono">
                    {c.sha.slice(0, 10)}
                  </th>
                  <td role="cell" data-label="Subject">{c.subject}</td>
                  <td role="cell" data-label="Files" className="is-num">
                    {c.files_changed}
                    {/* git prints `-` for both counts on a binary change; those
                        are counted separately rather than folded into a 0/0
                        line count that would read as "changed nothing". */}
                    {c.binary_files > 0 && (
                      <span className="ctl-sub">{c.binary_files} binary</span>
                    )}
                  </td>
                  <td role="cell" data-label="+/−" className="is-num">
                    +{c.insertions} −{c.deletions}
                  </td>
                </tr>
              ))}
            </tbody>
            {(git.commit_count ?? 0) > commits.length && (
              // THE FRACTION IS THE CAVEAT. "the patch above carries all of
              // them" is a standing fact about how the worker harvests, and
              // the patch row above is one line up.
              <caption>
                newest {commits.length} of {git.commit_count}
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
    // THE ONE SENTENCE IS NOT NEGOTIABLE AND IT IS NOT THE EXPLANATION. §8.5
    // allows an empty state one sentence; this is the sentence that stops an
    // operator doing the expensive wrong thing, so it is the one kept and the
    // description of what `integrate` is went to `#help/dispatch-strategies`.
    return (
      <Absent
        kind="zero"
        heading="pushed · no pull request, by request"
        say="This step is a contributor in an integrate workflow. Its branch is on the forge and the integrating step merges it into the single pull request the whole workflow opens."
        foot={reason ?? undefined}
        explain="dispatch-strategies"
      >
        <strong>Do not open one from this branch</strong> — that is the second
        pull request this strategy exists to prevent.
      </Absent>
    )
  }

  // 3. PUSHED, NO PULL REQUEST. The branch is on the forge; only the final API
  //    call did not land. Re-running the agent would be the wrong response.
  if (git.published === true) {
    return (
      <Absent
        kind="partial"
        heading={`pushed · no pull request${git.branch ? ` · ${git.branch}` : ''}`}
        say="The work reached the forge. Only the pull-request call did not complete, so the branch exists and the pull request does not."
        foot={reason ?? undefined}
      >
        Open one by hand from that branch — re-running this agent would
        duplicate work that already exists.
      </Absent>
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
    // WHAT THE THREE STRATEGIES ARE is `#help/dispatch-strategies`, which is
    // where the two sentences about `direct-pr` and `integrate` went. What is
    // on the glass is that nothing was pushed BY REQUEST, which is the fact
    // that stops someone debugging a token scope.
    return (
      <Absent
        kind="zero"
        heading="nothing pushed · collect"
        say="collect is the default strategy: the agent's work is harvested into this task's patch and artifacts and the repository is never written to. Nothing failed, and no token or forge setting changes this."
        foot={reason ?? undefined}
        explain="dispatch-strategies"
      />
    )
  }

  // 5. The tenant's token cannot push. EXPECTED TODAY, and a fact rather than a
  //    failure: the secret holds a clone token, the forge was asked and said
  //    no. The patch is the deliverable.
  if (git.can_push === false) {
    return (
      <Absent
        kind="zero"
        heading="not published · token cannot write"
        say="The forge was asked and refused write access, so the patch is the deliverable. Nothing failed — granting the tenant's credential write scope is what changes this."
        foot={reason ?? undefined}
      />
    )
  }

  // 6. No reason at all.
  if (reason === null) {
    return (
      <Absent
        kind="partial"
        heading="no pull request · no reason recorded"
        say="The worker writes publish_reason on every path, including the successful ones, so its absence means this summary was written by something other than the publish step."
      />
    )
  }

  // 7+. No structured signal beyond `published: false`, so the free-text
  //      reason is matched -- carefully. An unrecognised reason is printed
  //      verbatim and labelled as one, never forced into a bucket.
  //
  // EACH IS NOW A HEADING AND A SENTENCE ON THE MARK, not a heading and a
  // paragraph. The headings are the causes, unchanged in substance and shorter
  // in words, because the cause is what a reader acts on; the reasoning that
  // used to sit under each one is the mark's accessible name. `kind` still
  // separates the six, and it separates them the way §8.7.3 requires: a
  // deliberate outcome is a real zero, a failure to reach the forge is a
  // partial read, and neither is painted as the other.
  const known:
    | { match: string; heading: string; say: string; kind: 'zero' | 'partial' }
    | undefined = [
    {
      match: 'changed nothing',
      heading: 'not published · the agent changed nothing',
      say: 'The workspace was identical to the clone, so there was nothing to push. This is a statement about the run, not about publishing — the agent did no work on the repository.',
      kind: 'zero' as const,
    },
    {
      match: 'parked',
      heading: 'not published · this attempt parked',
      say: 'Publishing waits for the run to finish, and this attempt did not finish — it checkpointed and released its capacity. The next attempt resumes from the checkpoint and publishes then. Nothing is lost and nothing needs doing.',
      kind: 'zero' as const,
    },
    {
      match: 'not on a forge',
      heading: 'not published · not a forge we can publish to',
      say: 'The repository is not on a forge this worker knows how to open a pull request against, so the patch is the deliverable — apply it by hand.',
      kind: 'zero' as const,
    },
    {
      match: 'could not reach the forge',
      heading: 'not published · the forge did not answer',
      say: "The forge could not be reached at all, so nothing is known about whether publishing would have worked. This says nothing about the agent's work, which is in the patch.",
      kind: 'partial' as const,
    },
    {
      match: 'publishing is disabled',
      heading: 'not published · publishing is off for this worker',
      say: 'A configuration decision, not a failure. The patch is the deliverable.',
      kind: 'zero' as const,
    },
    {
      match: 'repository url is unknown',
      heading: 'not published · repository url unknown',
      say: 'The attempt had no repository to publish to. If the task was meant to have one, it was submitted without repository_url.',
      kind: 'zero' as const,
    },
  ].find((c) => lower.includes(c.match))

  if (known !== undefined) {
    return <Absent kind={known.kind} heading={known.heading} say={known.say} foot={reason} />
  }

  // The push itself was rejected -- a GitError, so the reason is git's own
  // message. This is the "something else moved the branch" case and it is the
  // one that needs a person to look at the branch.
  return (
    <Absent
      kind="partial"
      heading="not published · git's own message"
      say="The push did not land. A rejected push usually means something else moved the branch — this screen does not recognise the message well enough to say which, so it is reproduced exactly as the worker recorded it."
      foot={reason}
    />
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

      {/* WHAT THE PROFILE NAME MEANS IS NOT ON THIS PAGE, and saying so was a
          five-line paragraph under every run. It is a standing fact about the
          platform -- the runner-profile catalogue is frozen and no route
          serves it -- so it is `#help/runner-profile-by-name`. B7.4 took the
          `?` that sat beside the profile: the fact a reader needs HERE is which
          profile this run used, and that is the value itself; that no route
          serves the catalogue is a property of the platform, indexed in the
          card foot and in the rail's Help section rather than repeated on every
          run. The SIZING behind the resource class IS served, and stays, as
          figures rather than as a sentence about figures. */}
      <ul className="ctl-facts">
        <li className="ctl-fact">
          <b>profile</b>
          {task.runner_profile}
        </li>
        <li className="ctl-fact">
          <b>class</b>
          {task.resource_class}
          {cls !== null ? (
            <> · {cls.cpu} vCPU · {cls.memory_gib} GiB · {cls.disk_gib} GiB disk · {cls.units}u</>
          ) : (
            <>
              {' '}
              <Mark
                kind={run.classes === null ? 'unread' : 'absent'}
                say={`The sizing behind this class name is unknown: ${noCeiling ?? 'the catalogue did not answer'}.`}
              />
            </>
          )}
        </li>
        <li className={`ctl-fact${task.provider === null ? ' is-absent' : ''}`}>
          <b>provider</b>
          {task.provider ?? <Em />}
        </li>
        {/* Recorded for attribution. It selects NOTHING about the container --
            image, command and resource spec all come from the profile. */}
        <li className={`ctl-fact${task.model === null ? ' is-absent' : ''}`}>
          <b>model</b>
          {task.model ?? <Em />}
        </li>
        <li className="ctl-fact">
          <b>prio</b>
          {task.priority}
        </li>
        <li className={`ctl-fact${task.timeout_seconds === null ? ' is-absent' : ''}`}>
          <b>timeout</b>
          {task.timeout_seconds !== null ? `${task.timeout_seconds}s` : <Em />}
        </li>
        <li className={`ctl-fact${task.repository_url === null ? ' is-absent' : ''}`}>
          <b>repo</b>
          <span className="uri">
            {task.repository_url ?? <Em />}
            {task.repository_ref && ` @ ${task.repository_ref}`}
          </span>
        </li>
      </ul>

      <div className="section" style={SUB}>
        <span className="ctl-eyebrow">prompt</span>
        {prompt === null ? (
          // TWO DIFFERENT FACTS, AND THEY STAY APART. An input that is not an
          // object at all is a malformed submission; an object without a
          // `prompt` key is legitimate, because the key is a convention of the
          // CLI and mock runners rather than part of the schema. That second
          // sentence is `#help/input-is-opaque`.
          <p className="att-none">
            <Mark
              kind={record === null ? 'absent' : 'zero'}
              say={
                record === null
                  ? "This task's input is not an object at all, so it carries no prompt string."
                  : "This task's input has no prompt string. That is legitimate — the field is a convention of the CLI and mock runners, not part of the submission schema. The full input is below."
              }
            />{' '}
            {record === null ? 'input is not an object' : 'no prompt key'}
          </p>
        ) : (
          <pre className="json" style={{ maxHeight: 320, overflowY: 'auto' }}>
            {prompt}
          </pre>
        )}
      </div>

      <div className="section" style={SUB}>
        <span className="ctl-eyebrow">metadata</span>
        {metadata === null ? (
          <p className="att-none">
            <Mark
              kind="absent"
              say="The task document carried no metadata field at all, which is different from being submitted with none."
            />{' '}
            no field
          </p>
        ) : Object.keys(metadata).length === 0 ? (
          <p className="att-none">
            <Mark
              kind="zero"
              say="Submitted with no metadata. The read succeeded and the object is empty, so this is a real zero."
            />{' '}
            submitted with none
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

      {/* redesign-v2 Panel 3: the files staged into the workspace, each linked
          back to the run that produced it. Renders nothing for a run that
          declared no input and reported none. See StagedInputs.tsx. */}
      <StagedInputs task={task} />

      {input !== null && input !== undefined && (
        <div className="section" style={SUB}>
          <span className="ctl-eyebrow">full input</span>
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
        <span className="ctl-eyebrow">timeline</span>
        <Absent
          kind="failed"
          heading="Event history"
          say="The event history could not be read. This is a failed read, not an empty history."
        >
          {detail ?? undefined}
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
        <span className="ctl-eyebrow">timeline</span>
        <Absent
          kind="failed"
          heading="0 events · a task always has one"
          say="This task must have at least a submitted event — it is written in the same batch as the task itself — and the query returned none. This is a failed read, not an empty history."
        />
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
      <div className="ctl-toolbar">
        <h2>
          Timeline
          <span className="count-chip">{events.length}</span>
        </h2>
        {/* WHAT THE PAGE DOES AND DOES NOT COVER, AS ONE QUALIFIER.
            Two paragraphs stood here -- one for the proven case (a terminal
            task with no terminal event, which proves the page ends before the
            run did) and one for the unproven (the route caps the page and
            names neither the cap nor the remainder). Both said the same thing
            about the route, which is `#help/partial-read`; what differs is
            whether this page can PROVE it is short, and that is the difference
            between a `partial` mark and a `pending` one. */}
        <span className="is-end ctl-card-note">
          <Mark
            kind={endMissing ? 'partial' : 'pending'}
            say={
              endMissing
                ? `The task is ${task.state} and a terminal task writes a terminal event — none is on this page. The route orders events oldest-first, caps the page server-side and returns no page token, so the newest events are not reachable from this screen at all. The end of this task's history is missing, not absent.`
                : "One page, oldest first. This screen asks for no page size, so the cap is whatever the deployment's default is, and the response says neither what it was nor how many events were left out — a page that looks complete is not evidence that it is."
            }
          />{' '}
          {endMissing
            ? lastEvent === undefined
              ? 'ends early'
              : `ends at ${lastEvent.type}, ${timeAgo(lastEvent.at)}`
            : 'oldest first · cap unknown'}
        </span>
      </div>

      {groups.map((g) => (
        <div className="section panel" key={g.key}>
          <h2>
            {g.label}
            <span className="count-chip">{g.events.length}</span>
          </h2>
          {g.events.length === 0 ? (
            <p className="att-none">
              <Mark
                kind="partial"
                say="No event on this page belongs to this attempt — the page ends before them, or none were written. The two cannot be told apart from here."
              />{' '}
              none on this page
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
