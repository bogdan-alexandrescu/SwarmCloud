import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  loadWorkflowBoard,
  loadWorkflowUsage,
  type StepUsage,
  type WorkflowBoard,
  type WorkflowUsage,
} from './api'
import {
  edgePath,
  layoutOf,
  levelsOf,
  profileMix,
  shapeOf,
  stepDuration,
  workflowSpend,
  NODE_W,
  PAD,
  COL_GAP,
  type DagShape,
  type StepDuration,
  type WorkflowSpend,
} from './dag'
import {
  absentCell,
  costCell,
  countCell,
  durationText,
  measuredCell,
  tokenCell,
  type Absence,
  type Cell,
  FINISH_NOT_RECORDED,
  NEVER_RAN,
  NO_ATTEMPT_YET,
  STATE_UNREAD,
  TOKENS_NOT_REPORTED,
  USAGE_NOT_READ,
  USAGE_NOT_SAMPLED,
} from './measure'
import { workflowDispatchOf } from './Dispatch'
import { HelpCard } from './HelpCard'
import { Id, Screen, timeAgo } from './Shell'
import { StopRun } from './StopRun'
import {
  consequenceOf,
  dispatchOf,
  stateGlyph,
  stateTone,
  stepState,
  type StepState,
  type Task,
  type TaskDispatch,
  type Tone,
  type Workflow,
  type WorkflowDrift,
  type WorkflowStep,
  workflowHeaderState,
  TERMINAL_STATES,
} from './types'

/**
 * The workflow board. TWO FORMS OF ONE THING, and the collapsed one is the
 * default because it is the one a reader lands on.
 *
 * COLLAPSED is a single horizontal bar per workflow, the way a CI list is a
 * row per run: identity, derived state, progress, SHAPE, runner mix, spend and
 * when it last moved. The point of the line is to let somebody pick which of
 * ten workflows to open WITHOUT OPENING ANY, and the field that earns its place
 * hardest is the shape -- `1 → 5 → 1` next to a mini-map of the real edges.
 * Five steps in parallel and five steps in a chain have the same count, the
 * same fraction done and the same cost; they are completely different runs, and
 * a list that renders them identically is the defect the graph work exists to
 * fix. It must survive being collapsed or it has not been fixed.
 *
 * EXPANDED is a canvas: real edges drawn between generous node cards, flowing
 * left to right along dependency depth. Every node keeps the three facts it has
 * always carried -- the step NAME, its STATUS and its RUNNER PROFILE -- and
 * adds the one that was missing, how long it has taken.
 *
 * Step state is JOINED, not read off the step. `GET /v1/workflows` returns
 * steps with no state field at all; it only exists on the task a step created.
 * See `stepState` in types.ts for the three ways that join can come up empty
 * and why they must not render alike.
 *
 * WHERE THE SENTENCES WENT (design-system.md §8). This screen carried three
 * paragraphs of standing explanation -- a 44-word partial-read banner, a
 * 26-word sample note, and one absence note per node -- and every one of them
 * stated a fact the reader could already SEE if the fact had been drawn. They
 * are now drawn: `.ctl-mark` names which kind of nothing this is in two words,
 * the untrusted progress track is hatched with no fill rather than described
 * in amber prose, and the sentence itself is the mark's `aria-label` plus a
 * `?` card over an existing `help.ts` topic. The invariant is unchanged and is
 * STRONGER for it: a paragraph can sit beside a figure it does not describe,
 * and an attribute on the figure cannot.
 */
export function WorkflowsScreen() {
  // Same reason as AgentDetail's: `Screen` keeps its retry nonce to itself, so
  // a mutation inside the board (stopping a step) needs a key bump to make the
  // board re-read. Without it the node a moment ago said RUNNING would keep
  // saying it after the request was recorded.
  const [reloads, setReloads] = useState(0)
  const reload = useCallback(() => setReloads((n) => n + 1), [])

  // ABOVE THE `key`, DELIBERATELY. `reloads` remounts `Screen`, so anything
  // held inside it is lost on every stop-and-reload; a board that snapped every
  // open workflow shut the moment you stopped one step would be unusable.
  const [mode, setMode] = useState<BoardMode>('collapsed')
  const [open, setOpen] = useState<Record<string, boolean>>({})

  // A board-wide instruction overrules the per-card ones. Keeping stale
  // overrides would make "Collapse all" leave three cards open with no way to
  // tell why.
  const chooseMode = useCallback((m: BoardMode) => {
    setMode(m)
    setOpen({})
  }, [])

  const toggle = useCallback(
    (id: string, expanded: boolean) => setOpen((o) => ({ ...o, [id]: !expanded })),
    [],
  )

  return (
    <Screen
      key={reloads}
      title="Workflows"
      load={loadWorkflowBoard}
      summary={(d) => `${d.workflows.length} workflow${d.workflows.length === 1 ? '' : 's'}`}
      // ONE SENTENCE (design-system.md §6.9: mark, heading, one sentence, a
      // link out). The second sentence -- that the read succeeded and returned
      // nothing -- is what `.ctl-empty`'s default variant already means; it
      // said in words what the variant says by being the variant.
      empty={{
        heading: 'No workflows',
        body: 'Individually submitted tasks appear under Agents.',
      }}
    >
      {(d) => (
        <Board
          board={d}
          mode={mode}
          chooseMode={chooseMode}
          open={open}
          toggle={toggle}
          reload={reload}
        />
      )}
    </Screen>
  )
}

/**
 * The board, plus the SECOND read the step figures need.
 *
 * Separate from `WorkflowsScreen` because `Screen`'s children is a render prop:
 * hooks may not live inside it. It is also the right seam -- the attempt read
 * is deliberately started only once the board itself has landed, so the graph
 * is on screen while the figures are still arriving rather than after.
 */
