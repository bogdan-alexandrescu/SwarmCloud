import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'

import {
  loadWorkflowBoard,
  loadWorkflowUsage,
  type StepUsage,
  type WorkflowBoard,
  type WorkflowUsage,
} from './api'
import {
  autoTier,
  edgePath,
  layoutOf,
  levelsOf,
  profileMix,
  shapeOf,
  stageCensus,
  stepDuration,
  workflowSpend,
  CANVAS_COLUMN,
  TIER_DROPS,
  ZOOM_TIERS,
  type DagBand,
  type DagLayout,
  type DagShape,
  type StageCensus,
  type StepDuration,
  type WorkflowSpend,
  type ZoomTier,
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
 * hardest is the shape, `1 → 5 → 1`, as text. Five steps in parallel and five
 * steps in a chain have the same count, the same fraction done and the same
 * cost; they are completely different runs, and a list that renders them
 * identically is the defect the graph work exists to fix. It must survive being
 * collapsed or it has not been fixed. (This paragraph said "next to a mini-map
 * of the real edges" and there is no mini-map -- see `Shape`.)
 *
 * EXPANDED is a canvas: real edges drawn between generous node cards, FLOWING
 * TOP TO BOTTOM along dependency depth, with the steps of one level side by
 * side across it. It flowed left to right, which put the steps of a level in a
 * vertical stack -- the owner's "all nodes at the same stage displayed
 * horizontally not vertically ... top to bottom rather than left to right".
 * `dag.ts`'s layout section holds the transpose and what it costs.
 *
 * THE NODE IS THE LINK. One target, no anchors inside it: the step id and both
 * deep links resolved to the same task drawer, so the card is the anchor and
 * the stop control is its sibling. Every node keeps the three facts it has
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
  // WHICH STAGES THE READER HAS OPENED, and it is up here for a stronger
  // version of the same reason. A card can be re-opened with one click; a
  // stage you opened, scrolled sideways through and then lost because the
  // board re-read itself is the interaction the requirement for this feature
  // calls out by name -- "a stage that re-collapses under the user every 5
  // seconds is worse than no collapsing". `useNow` also re-renders every open
  // canvas once a second, so anything held inside `WorkflowGraph` would have to
  // survive that too. One store, above everything that remounts.
  //
  // Keyed by `stageKey`, so two workflows that both have a wide stage 1 do not
  // share one flag.
  const [stages, setStages] = useState<Record<string, boolean>>({})

  // HOW MUCH EACH WORKFLOW'S NODES SAY, keyed by workflow id, and up here for
  // exactly the reasons `stages` is. `auto` is not stored: an absent entry MEANS
  // auto, so a reader who never touched the control cannot be holding a stale
  // override from three polls ago, and the automatic choice is free to change
  // when a workflow's widest stage does.
  //
  // PER WORKFLOW RATHER THAN PER BOARD, because the tier is a function of the
  // workflow's own widest stage: a chain and a 13-wide fan on the same board
  // want different answers, and a board-wide control would take the figures off
  // the chain to pay for the fan.
  const [zooms, setZooms] = useState<Record<string, ZoomChoice>>({})

  // A board-wide instruction overrules the per-card ones. Keeping stale
  // overrides would make "Collapse all" leave three cards open with no way to
  // tell why.
  //
  // IT DOES NOT TOUCH `stages`. Rows/Graph is an instruction about WORKFLOWS;
  // a stage is a thing inside one, and throwing away which stages a reader had
  // opened because they toggled the board's own default would be the same
  // "it re-collapsed under me" defect arriving by a different route.
  const chooseMode = useCallback((m: BoardMode) => {
    setMode(m)
    setOpen({})
  }, [])

  const toggle = useCallback(
    (id: string, expanded: boolean) => setOpen((o) => ({ ...o, [id]: !expanded })),
    [],
  )

  const toggleStage = useCallback(
    (key: string, expanded: boolean) => setStages((s) => ({ ...s, [key]: !expanded })),
    [],
  )

  const chooseZoom = useCallback(
    (id: string, choice: ZoomChoice) => setZooms((z) => ({ ...z, [id]: choice })),
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
          openStages={stages}
          toggleStage={toggleStage}
          zooms={zooms}
          chooseZoom={chooseZoom}
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
  openStages,
  toggleStage,
  zooms,
  chooseZoom,
  reload,
}: {
  board: WorkflowBoard
  mode: BoardMode
  chooseMode: (m: BoardMode) => void
  open: Record<string, boolean>
  toggle: (id: string, expanded: boolean) => void
  openStages: Record<string, boolean>
  toggleStage: (key: string, expanded: boolean) => void
  zooms: Record<string, ZoomChoice>
  chooseZoom: (id: string, choice: ZoomChoice) => void
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
          {/* ONE `?` FOR THE WHOLE BOARD, AND SINCE B7.4 FOR THE WHOLE SCREEN.
              `absent-vs-zero` is the rule every absent figure here obeys -- a
              word where a digit would be, on a dashed rule -- and it is a
              property of the screen rather than of any one node. Drawn per node
              it would have been eight question marks on an eight-step graph.
              The screen's other four went the way the density pass sends them:
              the two marks below (`states unread`, `n/m sampled`) each carry a
              full sentence as their accessible name, longer and more specific
              than the topic a glyph would have opened, and the two dispatch
              topics are on the agent detail's card foot and in the rail's Help
              section. */}
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
            openStages={openStages}
            onToggleStage={toggleStage}
            zoom={zooms[w.workflow_id] ?? 'auto'}
            onZoom={chooseZoom}
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
 * a two-word mark in the board's own chrome, with the sentence as its
 * accessible name.
 *
 * B7.4 TOOK THE `?` THAT SAT BESIDE IT. The accessible name above is longer and
 * more specific than `#help/read-failed` -- it carries the server's own detail
 * and it states, for this board, which of the things on screen are still true
 * -- so the glyph opened a shorter, general version of the sentence it was
 * standing next to. The board keeps one glyph, on `absent-vs-zero`, which is
 * the rule every absent figure here obeys and which no mark can state.
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
  openStages,
  onToggleStage,
  zoom = 'auto',
  onZoom,
  reload,
}: {
  workflow: Workflow
  taskById: ReadonlyMap<string, Task> | null
  expanded: boolean
  usage: UsageRead
  onToggle: (id: string, expanded: boolean) => void
  /**
   * Which stages are open, keyed by `stageKey`. PASSED IN, never owned here:
   * the store lives above the `key` that a stop-and-reload bumps, so the card
   * remounting does not close a stage the reader opened.
   */
  openStages: Record<string, boolean>
  onToggleStage: (key: string, expanded: boolean) => void
  /**
   * How much this workflow's nodes say, or `auto` to fit the column.
   *
   * OPTIONAL, AND THE DEFAULT IS `auto`. The card is exported and rendered from
   * more than one place; a required prop here would have made every caller
   * state a preference it does not have, and the honest default for "I have no
   * opinion" is the one that measures the graph.
   */
  zoom?: ZoomChoice
  onZoom?: (id: string, choice: ZoomChoice) => void
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
              Firestore, the API, the logs and the `#work/task/<id>` address.
              Printed as WF_BCDC9180… it cannot be pasted anywhere, which is
              the only thing an id is for. */}
          {/* THE TITLE IS THE WHOLE ID, and that is inventory F11's last
              unpaid line on this row. `.wf-bar .id` ellipses at 390px, where
              `[name]` is the column every other field takes its pixels from --
              `wf_5e5ad3b6f7da4299a839` loses its tail. An ellipsed id cannot be
              pasted anywhere, which is the only thing an id is for (B17), so
              the complete value has to survive somewhere on the row itself
              rather than only in the open card. */}
          <Id title={workflow.workflow_id}>{workflow.workflow_id}</Id>
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
          <WorkflowGraph
            workflow={workflow}
            taskById={taskById}
            usage={usage}
            openStages={openStages}
            onToggleStage={onToggleStage}
            zoom={zoom}
            onZoom={onZoom}
            reload={reload}
          />
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
 * THE TOPOLOGY, COLLAPSED -- as text, and ONLY as text.
 *
 * This docstring used to describe two renderings, "the widths as text
 * (`1 → 5 → 1`) and a mini-map drawn from the SAME `layoutOf` the expanded
 * canvas uses". The mini-map was removed and the body's own comment records
 * why; the docstring above it was not, so the file has been promising a
 * drawing it does not make. Corrected here rather than left for the next lane
 * to implement back.
 *
 * The text is what a screen reader and a test can read, and it is not
 * decoration: without it a fan-out and a chain are the same row.
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
        {/* NO `?` (B7.4). The mark's accessible name above already draws the
            distinction the topic exists for -- an API older than the field,
            not a workflow that publishes nothing -- and it draws it per case,
            which a shared topic cannot. */}
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
      {/* NO `?` (B7.4). `consequenceOf` prints what this strategy publishes, in
          numbers, on the facts above -- one pull request over six steps, or
          six -- which is the topic's claim computed for THIS workflow rather
          than described in general. */}
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
 * One stage's address in the board-wide expansion store.
 *
 * THE LEVEL GOES FIRST AND THAT IS THE WHOLE TRICK. `${id}-${level}` collides:
 * `wf_a` level 12 and `wf_a1` level 2 are both `wf_a1-2`, and the store would
 * open a stage in the wrong workflow. With the level leading, the separator is
 * the first non-digit character, so the split is unambiguous whatever the id
 * turns out to contain -- and it stays printable, which a NUL separator does
 * not: one of those in a `.tsx` makes git call the whole file binary and stop
 * showing anyone its diff. (It did.)
 */
export function stageKey(workflowId: string, level: number): string {
  return `${level}|${workflowId}`
}

/** The DOM id of the element a band's `aria-controls` points at. Not
 *  `stageKey`: an id may not contain a NUL, and it has to be a valid selector
 *  for the assistive technology that resolves it. */
function stageDomId(workflowId: string, level: number): string {
  return `wf-stage-${workflowId}-${level}`
}

/**
 * What the reader asked for, or `auto` for whatever fits the column.
 *
 * `auto` IS A REAL CHOICE AND NOT AN ABSENCE. It is the value the control shows
 * as pressed when nobody has overridden anything, and it means "re-decide this
 * every render against the workflow's own widest stage" -- which is a different
 * thing from any of the three fixed tiers, and has to be selectable again once
 * a reader has left it.
 */
type ZoomChoice = 'auto' | ZoomTier

/**
 * THE ZOOM CONTROL, AND THE REASON SEMANTIC ZOOM IS ALLOWED TO DROP A FIELD.
 *
 * "If a field is dropped, its absence must be legibly a ZOOM decision and not a
 * data one" is the rule this control exists to satisfy, and it satisfies it in
 * the strongest available form: the reader can put the field back. A canvas
 * that silently stopped drawing runner profiles would be indistinguishable from
 * a canvas whose steps have no runner profile, and this console's whole subject
 * is keeping "not shown" apart from "not there".
 *
 * `.ctl-seg`, THE SAME PRIMITIVE AS THE BOARD'S Rows/Graph CONTROL, one level
 * down -- the same gesture, the same shape, the same keyboard behaviour, and
 * §6.11's one segmented control rather than a second kind of switch invented
 * for this screen. Four real buttons, so it is tab-reachable and operable from
 * the keyboard without the minimap being involved at all.
 *
 * THE LABELS NAME WHAT YOU GET, NOT HOW BIG IT IS. `Names`/`Details`/`Figures`
 * are the fields; `Small`/`Medium`/`Large` would be the pixels, and the pixels
 * are an output here -- `nodeWidthAt` measures the workflow's own step names, so
 * two tiers can come out the same width on a workflow whose names were what set
 * the width. A control whose labels claimed three sizes and delivered two would
 * be lying about the mechanism.
 */
function ZoomControl({
  workflowId,
  choice,
  auto,
  onChoose,
}: {
  workflowId: string
  choice: ZoomChoice
  auto: ZoomTier
  onChoose: (id: string, choice: ZoomChoice) => void
}) {
  // BUILT FROM `ZOOM_TIERS`, NOT LISTED AGAIN HERE. A fourth tier added to
  // `dag.ts` gets a button for free, and -- more to the point -- a tier REMOVED
  // there cannot leave a dead segment behind that still sets a `zoom` no
  // `layoutOf` understands.
  const options: ReadonlyArray<readonly [ZoomChoice, string, string]> = [
    [
      'auto',
      'Auto',
      `Draw as much as the column holds: the widest tier whose nodes let this workflow's widest stage be drawn in full. Right now that is ${TIER_LABEL[auto]}.`,
    ],
    ...ZOOM_TIERS.map((t) => [t, TIER_LABEL[t], TIER_TITLE[t]] as const),
  ]
  return (
    <div className="ctl-seg wf-zoom-seg" role="group" aria-label="How much each step says">
      {options.map(([value, label, title]) => (
        <button
          key={value}
          type="button"
          aria-pressed={choice === value}
          title={title}
          onClick={() => onChoose(workflowId, value)}
        >
          {label}
        </button>
      ))}
    </div>
  )
}

/** The tier names as the control spells them -- on its own buttons, in the
 *  `auto` option's sentence and in the notice below. One place, so the three
 *  cannot come apart. */
const TIER_LABEL: Readonly<Record<ZoomTier, string>> = {
  figures: 'Figures',
  details: 'Details',
  names: 'Names',
}

/**
 * What each segment does, as its `title`.
 *
 * THE FIELDS ARE SPELLED OUT RATHER THAN COUNTED. "3 fields" is a number a
 * reader has to go and check; "the runner profile, the duration and the four
 * run figures are not drawn" is the thing they were about to check.
 */
const TIER_TITLE: Readonly<Record<ZoomTier, string>> = {
  figures:
    'Every field: the step name, its state, the runner profile, how long it has taken and the four run figures.',
  details:
    'The step name, its state, the runner profile and how long it has taken. The four run figures are not drawn.',
  names:
    'The step name and its state. The runner profile, the duration and the four run figures are not drawn.',
}

/**
 * WHAT THIS ZOOM IS NOT DRAWING, SAID ONCE FOR THE WHOLE CANVAS.
 *
 * THE FAILURE IT PREVENTS is the one the requirement for semantic zoom names:
 * "a zoom level that renders an unmeasured field as blank has turned 'nobody
 * measured this' into 'this is fine'". Nothing here renders blank -- a dropped
 * field leaves the card entirely rather than leaving an empty slot -- but a
 * node with no figures on it still has to be distinguishable from a node whose
 * figures nobody measured, and the distinction cannot live on the node: the
 * node is what has stopped saying things.
 *
 * SO IT LIVES ON THE CANVAS, ONCE, in the board's own chrome vocabulary. It is
 * the same argument the single `?` on this screen makes and the same argument
 * `SampleNote` makes: the fact is a property of the READ (or here, of the
 * zoom), not of any one row, and drawn per node it would be one mark per step.
 *
 * `is-partial`, not `is-absent`. `.ctl-mark.is-absent` means the platform
 * reported nothing; this is a coverage statement -- part of the record is shown
 * and part is not -- which is exactly what the partial mark's one-sided dash
 * already encodes on `SampleNote`.
 *
 * SILENT AT THE FULL TIER, because `TIER_DROPS.figures` is empty. A mark saying
 * "nothing is hidden" on every canvas that fits would be noise of the kind
 * §8.4 removed from this screen twice already.
 */
function ZoomNotice({ tier, choice }: { tier: ZoomTier; choice: ZoomChoice }) {
  const dropped = TIER_DROPS[tier]
  if (dropped.length === 0) return null
  // `noUncheckedIndexedAccess` is on, so an index into a `readonly string[]` is
  // `string | undefined` -- written out rather than asserted away, because the
  // assertion would be the thing that broke if a tier ever dropped nothing and
  // still reached here.
  const head = dropped.slice(0, -1).join(', ')
  const tail = dropped[dropped.length - 1] ?? ''
  const list = head === '' ? tail : `${head} and ${tail}`
  return (
    <span className="wf-caveat" role="status">
      <span
        className="ctl-mark is-partial"
        aria-label={
          `This canvas is drawn at the ${TIER_LABEL[tier]} zoom` +
          (choice === 'auto'
            ? ', chosen automatically so that the widest stage fits the column'
            : ', which you chose') +
          `, so no step is drawing its ${list}. That is a decision about the zoom and not a fact about the steps: every one of those fields still exists and is still measured or still absent exactly as it was. Choose Figures above, or open a step, to see them.`
        }
      >
        {list} not drawn
      </span>
    </span>
  )
}

/**
 * WHERE YOU ARE ON A CANVAS THAT DOES NOT FIT.
 *
 * WHY IT EXISTS. Scrolling used to be the whole answer to a wide canvas, and
 * scrolling is what tells you where you are: you got there, so you know. A
 * canvas that fits by DROPPING FIELDS has no such trail -- and one that still
 * does not fit at the smallest tier is scrolled through with no idea how much
 * is off to the right. The minimap is the position channel the zoom took away.
 *
 * IT IS NOT A NAVIGATION CONTROL AND MUST NOT BE THE ONLY WAY TO MOVE. It is
 * `role="img"` with the whole position stated in its accessible name, plus a
 * pointer shortcut for people who have a pointer. Everything it does is
 * reachable without it: the wrapper scrolls natively (wheel, trackpad,
 * touch, the scrollbar), every node is an `<a>` in the tab order and focusing
 * one scrolls it into view, and every band is a `<button>`. A `role="img"` was
 * chosen over a `<button>` deliberately -- a button whose only meaningful
 * activation is "the x coordinate you clicked at" is a button a keyboard cannot
 * use, and announcing one would promise a route that is not there.
 *
 * `preserveAspectRatio="none"`, WHICH IS NORMALLY WRONG AND IS RIGHT HERE. The
 * two axes of this canvas answer different questions: X is the scroll this
 * wrapper owns and the thing the reader has lost track of, Y is the page's own
 * scroll and is not clipped by anything. Scaling them together would render a
 * 3,671 x 4,134 canvas as a 45px-tall sliver 40px wide, which states neither.
 * They are scaled independently, on purpose, and the strip is a map of the
 * HORIZONTAL extent with the levels kept in order down it.
 *
 * ITS HEIGHT IS `BAND_H`, DERIVED AND NOT PICKED. 45px is what this sheet
 * already gives a one-line summary of a whole stage (`dag.ts` sums it from the
 * band's own rows), and a one-line summary of the whole canvas is the same kind
 * of object at the next level up. Its width is the wrapper's, so nothing about
 * it is a number somebody chose.
 *
 * A FAILURE IS VISIBLE IN HERE TOO. A step in a bad state draws in the bad
 * tone, and a COLLAPSED stage holding one draws its band in that tone -- so
 * "find what broke" works on the map as well as on the canvas, including for
 * the part of the canvas that is off screen. That is the same guarantee
 * `.wf-band.has-failure` makes, at the one remaining zoom level where the band
 * itself might be off to the right.
 */
function Minimap({
  layout,
  taskById,
  view,
  onJump,
}: {
  layout: DagLayout
  taskById: ReadonlyMap<string, Task> | null
  /** The wrapper's horizontal viewport, in canvas pixels. `w === 0` means it
   *  has not been measured -- first paint, and every test, because jsdom has no
   *  layout engine and answers 0 to `clientWidth`. */
  view: { left: number; w: number }
  onJump: (fraction: number) => void
}) {
  const measured = view.w > 0
  // THE FALLBACK IS THE MEASURED COLUMN, NOT A GUESS THAT IT OVERFLOWS. Before
  // the wrapper has been measured the only width this file knows is
  // CANVAS_COLUMN, the 1,054px the canvas gets at the viewport the audit was
  // taken at, and `dag.ts` already treats it as the reference for exactly this.
  const overflowing = measured ? layout.width > view.w + 1 : layout.width > CANVAS_COLUMN
  if (!overflowing) return null

  const w = Math.round(layout.width)
  const label = measured
    ? `Map of the canvas. It is ${w} pixels wide; ${Math.round(view.w)} of them are on screen, starting ${Math.round(view.left)} pixels from the left. The canvas scrolls, and tabbing to a step brings it into view.`
    : `Map of the canvas. It is ${w} pixels wide, wider than the ${CANVAS_COLUMN} pixels the column holds at 1440. How much of it is on screen has not been measured. The canvas scrolls, and tabbing to a step brings it into view.`

  return (
    <div
      className="wf-minimap"
      role="img"
      aria-label={label}
      onPointerDown={(e) => {
        const box = e.currentTarget.getBoundingClientRect()
        // jsdom, and any layout that has not happened yet, answer 0. Dividing
        // by it would send `scrollLeft` to NaN, which silently pins the canvas
        // at 0 rather than throwing.
        if (box.width <= 0) return
        onJump((e.clientX - box.left) / box.width)
      }}
    >
      <svg
        viewBox={`0 0 ${layout.width} ${layout.height}`}
        preserveAspectRatio="none"
        aria-hidden
        focusable="false"
      >
        {layout.bands.map((b) => {
          const census = stageCensus(b.steps, taskById)
          const broken = census.failed + census.cancelled > 0
          return (
            <rect
              key={`band-${b.level}`}
              className={`wf-mini-band${broken ? ' is-bad' : ''}`}
              x={b.x}
              y={b.y}
              width={b.w}
              height={b.h}
            />
          )
        })}
        {layout.nodes.map((n) => (
          <rect
            key={n.step.step_id}
            className={`wf-mini-node is-${present(stepState(n.step, taskById)).tone}`}
            x={n.x}
            y={n.y}
            width={layout.nodeW}
            height={n.h}
          />
        ))}
        {measured && view.w < layout.width && (
          <rect
            className="wf-mini-view"
            x={view.left}
            y={0}
            width={view.w}
            height={layout.height}
          />
        )}
      </svg>
    </div>
  )
}

/**
 * THE CANVAS. Real edges between real node cards, FLOWING TOP TO BOTTOM.
 *
 * The positions come from `layoutOf`, which is pure and tested, so the edges
 * and the cards cannot disagree: both read the same numbers. The SVG holds only
 * the edges -- the cards are HTML on top of it, because a node carries an
 * anchor, a `title` and a stop button, and those are not things to re-implement
 * inside an `<svg>`.
 *
 * IT ZOOMS BEFORE IT COLLAPSES, AND IT COLLAPSES BEFORE IT CLIPS. Three
 * mechanisms, in that order, and the order is the whole design:
 *
 *  1. SEMANTIC ZOOM. `autoTier` takes the widest tier whose nodes let this
 *     workflow's widest stage be drawn in full. A five-step fan is 1,407px of
 *     full nodes and 852px of `details` ones, so it is DRAWN rather than
 *     summarised -- and what it costs is the four run figures, which are one
 *     click away on the step itself. Nothing is scaled: `typescale.test.ts`
 *     holds a floor under the type and a node scaled to fit is texture.
 *  2. STAGE COLLAPSING. Thirteen nodes are 2,150px even at the smallest tier,
 *     and no tier fixes that, so a stage that still does not fit is drawn as
 *     one band that says what is in it by state (`StageBand`) and expands on
 *     click. `autoTier` stays at the full tier when that happens rather than
 *     stripping every node in the workflow to pay for a stage that is going to
 *     be a band anyway.
 *  3. SCROLLING, for the stage a reader has asked to see in full.
 *
 * WHAT DID NOT CHANGE, and must not: the flow still runs top to bottom with the
 * steps of one stage side by side across it. That is the owner's explicit
 * instruction, and both zoom and collapsing are orthogonal to it -- a stage
 * drawn at any tier draws exactly the horizontal row it drew before.
 */
function WorkflowGraph({
  workflow,
  taskById,
  usage,
  openStages,
  onToggleStage,
  zoom,
  onZoom,
  reload,
}: {
  workflow: Workflow
  taskById: ReadonlyMap<string, Task> | null
  usage: UsageRead
  openStages: Record<string, boolean>
  onToggleStage: (key: string, expanded: boolean) => void
  zoom: ZoomChoice
  onZoom?: (id: string, choice: ZoomChoice) => void
  reload: () => void
}) {
  const now = useNow()
  const levels = levelsOf(workflow.steps)
  // Built every render rather than memoised: `useNow` re-renders this component
  // once a second anyway, so a memo over a set of at most one entry per level
  // would cost more than it saves and would be one more thing to invalidate.
  const expandedStages = new Set<number>()
  levels.forEach((_, i) => {
    if (openStages[stageKey(workflow.workflow_id, i)] === true) expandedStages.add(i)
  })
  // RESOLVED HERE AND PASSED DOWN AS ONE VALUE. `auto` is re-decided every
  // render against the steps as they are now, which is right: a workflow whose
  // fan-out has not been created yet should not be pinned to the tier its first
  // two steps needed.
  const auto = autoTier(workflow.steps)
  const tier: ZoomTier = zoom === 'auto' ? auto : zoom
  const layout = layoutOf(workflow.steps, expandedStages, tier)

  // WHERE THE VIEWPORT IS, for the minimap. Read off the wrapper rather than
  // computed, because it is the one number on this screen that genuinely is a
  // browser measurement -- `CANVAS_COLUMN` is what the canvas gets at ONE
  // viewport, and a reader on a 1920 monitor is looking at more of the canvas
  // than any constant here knows about.
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const [view, setView] = useState<{ left: number; w: number }>({ left: 0, w: 0 })
  const syncView = useCallback(() => {
    const el = wrapRef.current
    if (el === null) return
    // Compared before it is set: this runs on every scroll event and on every
    // layout change, and a fresh object each time would re-render the card once
    // per scroll tick for no change at all.
    setView((v) =>
      v.left === el.scrollLeft && v.w === el.clientWidth
        ? v
        : { left: el.scrollLeft, w: el.clientWidth },
    )
  }, [])
  useEffect(() => {
    syncView()
  }, [syncView, layout.width, layout.height])
  const jump = useCallback(
    (fraction: number) => {
      const el = wrapRef.current
      if (el === null || el.clientWidth <= 0) return
      // Centred on the point, not left-aligned to it: a map you click the
      // middle of should put the middle of the canvas in front of you.
      el.scrollLeft = Math.max(0, fraction * el.scrollWidth - el.clientWidth / 2)
      syncView()
    },
    [syncView],
  )

  // `levels`, NOT `layout.nodes`. A workflow whose every stage is collapsed has
  // no nodes and is emphatically not empty -- testing the node count would have
  // put "no steps" over a 30-step run the moment this feature shipped.
  if (levels.length === 0) {
    return <span className="ctl-mark">no steps</span>
  }

  // The top of each level's band, taken from the layout that placed them. It
  // was recovered with `layout.nodes.find((n) => n.level === i)?.y`, which is
  // wrong twice over now: a collapsed level has no node to find, and an open
  // wide level's nodes sit a band and a gap below the level's own top.
  const levelTop = layout.levelTop

  // WHETHER THERE IS ANYTHING TO ZOOM, and the answer is no for the commonest
  // shape in this product. A chain is one node wide at any depth, so no tier
  // changes anything about it, and a segmented control that cannot alter what
  // is under it is the noise §6.11 took off this screen twice. Four conditions,
  // each one a case where the strip has something to say:
  //
  //   * the automatic choice already dropped fields, so the notice has to
  //     explain them;
  //   * a stage is a band, so the reader may want to know why and what the
  //     alternatives cost;
  //   * the canvas is wider than the column -- by the measured viewport where
  //     there is one, and by CANVAS_COLUMN before the first layout -- so the
  //     minimap has a position to state;
  //   * the reader has OVERRIDDEN the zoom. This one is what stops the control
  //     disappearing under its own consequence: a workflow that loses a step
  //     and would now fit must not take away the only way back from a choice
  //     still in force, leaving dropped fields with nothing explaining them.
  const zoomable =
    zoom !== 'auto' ||
    tier !== 'figures' ||
    layout.bands.length > 0 ||
    layout.width > (view.w > 0 ? view.w : CANVAS_COLUMN)

  return (
    <>
      {/* THE ZOOM STRIP: the control, what this zoom is not drawing, and the
          map. Three pieces of chrome and zero sentences of prose between them
          (§6.11), in the order a reader needs them -- the choice, its
          consequence, and where that leaves you.

          `onChoose` FALLS BACK TO A NO-OP RATHER THAN HIDING THE CONTROL. A
          caller with no `onZoom` still gets the buttons, because hiding them
          would leave the dropped fields unexplained on exactly the surface with
          no way to ask for them back. There is no such caller today; this is
          what stops one appearing silently. */}
      {zoomable && (
        <div className="wf-zoom">
          <ZoomControl
            workflowId={workflow.workflow_id}
            choice={zoom}
            auto={auto}
            onChoose={onZoom ?? (() => {})}
          />
          <ZoomNotice tier={tier} choice={zoom} />
          <Minimap layout={layout} taskById={taskById} view={view} onJump={jump} />
        </div>
      )}
      <div className="wf-canvas-wrap" ref={wrapRef} onScroll={syncView}>
        <div className="wf-graph">
          {/* THE LEVEL RAIL, WHICH WAS A ROW OF COLUMN CAPTIONS. When levels ran
            left to right these sat across the top, one over each column. Levels
            run DOWN now, so the captions run down beside them -- and they are
            STICKY at the left edge, so a fan-out of five scrolled sideways
            keeps saying which level you are looking at. That is the case this
            axis creates and the old one did not have.

            TWO WORDS, NOT FOUR. `then 5 in parallel` spelled out what the band
            beside it is already a picture of: five boxes side by side with five
            edges into them. `×5` is the count, which is the part the picture
            does not state exactly, and it is in the eyebrow treatment every
            other section label in this console uses -- so it reads as chrome
            and can be skipped (design-system.md §6.13). */}
          <ol className="wf-levels" aria-label="Dependency levels" style={{ height: layout.height }}>
            {levels.map((level, i) => (
              <li key={i} className="ctl-eyebrow" style={{ top: levelTop[i] }}>
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
                // TWO PATHS, ONE EDGE, AND THE FIRST ONE IS WHY THE NODES LOST
                // THEIR SHADOWS. Fourteen drop shadows on one screen bought one
                // thing: an edge crossing a card read as passing under it rather
                // than into it. A node is always at least one level below every
                // parent, but it can be MORE than one, so a long edge does cross
                // the band between -- the separation is real and had to go
                // somewhere. It is now on the EDGE, which is the thing doing the
                // crossing: a wider stroke in the canvas's own colour, drawn
                // first, so the line carries its own clearance. One `<g>` per
                // (parent, child) pair keyed by that pair, so the guarantee that
                // one dependency draws one mark is unchanged.
                <g key={`${e.from}->${e.to}`}>
                  <path className="wf-edge-halo" d={edgePath(e)} />
                  <path
                    className="wf-edge"
                    d={edgePath(e)}
                    markerEnd={`url(#arrow-${workflow.workflow_id})`}
                  />
                </g>
              ))}
            </svg>
            {/* THE NODES, GROUPED BY STAGE, and the grouping exists for one
              reason: a wide stage's band is a disclosure control and a
              disclosure control needs something to point `aria-controls` at.
              `.wf-stage` is a zero-size positioned box at the canvas origin, so
              the slots inside it keep the exact canvas coordinates they had as
              direct children -- it changes the accessibility tree and nothing
              about the layout. Stages that are never collapsible are not
              wrapped at all; a wrapper with no control aimed at it would be a
              box in the tree with nothing to say.

              `layout.wide[i]`, NOT `stageIsWide(level.length)`. Which stages are
              banded is a property of the width THIS tier gave the nodes, and the
              layout is the thing that knows it -- asking the full tier's
              threshold here would have wrapped a six-step stage that the
              `details` tier draws in full. */}
            {layout.levels.map((level, i) => {
              const cards = layout.nodes
                .filter((n) => n.level === i)
                .map((n) => (
                  <StepNode
                    key={n.step.step_id}
                    step={n.step}
                    state={stepState(n.step, taskById)}
                    workflow={workflow}
                    now={now}
                    usage={usage}
                    tier={layout.tier}
                    x={n.x}
                    y={n.y}
                    w={layout.nodeW}
                    h={n.h}
                    reload={reload}
                  />
                ))
              if (cards.length === 0) return null
              return layout.wide[i] === true ? (
                <div key={i} className="wf-stage" id={stageDomId(workflow.workflow_id, i)}>
                  {cards}
                </div>
              ) : (
                <Fragment key={i}>{cards}</Fragment>
              )
            })}
            {/* LAST IN THE DOM AND ABOVE THE EDGES. A band is opaque and spans
              the canvas, so an edge arriving from the stage above passes behind
              it exactly as the cards pass behind the sticky level rail. */}
            {layout.bands.map((b) => (
              <StageBand
                key={b.level}
                band={b}
                census={stageCensus(b.steps, taskById)}
                controls={stageDomId(workflow.workflow_id, b.level)}
                onToggle={() => onToggleStage(stageKey(workflow.workflow_id, b.level), b.expanded)}
              />
            ))}
          </div>
        </div>
      </div>
    </>
  )
}

/**
 * A STAGE TOO WIDE TO DRAW, DRAWN AS ONE BAND.
 *
 * WHAT IT REPLACES. Thirteen node cards, 3,560px of them, of which the 1440px
 * laptop the audit was taken on could show four. The band is one row: how many
 * steps, and what they are doing, by state.
 *
 * THE ONE THING IT MAY NOT DO IS HIDE A FAILURE. Somebody scanning a workflow
 * for what broke must not have to open four bands to find it, so three separate
 * mechanisms carry that and none of them is the word order alone:
 *
 *  * `stageCensus` puts FAILED, DEAD_LETTERED and CANCELLED at the FRONT of the
 *    count list. `.wf-band-counts` clips rather than wraps -- it has to, or the
 *    band's height would depend on how many states a stage happens to be in and
 *    `layoutOf` could not place the stage under it -- and putting the failures
 *    first is what makes that clip safe;
 *  * `.has-failure` is a modifier on the band itself: a bad-toned left accent
 *    and a tint, so a stage with something broken in it is distinguishable
 *    WITHOUT being read, at a glance down a collapsed graph;
 *  * every count carries a `.ctl-dot` in its own tone, because colour is never
 *    the only signal (types.ts `stateGlyph` makes the same argument for chips).
 *
 * AND THE ABSENCE OF A FAILURE IS NOT THE SAME AS NOT KNOWING. A stage whose
 * task states were not in the read gets `.has-unread` -- dashed and amber, the
 * treatment `.node.unknown` already uses -- and its sentence says the census
 * cannot be called clean. A clean band and an unread band must not look alike;
 * that is the whole of this console's honesty rule applied to a summary.
 *
 * IT SURVIVES EXPANSION. The band stays when the stage is open, because it is
 * the only control that closes it again (moving the control into the rail would
 * throw keyboard focus away on every toggle), and because on a 13-wide stage
 * the census is the only thing on screen that says what the part you have
 * scrolled past is doing.
 */
function StageBand({
  band,
  census,
  controls,
  onToggle,
}: {
  band: DagBand
  census: StageCensus
  controls: string
  onToggle: () => void
}) {
  const broken = census.failed + census.cancelled > 0
  const cls = [
    'wf-band',
    // `has-unread` first so that a stage that is BOTH unread and broken keeps
    // the dashed edge (from this rule) and takes the bad colour (from the one
    // declared after it). Two facts, two properties, one border.
    census.unread > 0 ? 'has-unread' : '',
    broken ? 'has-failure' : '',
    // NO `is-open` MODIFIER. Open and closed are told apart by the caret, which
    // is the same glyph in the same slot that `.wf-bar` uses one level up for
    // the same gesture; a second visual channel for it would be teaching a
    // reader two answers to one question.
  ]
    .filter((c) => c !== '')
    .join(' ')
  return (
    <button
      type="button"
      className={cls}
      style={{ left: band.x, top: band.y, width: band.w, height: band.h }}
      aria-expanded={band.expanded}
      // Only when the stage is open, because only then does the element exist.
      // `aria-controls` pointing at an id that is not in the document is worse
      // than omitting it: it tells a screen reader there is somewhere to go.
      aria-controls={band.expanded ? controls : undefined}
      aria-label={`${census.sentence} ${
        band.expanded
          ? 'Activate to collapse this stage back to one band.'
          : `Activate to draw all ${census.steps} steps.`
      }`}
      onClick={onToggle}
    >
      <span className="wf-band-n">
        {census.steps} step{census.steps === 1 ? '' : 's'}
      </span>
      <span className="wf-band-counts">
        {census.counts.map((c) => (
          <span key={c.word} className={`wf-band-count is-${c.tone}`}>
            <i className={dotClass({ tone: c.tone, derived: true })} aria-hidden />
            {c.n} {c.word}
          </span>
        ))}
      </span>
      {/* The same glyph and the same column as the workflow row's own caret,
          because it is the same gesture one level down. */}
      <span className="wf-caret" aria-hidden>
        {band.expanded ? '▾' : '▸'}
      </span>
    </button>
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
  tier,
  x,
  y,
  w,
  h,
  reload,
}: {
  step: WorkflowStep
  state: StepState
  workflow: Workflow
  now: number
  usage: UsageRead
  /**
   * How much this card is allowed to say. See `dag.ts`'s semantic-zoom section:
   * the tier is a set of FIELDS, and `w` is what those fields measure to for
   * this workflow. Both come from the layout, so the card cannot draw a row the
   * box was not sized for.
   */
  tier: ZoomTier
  x: number
  y: number
  /** `layout.nodeW`. It was `NODE_W`, which is now only the full tier's value
   *  -- a `details` node given it would have been 111px wider than the slot the
   *  layout placed, overlapping the sibling beside it. */
  w: number
  h: number
  reload: () => void
}) {
  const p = present(state)
  const dur = stepDuration(state, now)
  // THE FIGURES ARE THE TIER, so they are only computed at the tier that draws
  // them. `figuresFor` is pure and cheap, but computing four cells per node per
  // second for a canvas that is not drawing them is work done to be thrown
  // away, and on a 30-step run that is 120 cells a second.
  const showFigures = tier === 'figures'
  const f = showFigures ? figuresFor(state, usage, now) : null
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

  // THE WHOLE CARD IS THE LINK, AND THERE IS NOTHING LINKED INSIDE IT.
  //
  // The owner's words: "The agent task node should be clicable not have links
  // inisde it if all of them point to the same page or section." There were
  // THREE anchors on this node and all three landed on the same page: the step
  // id and `input & output →` both went to `#work/task/<id>`, and
  // `attempts →` went to `#work/task/<id>/attempts`, which `App.tsx` resolves
  // to the SAME drawer with its second tab selected -- one click away once you
  // are there, and reachable from the drawer's own tab strip. So all three
  // collapse into one target and `.node-links` is gone entirely.
  //
  // HOW THE STOP CONTROL SURVIVES THAT, which is the constraint the old shape
  // was built around: a `<button>` inside an `<a>` is invalid HTML, so the node
  // used to be a plain `<div>` with the anchor on the id alone. The card is the
  // anchor now, and the stop control is its SIBLING inside `.node-slot` --
  // absolutely positioned into a strip the card reserves with padding. Nothing
  // is nested in anything it may not be, the button is still a real button, and
  // every `title` on this card still belongs to a real ancestor of the element
  // it explains rather than sitting under a transparent overlay. That last
  // point is load-bearing: `.node-num`'s `title` is the CAUSE of an absence,
  // and a stretched-link overlay would have swallowed every one of them.
  // The card's contents, written once and mounted into either an `<a>` or a
  // `<div>` below. Not a component: it closes over everything already computed
  // here, and a second component would be a second place to forget a fact.
  const body = (
    <>
      <div className="node-id">
        {/* The step NAME. It is no longer its own anchor -- the card around it
            is -- but a step the workflow has not reached still has nothing to
            open, so that card is a plain `<div>` and the name is not a dead
            link. */}
        {taskId ? <span className="node-name">{step.step_id}</span> : step.step_id}
        {/* A ZOOM FIELD, AND THE FIRST ONE TO GO. The tag and its margin are
            96px -- more than two thirds of a `names` node -- and it marks ONE
            step in a workflow. Spending it on every card in a stage to label the
            single node that opens the pull request is the worst ratio of any
            field here, which is why the reduced tiers drop it and
            `nodeWidthAt` only counts it at the full tier. The role is still on
            the step's own task page, which the card is a link to. */}
        {showFigures && role === 'integrator' && (
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
      {/* THE ONE ROW THAT IS IN EVERY TIER. The mark carries the tone, the word
           carries the state -- so a failed or cancelled step is findable at the
           smallest zoom, by colour AND by shape AND by word, without expanding
           anything. That is not a nicety: the reason to look at a 30-step
           workflow at all is usually to find what broke.

           THE DURATION SHARES THIS ROW ONLY AT THE FULL TIER. Together they are
           the widest row on the card -- `dead_lettered` beside `duration
           unread` is 226px of the 227 a full node has -- and splitting them is
           precisely what lets a `details` node be 144px instead of 255. The
           trade is one row of height for 111px of width, on the axis that ran
           out. */}
      <div className="node-line">
        <i className={dotClass({ tone: p.tone, derived: p.derived })} aria-hidden />
        <span className="node-state">{p.word}</span>
        {showFigures && <StepTime dur={dur} />}
      </div>
      {/* The duration on a row of its own. Dropped entirely at `names`, where
          the canvas mark beside the zoom control says so -- never drawn blank,
          because an empty slot where `not started` belongs is the exact
          confusion between "no measurement" and "no problem" that this screen
          exists to prevent. */}
      {tier === 'details' && <StepTime dur={dur} />}
      {/* The llm used. The owner's "the way it's done today", kept verbatim.

          MEASURED AND NOT MERGED INTO THE ID ROW, which is the obvious way to
          save a row and does not fit: `scan-terraform` is 118px at the id's
          600-weight 14px mono and `claude-code` is 79px at --t-micro, which
          needs 205px against the 188px of content box NODE_W gives. Letting it
          wrap would make the node's height something `layoutOf` cannot know,
          and a node taller than the box the layout drew is the overlap defect
          `heightOf` exists to prevent. */}
      {tier !== 'names' && <div className="node-meta">{step.runner_profile}</div>}

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
          own foot.

          STILL A VERTICAL STACK, and that was measured rather than assumed.
          Four cells across a node would give each one about 40px of value
          column; `no attempt yet` measures 118px. A figure strip that ellipsed
          an absence WORD would turn the one encoding this screen may not lose
          into `not att...`, so the stack stays and the height is what it
          costs. */}
      {f !== null && (
        <dl className="node-nums">
          <NodeNum label="ran" cell={f.ran} pending={false} />
          <NodeNum label="cost" cell={f.cost} pending={pending} />
          <NodeNum label="tokens" cell={f.tokens} pending={pending} />
          <NodeNum label="ckpts" cell={f.checkpoints} pending={pending} />
        </dl>
      )}

      {/* THE DEPENDENCY LIST, AND ITS ARROW NOW POINTS THE WAY THE GRAPH RUNS.
          It read `← plan` when parents were to the left; parents are ABOVE, so
          it reads `↑ plan`. A glyph left pointing at the old axis is worse than
          no glyph: it is a second, wrong statement about the topology beside a
          correct one.

          IT STAYS VISIBLE AND IT STAYS UNTRUNCATED even though the drawn edges
          now say the same thing more directly. It is the only complete
          statement of the graph in text and the screen-reader route to it, and
          it names parents this workflow does not contain -- which is exactly
          the case that draws no edge at all. */}
      {step.depends_on.length > 0 && (
        <div className="node-dep" title={`depends on ${step.depends_on.join(', ')}`}>
          ↑ {step.depends_on.join(', ')}
        </div>
      )}
    </>
  )

  return (
    <div className="node-slot" style={{ left: x, top: y, width: w, height: h }}>
      {taskId ? (
        <a
          className={`node ${p.tone} zoom-${tier}`}
          title={p.title}
          href={`#work/task/${encodeURIComponent(taskId)}`}
        >
          {body}
        </a>
      ) : (
        <div className={`node ${p.tone} zoom-${tier}`} title={p.title}>
          {body}
        </div>
      )}
      {/* B28, on the node. Only when the step's TASK was actually joined: a
          step whose state is `unknown` was not in the task read, and offering
          to stop something this screen could not read would be acting on a
          guess. `StopRun` then decides for itself whether the state is one the
          cancel route accepts, so a terminal node draws nothing at all.

          A SIBLING OF THE CARD, NOT A CHILD OF IT. The card is an `<a>` now and
          a `<button>` may not live inside one. It sits in the strip `.node`
          reserves at its foot, so it overlaps nothing -- `NODE_H` counts that
          strip. */}
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
 * `#work/task/<id>` -- the route App.tsx already resolves to the full agent
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
    </span>
  )
}