function Board({
  board,
  mode,
  chooseMode,
  open,
  toggle,
  reload,
}: {
  board: WorkflowBoard
  mode: BoardMode
  chooseMode: (m: BoardMode) => void
  open: Record<string, boolean>
  toggle: (id: string, expanded: boolean) => void
  reload: () => void
}) {
  // Every task a step points at, in board order. `loadWorkflowUsage` dedupes
  // and caps; the order decides which steps fall inside the cap, so it is the
  // board's own order rather than a set's iteration order.
  const taskIds = useMemo(
    () =>
      board.workflows.flatMap((w) =>
        w.steps.map((s) => s.task_id ?? null).filter((id): id is string => id !== null),
      ),
    [board.workflows],
  )

  const [usage, setUsage] = useState<UsageRead>({ kind: 'reading' })

  useEffect(() => {
    let live = true
    setUsage({ kind: 'reading' })
    loadWorkflowUsage(taskIds).then((r) => {
      if (!live) return
      if (r.status === 'ok') {
        setUsage({ kind: 'ready', usage: r.data })
        return
      }
      // `empty` is a real answer: no step has a task yet, so there is nothing
      // to read and nothing failed. The per-step cells then say "not started",
      // which is what the state join already says.
      if (r.status === 'empty') {
        setUsage({ kind: 'ready', usage: null })
        return
      }
      if (r.status === 'stale') {
        setUsage({ kind: 'ready', usage: r.data })
        return
      }
      if (r.status === 'error') {
        setUsage({ kind: 'failed', detail: r.error.message })
        return
      }
      // `loading` is not a value this promise resolves to, but leaving the
      // nodes in `reading` for ever if it ever did is a silent stall, and a
      // placeholder that never stops moving is the worst of the three states.
      setUsage({ kind: 'failed', detail: 'The attempt read did not complete.' })
    })
    return () => {
      live = false
    }
  }, [taskIds])

  return (
    <>
      {/* ONE STRIP OF CHROME ABOVE THE BOARD, and the count of sentences in it
          is zero (design-system.md §6.11). The control sits left; everything
          the board could not read sits right, as marks. Both banners this
          replaces were full-width panels that pushed the first row of actual
          data below the fold on a laptop. */}
      <div className="ctl-toolbar wf-chrome">
        <ModeControl mode={mode} onChoose={chooseMode} />
        <span className="is-end wf-caveats">
          {board.statesDetail !== null && <StatesUnavailable detail={board.statesDetail} />}
          {usage.kind === 'ready' && usage.usage !== null && <SampleNote usage={usage.usage} />}
          {/* ONE `?` FOR THE WHOLE BOARD. `absent-vs-zero` is the rule every
              absent figure on this screen obeys -- a word where a digit would
              be, on a dashed rule -- and it is a property of the screen rather
              than of any one node. Drawn per node it would have been eight
              question marks on an eight-step graph. */}
          <HelpCard topic="absent-vs-zero" />
        </span>
      </div>
      <div className="wf-board">
        {board.workflows.map((w) => (
          <WorkflowCard
            key={w.workflow_id}
            workflow={w}
            taskById={board.taskById}
            expanded={open[w.workflow_id] ?? mode === 'full'}
            onToggle={toggle}
            reload={reload}
            usage={usage}
          />
        ))}
      </div>
    </>
  )
}

type BoardMode = 'collapsed' | 'full'

/**
 * The board-wide default, as `.ctl-seg` rather than two standalone pills.
 *
 * ONE BORDERED GROUP, AND THE SELECTION IS HUELESS (design-system.md §6.11 and
 * §1.3). `.wf-mode.is-on` filled itself with 12% `--info` -- the same accent a
 * LIVE step is drawn in three rows below -- which is how a reader learns to
 * stop trusting colour as a state channel. The active segment is now a surface
 * step plus weight, and it costs no hue at all.
 *
 * The labels are one word each. "Collapsed"/"Full DAG" named the mechanism;
 * `Rows`/`Graph` name what you get, which is the thing being chosen.
 */
function ModeControl({ mode, onChoose }: { mode: BoardMode; onChoose: (m: BoardMode) => void }) {
  return (
    <div className="ctl-seg" role="group" aria-label="How much of each workflow to show">
      {(
        [
          ['collapsed', 'Rows'],
          ['full', 'Graph'],
        ] as const
      ).map(([value, label]) => (
        <button
          key={value}
          type="button"
          aria-pressed={mode === value}
          onClick={() => onChoose(value)}
        >
          {label}
        </button>
      ))}
    </div>
  )
}

/**
 * The partial state: the workflows read succeeded and the task read did not.
 *
 * WHAT IT WAS. A full-width amber panel carrying 44 words across a heading and
 * two paragraphs, whose entire content was that the shapes below are
 * trustworthy and the states are not.
 *
 * WHAT CARRIES IT NOW, and why this is not a weakening. The states themselves
 * were ALREADY drawn as unread: `present()` returns the word `state unread`
 * for that arm, `.node.unknown` is dashed and amber, and `.wf-state.unknown`
 * is the neutral tone rather than a state colour. The banner never carried the
 * fact -- it explained a fact that was already on screen, once, at the top,
 * where it could sit above a workflow it did not describe. What is here now is
 * a two-word mark in the board's own chrome, the sentence as its accessible
 * name, and the `?` over `read-failed`, which is the topic that already holds
 * the argument in full.
 */
function StatesUnavailable({ detail }: { detail: string }) {
  return (
    <span className="wf-caveat" role="status">
      <span
        className="ctl-mark is-unread"
        aria-label={`Step states could not be read: ${detail}. The steps below, their order and their dependencies are correct; their states are not shown. Nothing below is evidence that a step is or is not running.`}
      >
        states unread
      </span>
      <HelpCard topic="read-failed" />
    </span>
  )
}

/**
 * The rollup line, read off what the SERVER computed.
 *
 * This used to census the joined tasks here. It no longer does, and the reason
 * is the whole point of the change behind it: the API now derives a workflow's
 * state from its steps on every read, so a second census in the browser would be
 * a restatement of the server's rule with nothing checking the two still agree
 * -- `check-contract-parity.sh` does not cover TypeScript.
 *
 * Counts are still SUPPRESSED when any step state is unknown, because that
 * property belongs to the numbers rather than to where they were computed:
 * "3 succeeded" over a partial read is a wrong number wearing the clothes of a
 * right one. The server reports `complete: false` for exactly that case.
 */
interface Rollup {
  text: string
  trustworthy: boolean
  done: number
  total: number
  /**
   * The sentence the amber paragraph used to be, as the progress control's
   * accessible name. It is not optional and it is not decoration: it is the
   * keyboard-and-screen-reader half of the encoding, and the visual half is
   * the hatched track (design-system.md §8.3).
   */
  why: string
}

function rollupLine(workflow: Workflow): Rollup {
  const roll = workflow.rollup
  const total = workflow.steps.length
  if (!roll) {
    // `state_source: 'stored'` -- no read route produces this, but a payload
    // without a rollup must say it has no census rather than invent a zero one.
    return {
      // "not counted" attached to the STEP COUNT is the defect this line
      // was rewritten to remove: it rendered "3 steps - not counted" and
      // told the reader the console could not count to six. The count is
      // the length of an array that was read and is always knowable. What
      // is missing here is the per-step census, so that is what says so.
      // THE COUNT ALONE. "progress not derived" went with the amber prose: the
      // hatched track beside this text is the statement that there is no scale
      // to start, and it makes it at 390px where three ellipsed words could
      // not. The sentence is the track's accessible name.
      text: `${total} step${total === 1 ? '' : 's'}`,
      trustworthy: false,
      done: 0,
      total,
      why: `${total} steps. No per-step census was derived for this workflow, so no progress is shown. The step count itself was read and is exact.`,
    }
  }
  if (!roll.complete) {
    const n = roll.unreadable_steps.length
    // `steps unread`, not `steps: state unread`. The colon-and-restatement was
    // the only part a reader could not have got from the hatch.
    return {
      text: `${n} of ${total} steps unread`,
      trustworthy: false,
      done: 0,
      total,
      why: `${n} of ${total} steps could not be read (${roll.reason}), so there is no progress figure. This is an unread census, not a stalled workflow.`,
    }
  }
  const done = roll.counts.SUCCEEDED ?? 0
  const failed = roll.counts.FAILED ?? 0
  const unstarted = roll.counts.unstarted ?? 0
  const parts = [`${done}/${total} done`]
  if (failed > 0) parts.push(`${failed} failed`)
  if (unstarted > 0) parts.push(`${unstarted} not started`)
  return {
    text: parts.join(' · '),
    trustworthy: true,
    done,
    total,
    why: `${done} of ${total} steps done.`,
  }
}

/**
 * The accounting-drift check, for a workflow's two records of its own state.
 *
 * Modelled on `Drift` in Holders.tsx and there for the same reason: the stored
 * `workflow.state` and the state derived from the steps are two records of one
 * fact, so a disagreement is never rounding. It is shown AFTER the server has
 * already repaired it, on purpose -- a repair that leaves no trace is a silent
 * resolution, and the thing worth seeing is that the stored copy was wrong at
 * all, not the instant in which it was wrong.
 *
 * `agrees === null` is rendered as "not checked", never as a disagreement: the
 * derivation was incomplete, so the two records were not compared.
 *
 * TWO RECORDS SIDE BY SIDE BEAT A SENTENCE ABOUT TWO RECORDS. This was two
 * prose paragraphs that spelled out, in 27 words, a comparison the reader
 * makes instantly when the two values are set next to each other with their
 * keys on (design-system.md §6.13). `.ctl-facts` is that shape, and the
 * unchecked arm keeps its slot and its key rather than vanishing -- a fact
 * that disappears is indistinguishable from one that was never going to be
 * there.
 */
function StateDrift({ drift }: { drift: WorkflowDrift | undefined }) {
  if (!drift || drift.agrees === true) return null
  if (drift.agrees === null) {
    return (
      <ul
        className="ctl-facts wf-drift"
        aria-label={`State could not be checked: ${drift.unreadable_steps.length} step${
          drift.unreadable_steps.length === 1 ? '' : 's'
        } unread (${drift.reason}). The stored value is ${drift.stored} and nothing has confirmed it.`}
      >
        <li className="ctl-fact">
          <b>stored</b>
          <code>{drift.stored}</code>
        </li>
        <li className="ctl-fact is-absent">
          <b>steps</b>
          <span className="ctl-mark is-unread">not checked</span>
        </li>
      </ul>
    )
  }
  return (
    <ul
      className="ctl-facts wf-drift"
      aria-label={`Stored state was ${drift.stored}; the steps say ${drift.derived}${
        drift.repaired ? ', and the stored copy has been corrected' : ''
      }.`}
    >
      <li className="ctl-fact">
        <b>stored</b>
        <code>{drift.stored}</code>
      </li>
      <li className="ctl-fact">
        <b>steps</b>
        <code>{drift.derived}</code>
      </li>
      {/* A plain value, NOT a `.ctl-mark`: the mark vocabulary names kinds of
          absence, and a repair is a thing that happened. Borrowing an absence
          mark for it would put a measured event in the vocabulary a reader has
          learned to read as "nothing here". */}
      {drift.repaired && (
        <li className="ctl-fact">
          <b>stored</b>repaired
        </li>
      )}
    </ul>
  )
}

export function WorkflowCard({
  workflow,
  taskById,
  expanded,
  usage,
  onToggle,
  reload,
}: {
  workflow: Workflow
  taskById: ReadonlyMap<string, Task> | null
  expanded: boolean
  usage: UsageRead
  onToggle: (id: string, expanded: boolean) => void
  reload: () => void
}) {
  const roll = rollupLine(workflow)
  const shape = shapeOf(workflow.steps)
  const spend = workflowSpend(workflow.steps, taskById)
  const header = workflowHeaderState(workflow)
  const bodyId = `wf-body-${workflow.workflow_id}`

  return (
    <section className={`section wf-card${expanded ? ' is-open' : ''}`}>
      {/* STILL AN <h2>, and the button is inside it rather than around it. The
          bar is this panel's heading -- it is how the workflow is named on the
          board -- and demoting it to a bare <button> would take the row out of
          the document outline that every other section is in. `.section > h2`
          uppercases, so `.wf-bar` turns that off again for its own contents;
          B17's rule on `.id` is what keeps the id itself lowercase either
          way, and it is asserted on the rendered style in brand.test.tsx. */}
      <h2 className="wf-h">
        <button
          type="button"
          className="wf-bar"
          aria-expanded={expanded}
          aria-controls={bodyId}
          onClick={() => onToggle(workflow.workflow_id, expanded)}
        >
          {/* THE MARK, AT `[state]`, AND IT NEVER DROPS (design-system.md
              §6.8). 8px of shape at the left edge is what makes a list of ten
              workflows scannable without reading a word of it, and it is the
              one column that survives to 390px along with the name and the
              caret. `dotClass` is where the two absences finally stop drawing
              alike: `state not derived` is a ring with a bar through it and
              `state unread` is a hollow ring, where the old row gave both the
              same grey `?`. */}
          <i className={dotClass(header)} aria-hidden />
          {/* B17. This id is lowercase everywhere it actually lives --
              Firestore, the API, the logs and the `#agents/task/<id>` address.
              Printed as WF_BCDC9180… it cannot be pasted anywhere, which is
              the only thing an id is for. */}
          <Id>{workflow.workflow_id}</Id>
          {/* `workflowHeaderState`, NOT `workflow.state`. The same field name
              carries two different meanings depending on which server answered:
              derived from the step tasks on this read, or the Firestore cache
              that nothing advanced before rollup.py existed and which therefore
              reads QUEUED for a workflow's whole life. Printing it raw is how
              this card came to say "queued" in its heading while the step chips
              under it read succeeded and failed -- one card, one typeface, and
              nothing saying which to believe. An underived read claims no state
              at all and names the stored copy as a stored copy. */}
          {/* THE WORD STAYS, and the glyph went with the dot that replaced it.
              A chip carrying a mark AND a glyph AND a word said the same thing
              three times in 104px. */}
          <span className={`wf-state ${header.tone}`} title={header.title}>
            {header.word}
          </span>
          <Progress roll={roll} />
          <Shape shape={shape} />
          <Mix steps={workflow.steps} />
          <Spend spend={spend} />
          <span className="wf-when" title={`Last state change: ${workflow.updated_at}`}>
            {timeAgo(workflow.updated_at)}
          </span>
          {/* ALWAYS RENDERED, empty or not. `.wf-bar` is a grid with one column
              per field, and a conditionally-absent child would shift every
              field after it into the wrong column on exactly the rows that
              have something to say. */}
          <span className="wf-flags">
            {/* Still a separate annotation, not folded into the state above.
                `cancel_requested` is a REQUEST: a step holding a lease keeps it
                until the worker or the reconciler releases it, so between the
                request and the release the workflow really is still running. */}
            {workflow.cancel_requested && <span className="tag wait">cancel requested</span>}
          </span>
          {/* `[actions]`, AT THE RIGHT EDGE, 20px, AND IT NEVER DROPS EITHER.
              It was the first column: a caret on the left pushes the id -- the
              thing a reader scans down -- off the row's own left edge, so ten
              rows have ten ragged starts. Railway, Northflank and Vercel all
              put the disclosure last for that reason. */}
          <span className="wf-caret" aria-hidden>
            {expanded ? '▾' : '▸'}
          </span>
        </button>
      </h2>

      {expanded && (
        <div className="wf-body" id={bodyId}>
          <StateDrift drift={workflow.drift} />
          <WorkflowDispatchLine workflow={workflow} taskById={taskById} />
          <WorkflowGraph workflow={workflow} taskById={taskById} usage={usage} reload={reload} />
        </div>
      )}
    </section>
  )
}

/**
 * Which silhouette the row's state mark takes.
 *
 * FIVE TONES, SIX MARKS, because `unknown` is two different facts and the old
 * row drew them as one grey `?`:
 *
 *  - `derived: false` -- this API did not derive a state at all. The state
 *    EXISTS; nobody computed it. `.ctl-dot.is-underived`, a ring with a bar
 *    through it (design-system.md §6.6).
 *  - `derived: true` with an incomplete rollup -- the derivation was attempted
 *    and some steps could not be read. An absence of information, drawn as the
 *    default hollow ring.
 *
 * Neither is a filled mark, so neither can be read as a state the platform
 * holds -- which is the whole of the invariant, restated as a shape.
 */
function dotClass(header: { tone: Tone | 'unknown'; derived: boolean }): string {
  if (header.tone === 'unknown') {
    return header.derived ? 'ctl-dot' : 'ctl-dot is-underived'
  }
  switch (header.tone) {
    case 'ok':
      return 'ctl-dot is-ok'
    case 'bad':
      return 'ctl-dot is-bad'
    case 'live':
      return 'ctl-dot is-live'
    // QUEUED, PARKED, READY. `--info` is this sheet's "a fact, not a verdict"
    // (§1.2), which is exactly what a waiting workflow is: it is not a
    // failure, it is not healthy, and it is certainly not an absence.
    case 'wait':
      return 'ctl-dot is-info'
  }
}

/**
 * PROGRESS, and the reason the bar is sometimes hatched rather than absent.
 *
 * A FILLED METER IS A CLAIM THAT THE NUMBERS BEHIND IT ARE A CENSUS. When the
 * server reports `complete: false` the census failed, and drawing "2 of 6" as
 * a two-thirds-empty bar would turn a failed read into a measurement -- the
 * exact substitution this console exists to refuse. That has not changed: no
 * `.wf-meter-fill` is rendered on that path, and there is no width to read.
 *
 * WHAT CHANGED IS THE OTHER HALF. The untrusted case used to draw NO track at
 * all and lean entirely on amber words -- and `.wf-progress-text` is one of
 * the columns that drops at 560px, so at 390px an unreadable census showed as
 * an empty cell, which is the strongest possible way of saying "nothing is
 * wrong here". It now draws `.wf-meter.is-unknown`: hatched, no fill, and no
 * axis, because there is no scale to start (design-system.md §6.4). That mark
 * survives every breakpoint, survives greyscale, and cannot be mistaken for a
 * 0% bar because a 0% bar has an axis tick and this has none.
 */
function Progress({ roll }: { roll: Rollup }) {
  if (!roll.trustworthy) {
    return (
      <span className="wf-progress untrusted">
        <span className="ctl-track wf-meter is-unknown" role="img" aria-label={roll.why} />
        <span className="wf-progress-text">{roll.text}</span>
      </span>
    )
  }
  const pct = roll.total === 0 ? 0 : Math.round((roll.done / roll.total) * 100)
  return (
    <span className="wf-progress">
      <span className="ctl-track wf-meter" role="img" aria-label={roll.why} title={roll.text}>
        <span className="ctl-util-fill wf-meter-fill" style={{ width: `${pct}%` }} />
      </span>
      <span className="wf-progress-text">{roll.text}</span>
    </span>
  )
}

/**
 * THE TOPOLOGY, COLLAPSED. Two renderings of one layout: the widths as text
 * (`1 → 5 → 1`) and a mini-map drawn from the SAME `layoutOf` the expanded
 * canvas uses, scaled down, with the real edges and one dot per step coloured
 * by that step's state.
 *
 * The text is what a screen reader and a test can read; the map is what the eye
 * gets in 90 pixels. Neither is decoration: without them a fan-out and a chain
 * are the same row.
 */
function Shape({ shape }: { shape: DagShape }) {
  // NO MINI-MAP. The collapsed row draws no graph at all: a 60px thumbnail of
  // a six-node DAG resolves into a smudge at the size a one-line row allows,
  // and a picture too small to read is worse than no picture -- it occupies
  // the space a legible fact would have had. The graph is what expanding is
  // FOR. What the row keeps is the shape as text, `1 -> 5 -> 1`, which tells a
  // fan-out from a chain at a glance, survives a screen reader, and costs one
  // column.
  return (
    <span className="wf-shape" title={shape.label}>
      <span className="wf-shape-text" data-kind={shape.kind}>
        {shape.text}
      </span>
    </span>
  )
}



/**
 * THE RUNNER MIX. Which models this workflow is spending the subscription on,
 * commonest first, two named and the rest counted.
 *
 * It earns the line because it is the field that decides whether a quota park
 * is about to matter to THIS workflow: a twenty-step run that is nine
 * claude-code steps and a browser step behaves nothing like one that is twenty
 * mock steps, and the difference is invisible from the id, the state and the
 * progress. The full per-step profile stays on every expanded node; this never
 * replaces it.
 */
function Mix({ steps }: { steps: WorkflowStep[] }) {
  const mix = profileMix(steps)
  if (mix.length === 0) return <span className="wf-mix" />
  const shown = mix.slice(0, 2)
  const rest = mix.length - shown.length
  const all = mix.map((m) => `${m.profile} ×${m.count}`).join(', ')
  return (
    <span className="wf-mix" title={`Runner profiles: ${all}`}>
      {shown.map((m) => (
        <span className="wf-chip" key={m.profile}>
          {m.profile}
          <span className="wf-chip-n">×{m.count}</span>
        </span>
      ))}
      {rest > 0 && <span className="wf-chip more">+{rest}</span>}
    </span>
  )
}

/**
 * SPEND, and the one rule that outranks everything on this screen.
 *
 * `usd === null` means NO STEP REPORTED A COST. It is rendered "not reported",
 * never `$0.00`: an absent measurement is not a free run. A step that reported
 * `0` -- a mock profile does exactly that -- is a MEASURED zero and renders as
 * a digit.
 *
 * The coverage travels with the figure whenever it is partial, because a total
 * over three of six steps is not the workflow's spend. Four decimals under ten
 * dollars for the same reason Overview uses them: a single attempt is routinely
 * worth $0.0312, and $0.03 loses a third of the figures on this board.
 */
function Spend({ spend }: { spend: WorkflowSpend }) {
  if (spend.usd === null) {
    // `title` AND `aria-label`. The title was already here and was never the
    // only route -- the word `not reported` is on the surface, in the absent
    // treatment, which is what the acceptance test asks for -- but a hover is
    // not a keyboard, and the sentence is the half that says what it is NOT.
    const why =
      spend.joined === 0
        ? 'No task was joined for this workflow, so nothing could have reported a cost. This is an absent measurement, not $0.00.'
        : `None of the ${spend.joined} joined step${spend.joined === 1 ? '' : 's'} reported a cost. This is an absent measurement, not $0.00.`
    return (
      <span className="wf-spend absent" title={why} aria-label={why}>
        not reported
      </span>
    )
  }
  const partial = spend.covered < spend.steps
  const note = `${spend.covered} of ${spend.steps} steps reported a cost.${
    partial ? ' The rest have not reported one, so this is a floor rather than the total.' : ''
  }`
  return (
    <span className="wf-spend" title={note} aria-label={note}>
      {money(spend.usd)}
      {partial && (
        <span className="wf-spend-cov">
          {spend.covered}/{spend.steps}
        </span>
      )}
    </span>
  )
}

/** Dollars of token cost. Four decimals under ten dollars: a single attempt is
 *  routinely worth $0.0312, and rounding that to $0.03 loses a third of the
 *  figures on this board to "$0.00". */
function money(v: number): string {
  return v < 10 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`
}

/**
 * HOW MANY PULL REQUESTS THIS WORKFLOW IS GOING TO PRODUCE.
 *
 * The same sentence the submit form showed when it was chosen, computed from
 * the same function over the same step count -- so "I picked one pull request"
 * and "this workflow will open one pull request" cannot come apart.
 *
 * EXPANDED ONLY, and that is a judgement rather than an oversight: the strategy
 * does not change while a workflow runs, so it cannot help a reader choose
 * which of ten rows to open. It is a property of what comes out at the end,
 * which is a thing you read once you have opened the one you care about.
 */
function WorkflowDispatchLine({
  workflow,
  taskById,
}: {
  workflow: Workflow
  taskById: ReadonlyMap<string, Task> | null
}) {
  // Rolled up from the steps' TASKS, exactly as `codec.workflow_dispatch` does
  // server-side -- the frozen `Workflow` has no metadata field, so there is
  // nowhere else it could live. Null when the task join produced nothing to
  // read, which the banner above is already explaining.
  const tasks = workflow.steps
    .map((s) => (s.task_id ? (taskById?.get(s.task_id) ?? null) : null))
    .filter((t): t is Task => t !== null)
  const dispatch = workflowDispatchOf(tasks)
  return (
    <WorkflowDispatch
      dispatch={dispatch?.dispatch ?? null}
      integratorTaskId={dispatch?.integratorTaskId ?? null}
      steps={workflow.steps.length}
      joined={tasks.length > 0}
    />
  )
}

/**
 * Three absences, and they are not the same:
 *  - no task joined at all: the task read failed or has not reached any step,
 *    and the state banner above is already saying so. Nothing is claimed.
 *  - tasks joined but none carried a dispatch: an API older than the field.
 *  - a dispatch was read: shown.
 */
function WorkflowDispatch({
  dispatch,
  integratorTaskId,
  steps,
  joined,
}: {
  dispatch: TaskDispatch | null
  integratorTaskId: string | null
  steps: number
  joined: boolean
}) {
  // BOTH ABSENCES KEEP THEIR SLOT AND THEIR KEY (design-system.md §6.13). The
  // two 29- and 15-word paragraphs this replaces are one `.ctl-mark` each --
  // `not reported` and `no task joined` still tell the two apart, because they
  // are different words for different facts -- plus the `?` over
  // `dispatch-absent-is-old-api`, which is where that argument was already
  // written out in full before this screen restated it.
  if (dispatch === null) {
    return (
      <div className="wf-dispatch">
        <ul className="ctl-facts">
          <li className="ctl-fact is-absent">
            <b>opens</b>
            <span
              className="ctl-mark is-absent"
              aria-label={
                joined
                  ? 'None of this workflow’s tasks reported a dispatch, so what it publishes is not shown. That is an API older than the field, not a workflow that publishes nothing.'
                  : 'No task was joined for this workflow, so what it publishes cannot be read here.'
              }
            >
              {joined ? 'not reported' : 'no task joined'}
            </span>
          </li>
        </ul>
        <HelpCard topic="dispatch-absent-is-old-api" />
      </div>
    )
  }
  const c = consequenceOf(dispatch.strategy, steps)
  // The `is-none` modifier is on THIS screen's wrapper, not on `.ctl-facts`.
  // A screen may modify a primitive it shares; it may not teach the shared
  // primitive a meaning only this screen has.
  return (
    <div className={`wf-dispatch${c.pushes ? '' : ' is-none'}`}>
      <ul className="ctl-facts">
        <li className="ctl-fact">
          <b>dispatch</b>
          <code>{dispatch.strategy}</code>
        </li>
        {/* `c.headline` is one short statement of what comes OUT of the run --
            "Up to 3 pull requests — one per step." It is the answer to the
            only question this panel exists for, so it is a fact in a fact
            slot rather than a paragraph under one. */}
        <li className="ctl-fact">
          <b>opens</b>
          {c.headline}
        </li>
        {dispatch.strategy === 'integrate' && integratorTaskId !== null && (
          <li className="ctl-fact">
            <b>via</b>
            <code>{integratorTaskId.slice(-8)}</code>
          </li>
        )}
      </ul>
      <HelpCard topic="dispatch-strategies" />
    </div>
  )
}

/**
 * A clock that ticks, so "running 4m 12s" is true a second later.
 *
 * SCOPED TO THE OPEN CANVAS, following the same rule Overview's `useNow`
 * records: a 1Hz clock held at the top of the board would re-render every
 * collapsed row once a second to move one number inside one expanded card. This
 * hook only exists while a canvas is mounted, which is only while a workflow is
 * open.
 */
function useNow(): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])
  return now
}

/**
 * THE CANVAS. Real edges between real node cards, flowing left to right.
 *
 * The positions come from `layoutOf`, which is pure and tested, so the edges
 * and the cards cannot disagree: both read the same numbers. The SVG holds only
 * the edges -- the cards are HTML on top of it, because a node carries an
 * anchor, a `title` and a stop button, and those are not things to re-implement
 * inside an `<svg>`.
 *
 * It scrolls horizontally rather than shrinking. A twenty-step workflow across
 * six levels is genuinely wider than a phone, and scaling it down to fit turns
 * the step names into texture.
 */
function WorkflowGraph({
  workflow,
  taskById,
  usage,
  reload,
}: {
  workflow: Workflow
  taskById: ReadonlyMap<string, Task> | null
  usage: UsageRead
  reload: () => void
}) {
  const now = useNow()
  const layout = layoutOf(workflow.steps)
  const levels = levelsOf(workflow.steps)

  if (layout.nodes.length === 0) {
    return <span className="ctl-mark">no steps</span>
  }

  return (
    <div className="wf-canvas-wrap">
      {/* The column captions, positioned from the SAME constants the canvas
          lays out with rather than from a number repeated in the stylesheet.
          A caption that drifts one column off the nodes it names is worse
          than no caption.

          TWO WORDS, NOT FOUR. `then 5 in parallel` spelled out what the column
          under it is already a picture of: five boxes stacked in one column
          with five edges into them. `×5` is the count, which is the part the
          picture does not state exactly, and it is now in the eyebrow
          treatment every other section label in this console uses -- so it
          reads as chrome and can be skipped (design-system.md §6.13). */}
      <ol className="wf-legend" aria-label="Dependency levels" style={{ width: layout.width }}>
        {levels.map((level, i) => (
          <li
            key={i}
            className="ctl-eyebrow"
            style={{ left: PAD + i * (NODE_W + COL_GAP), width: NODE_W }}
          >
            {i === 0 ? 'starts' : 'then'}
            {level.length > 1 ? ` ×${level.length}` : ''}
          </li>
        ))}
      </ol>
      <div className="wf-canvas" style={{ width: layout.width, height: layout.height }}>
        <svg
          className="wf-edges"
          width={layout.width}
          height={layout.height}
          viewBox={`0 0 ${layout.width} ${layout.height}`}
          aria-hidden
          focusable="false"
        >
          <defs>
            <marker
              id={`arrow-${workflow.workflow_id}`}
              viewBox="0 0 8 8"
              refX="7"
              refY="4"
              markerWidth="7"
              markerHeight="7"
              orient="auto-start-reverse"
            >
              <path className="wf-arrowhead" d="M 0 1 L 7 4 L 0 7 z" />
            </marker>
          </defs>
          {layout.edges.map((e) => (
            <path
              key={`${e.from}->${e.to}`}
              className="wf-edge"
              d={edgePath(e)}
              markerEnd={`url(#arrow-${workflow.workflow_id})`}
            />
          ))}
        </svg>
        {layout.nodes.map((n) => (
          <StepNode
            key={n.step.step_id}
            step={n.step}
            state={stepState(n.step, taskById)}
            workflow={workflow}
            now={now}
            usage={usage}
            x={n.x}
            y={n.y}
            h={n.h}
            reload={reload}
          />
        ))}
      </div>
    </div>
  )
}

/** How each of the three step-state kinds presents. Kept together so the
 *  difference between "not started" and "not read" stays deliberate.
 *
 *  `derived` is what `dotClass` reads to pick between the hollow ring and the
 *  ring-with-a-bar. At STEP level it is always true: a step whose task was not
 *  in the read is an absence of information, not a state nobody computed --
 *  the distinction that needs the second mark exists one level up, on the
 *  workflow header, where an API without `rollup.py` derives nothing at all. */
function present(state: StepState): {
  tone: Tone | 'unknown'
  glyph: string
  word: string
  title: string
  derived: boolean
} {
  switch (state.kind) {
    case 'unstarted':
      return {
        tone: 'wait',
        glyph: '◌',
        word: 'not started',
        title: 'This step has no task yet. The workflow has not reached it.',
        derived: true,
      }
    case 'unknown':
      return {
        tone: 'unknown',
        glyph: '?',
        word: 'state unread',
        title: `Task ${state.taskId} exists but was not in the task read. Its state is unknown -- this does not mean it is idle.`,
        derived: true,
      }
    case 'state':
      return {
        tone: stateTone(state.state),
        glyph: stateGlyph(state.state),
        word: state.state.toLowerCase(),
        title: `Task ${state.task.id}, attempt ${state.task.attempt_count} of ${state.task.max_attempts}`,
        derived: true,
      }
  }
}






function StepNode({
  step,
  state,
  workflow,
  now,
  usage,
  x,
  y,
  h,
  reload,
}: {
  step: WorkflowStep
  state: StepState
  workflow: Workflow
  now: number
  usage: UsageRead
  x: number
  y: number
  h: number
  reload: () => void
}) {
  const p = present(state)
  const dur = stepDuration(state, now)
  const f = figuresFor(state, usage, now)
  // A read still IN FLIGHT is not an absence. "not reported" is a claim about
  // the platform; a request that has not landed has made no claim at all, so
  // the cell draws a moving placeholder instead of a sentence.
  const pending = usage.kind === 'reading' && state.kind === 'state'
  // Read off the step's own task, so the node that opens the pull request is
  // marked in the graph rather than only named in the line above it. Silent on
  // a step with no task and on a `contributor`: every step of an `integrate`
  // workflow but one is a contributor, so a badge on each would mark nothing.
  //
  // Through `dispatchOf`, not off the field: it is the one reader that decides
  // what an absent or unrecognised block means, and a second one here is how
  // two screens start disagreeing about the same task.
  const role = state.kind === 'state' ? (dispatchOf(state.task)?.role ?? null) : null
  const taskId = state.kind === 'state' ? state.task.id : state.kind === 'unknown' ? state.taskId : null

  return (
    <div
      className={`node ${p.tone}`}
      title={p.title}
      style={{ left: x, top: y, width: NODE_W, height: h }}
    >
      <div className="node-id">
        {/* The step NAME, and where its run actually lives. An id you cannot
            reach is a label; `#agents/task/<id>` is the address every other
            screen uses for the same task. Only when a task exists: a step the
            workflow has not reached has nothing to open. */}
        {taskId ? (
          <a href={`#agents/task/${encodeURIComponent(taskId)}`}>{step.step_id}</a>
        ) : (
          step.step_id
        )}
        {role === 'integrator' && (
          <span className="tag ok" title="This step merges the other steps' branches and opens the workflow's single pull request.">
            opens the PR
          </span>
        )}
      </div>
      {/* STATE AND DURATION ON ONE LINE. They were two stacked 12px rows
           saying `running` and then `running 1m 32s`, which is the same word
           twice and two rows of height on every node in the graph. The mark
           carries the tone, the word carries the state, the figure carries the
           time. Both keep their own element, because they are two different
           facts and each is asserted separately. */}
      <div className="node-line">
        <i className={dotClass({ tone: p.tone, derived: p.derived })} aria-hidden />
        <span className="node-state">{p.word}</span>
        <StepTime dur={dur} />
      </div>
      {/* The llm used. The owner's "the way it's done today", kept verbatim. */}
      <div className="node-meta">{step.runner_profile}</div>

      {/* The run, as four figures. A placeholder while the attempt read is in
          flight -- a request that has not landed has made no claim, and
          "not reported" is a claim about the platform.

          WHERE THE `node-why` PARAGRAPH WENT. Every node whose figures were
          not measured carried a full sentence of explanation underneath them
          -- 12 to 24 words from `measure.ts`, once per node, so an eight-step
          graph on a board whose attempt read failed rendered the same
          paragraph eight times. It was also the one element on the node that
          `layoutOf` does not measure, so it overflowed the box it was drawn
          in.

          The FACT was never in that paragraph. It is in the cell: a word where
          a digit would be, on a dashed rule, in the absent tone -- and each of
          the six absences uses a DIFFERENT word, so `not sampled` and `not
          read` and `no attempt yet` were already told apart without reading a
          sentence. What the sentence added was the cause, and the cause is now
          the cell's accessible name (`NodeNum`) plus the `?` in this card's
          own foot. */}
      <dl className="node-nums">
        <NodeNum label="ran" cell={f.ran} pending={false} />
        <NodeNum label="cost" cell={f.cost} pending={pending} />
        <NodeNum label="tokens" cell={f.tokens} pending={pending} />
        <NodeNum label="ckpts" cell={f.checkpoints} pending={pending} />
      </dl>

      {/* The two things a reader wants from a node that are not "stop it":
          what this step read and wrote, and what it tried. Both are routes the
          console already resolves; a node without them is a dead end, which is
          what it was before the graph work. Only drawn when a task exists --
          a step the workflow has not reached has nothing to open.

          NO `?` HERE. It was drawn per node for one draft of this pass, and a
          graph of eight unmeasured steps then carried eight identical question
          marks -- which is the paragraph problem again at one character each.
          `absent-vs-zero` is a property of the SCREEN, not of a node, so there
          is exactly one of them, in the board's chrome. */}
      {taskId !== null && (
        <div className="node-links">
          <a href={`#agents/task/${encodeURIComponent(taskId)}`}>input &amp; output →</a>
          <a href={`#agents/task/${encodeURIComponent(taskId)}/attempts`}>attempts →</a>
        </div>
      )}


      {step.depends_on.length > 0 && (
        <div className="node-dep" title={`depends on ${step.depends_on.join(', ')}`}>
          ← {step.depends_on.join(', ')}
        </div>
      )}
      {/* B28, on the node. Only when the step's TASK was actually joined: a
          step whose state is `unknown` was not in the task read, and offering
          to stop something this screen could not read would be acting on a
          guess. `StopRun` then decides for itself whether the state is one the
          cancel route accepts, so a terminal node draws nothing at all. */}
      {state.kind === 'state' && (
        <div className="node-stop">
          <StopRun
            task={state.task}
            what={`step ${step.step_id}`}
            workflow={workflow}
            step={step}
            reload={reload}
            variant="inline"
          />
        </div>
      )}
    </div>
  )
}

/**
 * HOW LONG THIS STEP HAS TAKEN, AND WHAT KIND OF TIME THAT IS.
 *
 * Five different things are rendered five different ways, and the first one is
 * the rule the whole product rests on: a step that has not started HAS NO
 * DURATION. It reads "not started", never `0s`, because `0s` is a measurement
 * and nothing measured it. `stepDuration`'s absent arm carries no number at
 * all, so this component could not print one if it tried.
 *
 * The other four are distinguished because they answer different questions:
 * time queued and time parked are time WAITED and are drawn as waiting; a
 * running step's figure is elapsed and still moving; only a finished step has a
 * duration in the ordinary sense. The measured six-step run this was designed
 * against had five steps waiting exactly 153s and then running 68-92s -- one
 * number covering both would have hidden the entire story of that workflow.
 */
function StepTime({ dur }: { dur: StepDuration }) {
  return (
    <div className={`node-dur is-${dur.kind}`} title={dur.note}>
      {dur.text}
    </div>
  )
}

/**
 * How the attempt read went, as the node needs to know it.
 *
 * `reading` is NOT an absence and must not render as one: "not reported" is a
 * claim about the platform, and a request still in flight has made no claim at
 * all. The node draws a moving placeholder for it instead -- the same
 * distinction `Overview.tsx` draws between `is-absent` and `ov-reading`.
 */
type UsageRead =
  | { kind: 'reading' }
  | { kind: 'ready'; usage: WorkflowUsage | null }
  | { kind: 'failed'; detail: string }


/** Every figure one node shows, and what to say where there is none. */
interface StepFigures {
  ran: Cell
  cost: Cell
  tokens: Cell
  checkpoints: Cell
  /**
   * The single sentence explaining the usage absences, or null if measured.
   *
   * NO LONGER RENDERED AS A PARAGRAPH. It is the flag the node reads to decide
   * whether to draw its `?` at all -- null means every figure above was
   * measured, so there is nothing to explain and no question mark appears.
   * The sentence itself reaches the reader through `Cell.note` on each figure
   * and through the help topic the `?` opens.
   */
  why: string | null
}

/**
 * THE NODE IS THE WAY IN.
 *
 * This was a plain `<div>` with no href and no onClick, so from "draft is
 * parked" there was no click that reached `draft`. It is now an `<a>` to
 * `#agents/task/<id>` -- the route App.tsx already resolves to the full agent
 * run: runtime environment, attempts, spend, duration, logs, checkpoints,
 * artifacts and outputs. An anchor rather than a click handler on purpose: it
 * is middle-clickable, copyable, and reachable by keyboard without this file
 * reimplementing any of that.
 *
 * A step with NO TASK is not a link, and says why. A dead link to a task that
 * does not exist would be the same defect one level down.
 *
 * A step whose task id we hold but whose task the read did not return IS a
 * link: the task exists, the run page fetches it by id, and the fact that this
 * board's page of 200 tasks did not include it says nothing about whether the
 * run can be opened.
 */
/**
 * HOW LONG THE STEP HAS BEEN RUNNING, or the reason that is not a number.
 *
 * `started_at` is written on DISPATCHED -> STARTING, so a QUEUED, LEASED or
 * DISPATCHED step legitimately has none and "0s" for it would be a lie in the
 * most literal sense. `completed_at` can also be missing on a task that went
 * terminal between polls, which is a THIRD case: it ran, it ended, and the
 * finish was never written -- "not recorded", exactly as the attempts drawer
 * says for peak memory.
 */
function ranCell(task: Task, now: number): Cell {
  const ms = (v: string | null) => (v ? new Date(v).getTime() : NaN)
  const started = ms(task.started_at)
  const completed = ms(task.completed_at)
  const terminal = TERMINAL_STATES.has(task.state)

  if (!Number.isFinite(started)) {
    const created = ms(task.created_at)
    return absentCell({
      text: 'not started',
      note: Number.isFinite(created)
        ? `started_at is written on DISPATCHED → STARTING. This step has not begun; it was submitted ${durationText(now - created)} ago.`
        : 'started_at is written on DISPATCHED → STARTING. This step has not begun.',
    })
  }
  if (Number.isFinite(completed)) {
    return measuredCell(durationText(completed - started), 'Start to finish, including any provider wait and any park.')
  }
  if (terminal) {
    return absentCell(FINISH_NOT_RECORDED)
  }
  return measuredCell(`${durationText(now - started)} so far`, 'Still running. This figure moves.')
}

/**
 * The four figures, for one step.
 *
 * FOUR DIFFERENT ABSENCES, and the whole value of this function is keeping them
 * apart. A step with no task has nothing to measure; a step whose task was not
 * in the task read has figures nobody fetched; a step outside the attempt
 * sample has figures this board chose not to fetch; a step whose attempt read
 * failed has figures that could not be fetched. One "—" for all four sends an
 * operator to four different places at random.
 */
function figuresFor(state: StepState, usage: UsageRead, now: number): StepFigures {
  const allAbsent = (a: Absence): StepFigures => ({
    ran: absentCell(a),
    cost: absentCell(a),
    tokens: absentCell(a),
    checkpoints: absentCell(a),
    why: a.note,
  })

  if (state.kind === 'unstarted') return allAbsent(NEVER_RAN)
  if (state.kind === 'unknown') return allAbsent(STATE_UNREAD)

  const ran = ranCell(state.task, now)
  const taskId = state.task.id

  // The read has not landed. NOT an absence: the node draws a placeholder for
  // these rather than claiming the platform reported nothing.
  if (usage.kind === 'reading') {
    const pending: Absence = {
      text: 'reading',
      note: 'The attempt read for this step is still in flight.',
    }
    return { ran, cost: absentCell(pending), tokens: absentCell(pending), checkpoints: absentCell(pending), why: null }
  }

  const absentUsage = (a: Absence): StepFigures => ({
    ran,
    cost: absentCell(a),
    tokens: absentCell(a),
    checkpoints: absentCell(a),
    why: a.note,
  })

  if (usage.kind === 'failed') {
    return absentUsage({ text: USAGE_NOT_READ.text, note: `${USAGE_NOT_READ.note} (${usage.detail})` })
  }
  if (usage.usage === null) return absentUsage(USAGE_NOT_SAMPLED)

  const failed = usage.usage.failed.get(taskId)
  if (failed !== undefined) {
    return absentUsage({ text: USAGE_NOT_READ.text, note: `${USAGE_NOT_READ.note} (${failed})` })
  }
  const u = usage.usage.byTaskId.get(taskId)
  if (u === undefined) return absentUsage(USAGE_NOT_SAMPLED)
  // The read succeeded and there is nothing to sum. A FOURTH thing, and not
  // "the runner reported no cost": nothing has run.
  if (u.attempts === 0) return absentUsage(NO_ATTEMPT_YET)

  return {
    ran,
    cost: costCell(
      u.costUsd,
      `Summed over ${u.attemptsWithCost} of ${u.attempts} attempt${u.attempts === 1 ? '' : 's'} that reported one. Token cost only — no infrastructure cost is recorded anywhere.`,
    ),
    tokens: tokensOf(u),
    // COUNTED, not reported: the attempt document carries its own list of
    // checkpoint ids, so this figure is never absent once the attempts are in
    // hand, and a zero here IS the measurement. It renders as a digit while its
    // neighbours render as sentences, because its neighbours were not measured.
    checkpoints: countCell(
      u.checkpoints,
      USAGE_NOT_SAMPLED,
      u.checkpoints === 0
        ? 'No attempt document lists one. This zero was counted, not assumed.'
        : `Across ${u.attempts} attempt${u.attempts === 1 ? '' : 's'}. Contents are not recorded.`,
    ),
    why: null,
  }
}

/**
 * Input and output tokens as one cell.
 *
 * EACH HALF SUMS SEPARATELY, so a step whose runner reported input and no
 * output shows the half it has and says which -- `(input ?? 0) + (output ?? 0)`
 * counts the missing half as a zero, which is the same defect
 * `AgentDetail.tsx:408` records having already been fixed once at the tile
 * level.
 */
function tokensOf(u: StepUsage): Cell {
  const tin = tokenCell(u.inputTokens, '')
  const tout = tokenCell(u.outputTokens, '')
  if (tin.kind === 'absent' && tout.kind === 'absent') return absentCell(TOKENS_NOT_REPORTED)
  const parts: string[] = []
  if (tin.kind === 'measured') parts.push(`${tin.text} in`)
  if (tout.kind === 'measured') parts.push(`${tout.text} out`)
  const both = tin.kind === 'measured' && tout.kind === 'measured'
  return measuredCell(
    parts.join(' · '),
    both
      ? `Summed over ${u.attemptsWithTokens} of ${u.attempts} attempt${u.attempts === 1 ? '' : 's'} that reported tokens.`
      : tin.kind === 'measured'
        ? 'Input only. No attempt reported an output count, which is not the same as none.'
        : 'Output only. No attempt reported an input count, which is not the same as none.',
  )
}

/**
 * One figure on a node.
 *
 * `is-absent` is the dashed, faint treatment the metric tiles use, and it is
 * applied to ABSENCE only. A read still in flight gets `is-reading` and a
 * moving bar instead, because the two say different things: one is a statement
 * about the platform, the other is a statement about this request.
 *
 * THE CELL IS THE ENCODING AND THE CELL IS ENOUGH. `cell.text` is a word where
 * a digit would be -- `not sampled`, `not read`, `no attempt yet` -- on a
 * dashed rule in `--ctl-absent`, and `measure.ts` guarantees no two absences
 * that can land in this slot share a word. A reader who has not hovered
 * anything can see that there is no number here and which kind of nothing it
 * is. `cell.note` is the CAUSE, which is a different question, and it is on
 * the `title` and behind the node's `?`.
 */
function NodeNum({ label, cell, pending }: { label: string; cell: Cell; pending: boolean }) {
  const cls = pending ? 'is-reading' : cell.kind === 'absent' ? 'is-absent' : ''
  return (
    <div className={`node-num ${cls}`.trimEnd()} title={cell.note || undefined}>
      <dt>{label}</dt>
      <dd>{pending ? <span className="node-reading" aria-label="reading" /> : cell.text}</dd>
    </div>
  )
}

/**
 * What the sample covered, said once in the board's chrome rather than implied
 * per node.
 *
 * Silent when every task on the board was read: a line saying "12 of 12" on
 * every refresh is noise, and the per-node "not sampled" already carries the
 * case that matters.
 *
 * A COVERAGE FIGURE, NOT A PARAGRAPH ABOUT COVERAGE. This was 26 words in an
 * amber block across the full width of the page; it is now `9/20 sampled` on
 * a partial mark, which is the same fact in the shape the reader was going to
 * reduce it to anyway (design-system.md §8.4, the qualifier slot). `is-partial`
 * is dashed on one side only -- the side the missing part would have been on
 * -- so a coverage that is not a total does not LOOK like a total.
 *
 * THE SENTENCE IS STILL HERE, as the mark's accessible name, and it still
 * contains the words `not zero`: that clause is the entire reason this
 * component exists and it does not get to become implicit just because it
 * stopped being visible ink.
 */
function SampleNote({ usage }: { usage: WorkflowUsage }) {
  const uncovered = usage.notSampled.size
  const failed = usage.failed.size
  if (uncovered === 0 && failed === 0) return null
  const ceiling =
    uncovered > 0
      ? ` ${uncovered} ${uncovered === 1 ? 'is' : 'are'} outside the ${usage.sampleLimit}-task read ceiling, so their cost and tokens are unknown here, not zero.`
      : ''
  const unread = failed > 0 ? ` ${failed} attempt read${failed === 1 ? '' : 's'} failed.` : ''
  return (
    <span className="wf-caveat" role="status">
      <span
        className="ctl-mark is-partial"
        aria-label={`Step figures cover ${usage.byTaskId.size} of ${usage.tasksRequested} tasks on this board.${ceiling}${unread}`}
      >
        {usage.byTaskId.size}/{usage.tasksRequested} sampled
      </span>
      <HelpCard topic="partial-read" />
    </span>
  )
}
