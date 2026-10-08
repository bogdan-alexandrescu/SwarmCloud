import {
  Fragment,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from 'react'

import {
  cancelWorkflow,
  loadWorkflow,
  loadWorkflowBoard,
  loadTaskEventsPage,
  loadWorkflowUsage,
  type CancelWorkflowResult,
  type ResourceClasses,
  type StepUsage,
  type WorkflowBoard,
  type WorkflowUsage,
} from './api'
import { classUnits, useResourceClasses } from './Blockers'
import {
  autoTier,
  censusWord,
  depUnits,
  edgeKinds,
  edgePath,
  finishedResultOf,
  foldMix,
  hasTokenKind,
  inputsByStep,
  layoutOf,
  levelsOf,
  lookClass,
  partialDeps,
  stepLook,
  profileMix,
  shapeOf,
  stageCensus,
  stageGlyph,
  stageMix,
  stepCostOf,
  totalCostCell,
  stepDuration,
  workflowSpend,
  dueSteps,
  noAgentSpend,
  CANVAS_COLUMN,
  PANEL_COLUMN,
  DECLARED_WORDS,
  NEVER_STARTED_WORD,
  PAD,
  SKIPPED_WORD,
  TIER_DROPS,
  ZOOM_TIERS,
  type DagBand,
  type DagLayout,
  type DagShape,
  type EdgeKind,
  type EdgeProvenance,
  type MixPart,
  type NoSpend,
  type StageCensus,
  type StageMix,
  type StepDuration,
  type StepInputs,
  type StepLook,
  type WorkflowSpend,
  type ZoomTier,
} from './dag'
import {
  absentCell,
  costCell,
  countCell,
  durationText,
  measuredCell,
  usd,
  type Absence,
  type Cell,
  FINISH_NOT_RECORDED,
  NEVER_RAN,
  NO_ATTEMPT_YET,
  STATE_UNREAD,
  USAGE_NOT_READ,
  USAGE_NOT_SAMPLED,
} from './measure'
import { workflowDispatchOf } from './Dispatch'
import { CountNote, Id, Screen } from './Shell'
import {
  axisOf,
  boardResultNote,
  failureGroups,
  failureCause,
  nodeNote,
  parentsDoneOf,
  resultTokenKinds,
  sameStepAcross,
  shapeSignature,
  stateEnteredAt,
  stateRankOf,
  stepOrder,
  stepTimes,
  stepWhy,
  ENTRY_EVENT,
  tokenKindsCell,
  tokenKindsTotal,
  workflowLabel,
  workflowPullRequest,
  mergeCardOf,
  VIEW_LABEL,
  type BoardTelemetryGap,
  type StepWhy,
  type WorkflowPullRequest,
  type WorkflowView,
} from './stepviews'
import type { Result } from './fetch'
import { STATE_MARK, StateMark } from './marks'
import { Button, Chip, NamedMark, ProgressBar, Segmented, Tabs, TypedConfirm, type ChipTone } from './components'
import { offerNewestWorkflows, recentName, rememberWorkflow, RECENT_WORKFLOWS_EVENT } from './Spine'
import { StopRun } from './StopRun'
import { AGE_TICK_MS, useNow as useSharedClock } from './useNow'
import { useInView } from './useInView'
import {
  bytesLabel,
  consequenceOf,
  dispatchOf,
  stateGlyph,
  stateTone,
  stepState,
  type StepState,
  type Task,
  type TaskDispatch,
  type TaskState,
  type Tone,
  type Workflow,
  type WorkflowDrift,
  type WorkflowStep,
  workflowHeaderState,
  workflowStartText,
  TERMINAL_STATES,
} from './types'
import {
  MergeStepCard,
  SourceNote,
  StepInspector,
  StrayMark,
  UnreadableMark,
  ViewControl,
  WorkflowTable,
  WorkflowTimeline,
  type AttemptLoader,
  type ScrubFocus,
  type SiblingRef,
  type StepRowModel,
  type StepLook as RowLook,
  WfStepMark,
} from './WorkflowViews'
import { gateVerdictOf, isReviewedBy, mergeOf, skippedByVerdict, verdictFor, type MergeRead, type VerdictRead } from './wfreview'
import './styles/workflows.css'
import './styles/names.css'
import {
  anyRunning,
  backLabel,
  bucketCounts,
  bucketOf,
  BUCKET_FILTERS,
  BUCKET_LABEL,
  derivedStateOf,
  listHref,
  matchesFilters,
  ownersOf,
  parseWorkflowQuery,
  phoneSummary,
  profilesOf,
  rowWhy,
  SORT_LABEL,
  sortWorkflows,
  stepsRead,
  workflowDuration,
  workflowHref,
  workflowQueryString,
  WORKFLOW_SORTS,
  type BucketFilter,
  type RowWhy,
  type WorkflowQuery,
  type WorkflowSort,
} from './workflowlist'

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
 * THE NODE IS ONE TARGET, AND IT PICKS. No anchors inside it: the step id and
 * both deep links resolved to the same task drawer, so the card became the one
 * target and the stop control its sibling. Since the owner's WF-7 decision
 * (epic #83) that target is a button that fills the inspector in place --
 * nothing on the canvas navigates -- and the inspector carries `open agent →`
 * to the drawer. Every node keeps the three facts it has always carried -- the
 * step NAME, its STATUS and its RUNNER PROFILE -- and adds the one that was
 * missing, how long it has taken.
 *
 * Step state is JOINED, not read off the step. `GET /v1/workflows` returns
 * steps with no state field at all; it only exists on the task a step created.
 * See `stepState` in types.ts for the three ways that join can come up empty
 * and why they must not render alike.
 *
 * AND AN OPEN WORKFLOW HAS THREE DRAWINGS NOW, not one (redesign-v2 §2.3, "one
 * object, several view modes"). The canvas above is the GRAPH; the TIMELINE
 * puts the same steps on one wall-clock axis with time waited drawn apart from
 * time run; the TABLE puts them in rows sortable by duration, cost, attempts and
 * state. The board's control reads Rows / Graph / Timeline / Table, and each
 * open card has its own Graph / Timeline / Table. Both new views are in
 * `WorkflowViews.tsx` and their arithmetic in `stepviews.ts`; every row is built
 * HERE, from the node's own readers, so a step reads the same in all three.
 * Picking a step in any of the three -- a node on the Graph too, since WF-7 --
 * opens the INSPECTOR under it, whose two
 * scrubbers walk the step's attempts and the same step across the board's
 * other workflows.
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
/**
 * THE STORES EVERY FORM OF THE BOARD KEEPS ABOVE ITS `Screen`'s KEY. The
 * comments on each are the reasons they sit there; they did not change when
 * the screen split into a list and a page per workflow (Workflows V2).
 */
function useBoardStores() {
  // Same reason as AgentDetail's: `Screen` keeps its retry nonce to itself, so
  // a mutation inside the board (stopping a step, cancelling the workflow)
  // needs a key bump to make the board re-read. Without it the node a moment
  // ago said RUNNING would keep saying it after the request was recorded.
  const [reloads, setReloads] = useState(0)
  const reload = useCallback(() => setReloads((n) => n + 1), [])

  // ABOVE THE `key`, DELIBERATELY. `reloads` remounts `Screen`, so anything
  // held inside it is lost on every stop-and-reload; a board that snapped every
  // open workflow shut the moment you stopped one step would be unusable.
  // WHICH STAGES THE READER HAS OPENED, and it is up here for a stronger
  // version of the same reason. A card can be re-opened with one click; a
  // stage you opened, scrolled sideways through and then lost because the
  // board re-read itself is the interaction the requirement for this feature
  // calls out by name -- "a stage that re-collapses under the user every 5
  // seconds is worse than no collapsing". The page now polls every 10s while
  // a workflow runs, which makes this store matter more, not less.
  //
  // Keyed by `stageKey`, so two workflows that both have a wide stage 1 do not
  // share one flag.
  const [stages, setStages] = useState<Record<string, boolean>>({})

  // HOW MUCH EACH WORKFLOW'S NODES SAY, keyed by workflow id. `auto` is not
  // stored: an absent entry MEANS auto, so a reader who never touched the
  // control cannot be holding a stale override from three polls ago.
  //
  // PER WORKFLOW RATHER THAN PER BOARD, because the tier is a function of the
  // workflow's own widest stage.
  const [zooms, setZooms] = useState<Record<string, ZoomChoice>>({})

  // THE PICKED STEP, ONE FOR THE WHOLE BOARD. Board-wide because the second
  // scrubber MOVES it between workflows; `focus` says which scrub control the
  // inspector should take focus on when it arrives.
  const [pick, setPick] = useState<PickedStep | null>(null)

  const toggleStage = useCallback(
    (key: string, expanded: boolean) => setStages((s) => ({ ...s, [key]: !expanded })),
    [],
  )
  const chooseZoom = useCallback((id: string, choice: ZoomChoice) => setZooms((z) => ({ ...z, [id]: choice })), [])

  return {
    reloads,
    reload,
    openStages: stages,
    toggleStage,
    zooms,
    chooseZoom,
    pick,
    choosePick: setPick,
  }
}

type BoardStores = ReturnType<typeof useBoardStores>

/** How often a page with a running workflow re-reads (workflows.html F; #117). */
export const WORKFLOW_POLL_MS = 10_000

/**
 * RE-READ EVERY 10s WHILE ANY WORKFLOW ON THE PAGE IS RUNNING, and not at all
 * once none is: a finished list has nothing left to change. `Screen` pauses
 * the timer while the tab is hidden and reads at once when it comes back.
 */
function workflowPoll(d: WorkflowBoard | null): number | null {
  return d !== null && anyRunning(d.workflows) ? WORKFLOW_POLL_MS : null
}

/**
 * WORKFLOWS V2 (owner's pick, 2026-10-01; workflows.html): a full-width LIST
 * at `/workflows`, and A PAGE PER WORKFLOW at `/workflows/<id>`, with its
 * Table and Timeline at `/workflows/<id>/table` and `/timeline`. The list's
 * filters ride on the address of both (`workflowlist.ts`), so the page's back
 * link returns to the list it came from.
 *
 * `view` is the route's query (App.tsx); a caller that passes no `onView`
 * gets a screen that keeps its address itself, so the controls still work.
 */
export function WorkflowsScreen({
  view = null,
  onView,
}: {
  view?: string | null
  onView?: (view: string) => void
} = {}) {
  const [localView, setLocalView] = useState<string>(view ?? '')
  const current = onView !== undefined ? (view ?? '') : localView
  const setView = useCallback((v: string) => (onView !== undefined ? onView(v) : setLocalView(v)), [onView])
  const query = useMemo(() => parseWorkflowQuery(current), [current])
  const choose = useCallback((next: WorkflowQuery) => setView(workflowQueryString(next)), [setView])
  const stores = useBoardStores()
  const name = useRecentName(query.wf)

  if (query.wf !== null) {
    const id = query.wf
    // A local, not an inline expression: the nav-heading test reads a literal or
    // module constant as the tab's heading ('Workflows', below) and skips a
    // per-workflow local like this one.
    const pageTitle = name ?? id
    return (
      <>
        <a className="wfp-back" href={listHref(query)}>
          ‹ {backLabel(query)}
        </a>
        <Screen
          key={`wf:${id}:${stores.reloads}`}
          // THE PAGE IS TITLED BY THE WORKFLOW'S NAME (#503: the id was the
          // `h1` and the name a second heading under it). The name is the
          // spec's label as Recent last read it -- the list or this page
          // records it -- and the id until one is known; the id stays on the
          // page whole, as the head's chip.
          title={pageTitle}
          help="absent-vs-zero"
          load={() => loadWorkflowPage(id)}
          pollMs={workflowPoll}
          // THE PAGE'S CENSUS, AS THE NOTE OVER THE FIRST CARD (workflows.html
          // B; #138, owner ruling 2026-10-07: no line under the title). The
          // screen's age is the head's refresh control's, not this note's.
          summary={(d) => pageSummary(d, id)}
          // THE PAGE'S OWN SKELETON (#113): its head row with the actions
          // disabled, its view tabs and a body card, so the board lands under
          // the tabs instead of pushing them in above generic rows.
          skeleton={<WorkflowPageSkeleton id={id} query={query} choose={choose} />}
          empty={{ heading: 'No workflows', body: 'Individually submitted tasks appear under Agents.' }}
        >
          {(d) => <WorkflowPage board={d} id={id} query={query} choose={choose} stores={stores} />}
        </Screen>
      </>
    )
  }

  return (
    <Screen
      key={`list:${stores.reloads}`}
      title="Workflows"
      // ONE `?` FOR THE WHOLE SCREEN (B7.4), AFTER ITS HEADING (AH-24):
      // `absent-vs-zero` is the rule every absent figure on the page obeys.
      help="absent-vs-zero"
      load={loadWorkflowBoard}
      pollMs={workflowPoll}
      // THE LIST'S OWN SKELETON (#113): its toolbar, disabled, and its table
      // head, so the first row lands where the skeleton's first row was.
      skeleton={<WorkflowListSkeleton query={query} />}
      summary={(d) => listSummary(d.workflows)}
      empty={{
        heading: 'No workflows',
        body: 'Individually submitted tasks appear under Agents.',
      }}
    >
      {(d) => <WorkflowList board={d} query={query} choose={choose} />}
    </Screen>
  )
}

/** The name Recent holds for `id`, kept current as the list or the page records it. */
function useRecentName(id: string | null): string | null {
  const [name, setName] = useState<string | null>(() => (id === null ? null : recentName(id)))
  useEffect(() => {
    const again = () => setName(id === null ? null : recentName(id))
    again()
    globalThis.addEventListener?.(RECENT_WORKFLOWS_EVENT, again)
    return () => globalThis.removeEventListener?.(RECENT_WORKFLOWS_EVENT, again)
  }, [id])
  return name
}

/** `4 steps · 1 → 2 → 1 · 1 of 4 done · by priya · started 8m ago`: one workflow's count note (#138). */
function pageSummary(d: WorkflowBoard, id: string): string {
  const w = d.workflows.find((x) => x.workflow_id === id)
  if (w === undefined) return 'not in this read'
  const shape = shapeOf(w.steps)
  const started = workflowStartText(w, d.taskById)
  return [
    `${shape.steps} step${shape.steps === 1 ? '' : 's'} · ${shape.text}`,
    rollupLine(w, d.taskById).text,
    `by ${ownerShort(w.submitted_by)}`,
    startedPhrase(started),
  ].join(' · ')
}

/**
 * THE HEAD'S START, IN WORDS THAT READ (owner QA R16, 2026-10-04): it was
 * `started ${text}` whatever the text, so an unread start printed "started
 * start not read" and a workflow with no step running "started never
 * started". An unread start is "started — not read"; one that never happened
 * is said as that; a known one is "started 8m ago".
 */
export function startedPhrase(started: { text: string; kind: 'never' | 'unread' | 'at' }): string {
  if (started.kind === 'unread') return 'started — not read'
  if (started.kind === 'never') return started.text
  return `started ${started.text}`
}

/** `2 running · 31 finished · 1 failed`, the list's count note over its first card (#138). */
function listSummary(workflows: readonly Workflow[]): string {
  const c = bucketCounts(workflows)
  return `${c.running} running · ${c.finished} finished · ${c.failed} failed`
}

/**
 * ONE WORKFLOW'S READ: the board, so the page can scrub the same step across
 * other workflows and its states join through the same task read. A workflow
 * past the board's 100 is read on its own (`GET /v1/workflows/{id}`) and
 * joined in, so a pasted link to an old workflow still opens it.
 */
export async function loadWorkflowPage(id: string): Promise<Result<WorkflowBoard>> {
  // THE PAGE'S OWN STEPS ARE READ BY ID, EVERY TIME (owner QA R8,
  // 2026-10-04). The board joins states through `GET /v1/tasks?limit=200`, a
  // window of the tenant's newest tasks, so an older workflow's steps fell
  // outside it: a succeeded 10/10 workflow drew every step `state unread`
  // and every cell `task unread` (12 of 267 joined) while its step card read
  // exit 0, 2m 56s and $0.89 for the same step. `GET /v1/workflows/{id}`
  // returns the workflow's own step tasks, so they are joined whether or not
  // the window reached them.
  const [b, one] = await Promise.all([loadWorkflowBoard(), loadWorkflow(id)])
  if (b.status === 'loading' || b.status === 'error') {
    // No board: the page is still its own workflow, if that read answered.
    if (one.status !== 'ok' && one.status !== 'stale') return b
    return {
      status: 'ok',
      fetchedAt: one.fetchedAt,
      data: { workflows: [one.data.workflow], taskById: new Map(one.data.tasks.map((t) => [t.id, t])), statesDetail: null },
    }
  }
  if (one.status !== 'ok' && one.status !== 'stale') return b
  const base: WorkflowBoard =
    b.status === 'ok' || b.status === 'stale' ? b.data : { workflows: [], taskById: new Map(), statesDetail: null }
  // A task read that failed stays failed for the OTHER workflows: their
  // steps were not read. This workflow's steps were, so a failed window
  // starts a map that holds only them, and the other workflows' steps --
  // absent from it -- still read as unread, never "not started".
  const taskById = new Map(base.taskById ?? [])
  for (const t of one.data.tasks) taskById.set(t.id, t)
  const inBoard = base.workflows.some((w) => w.workflow_id === id)
  return {
    status: 'ok',
    fetchedAt: b.status === 'ok' || b.status === 'stale' ? b.fetchedAt : one.fetchedAt,
    data: {
      ...base,
      workflows: inBoard ? base.workflows : [...base.workflows, one.data.workflow],
      taskById: base.taskById === null && one.data.tasks.length === 0 ? null : taskById,
    },
  }
}

/** The step the inspector is on, and how it got there. */
interface PickedStep {
  readonly workflowId: string
  readonly stepId: string
  /** Set when the selection MOVED into a card, so focus can follow it; cleared once it has. */
  readonly focus: ScrubFocus
}

/**
 * THE ATTEMPT READ, ONCE PER SURFACE, SHARED BY EVERY TOTAL ON IT (WF-5).
 *
 * It lived inside `Board`, so the step table's total read attempt telemetry
 * while the page head beside it and the list row summed the step results
 * alone, and one page showed two totals. The page now reads it once and hands
 * it to the head and the board; the list reads it for the rows it draws.
 * `firstId` is the workflow whose steps go first, so the sample cap never
 * leaves out the steps of the page's own workflow.
 */
function useWorkflowUsage(workflows: readonly Workflow[], firstId: string | null): UsageRead {
  // Every task a step points at, in board order. `loadWorkflowUsage` dedupes
  // and caps; the order decides which steps fall inside the cap, so it is the
  // board's own order rather than a set's iteration order -- except that the
  // page puts its own workflow first, so its steps are never the ones the cap
  // leaves out.
  //
  // AS ONE KEY, because the page now polls (every 10s while a workflow runs):
  // each poll is a new array, and re-reading every step's attempts on every
  // poll -- and blanking every figure to `reading` meanwhile -- would make the
  // figures flicker for no new information. A changed set of tasks re-reads.
  const taskKey = useMemo(() => {
    const ordered = [
      ...workflows.filter((w) => w.workflow_id === firstId),
      ...workflows.filter((w) => w.workflow_id !== firstId),
    ]
    return ordered
      .flatMap((w) => w.steps.map((s) => s.task_id ?? null).filter((id): id is string => id !== null))
      .join('\n')
  }, [workflows, firstId])

  const [usage, setUsage] = useState<UsageRead>({ kind: 'reading' })

  useEffect(() => {
    let live = true
    setUsage({ kind: 'reading' })
    loadWorkflowUsage(taskKey === '' ? [] : taskKey.split('\n')).then((r) => {
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
  }, [taskKey])
  return usage
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
  stores,
  focus,
  usage,
}: {
  board: WorkflowBoard
  stores: BoardStores
  /** One workflow's page (Workflows V2): draw only it, open, in its address's view. */
  focus: BoardFocus
  /** The page's attempt read (`useWorkflowUsage`), the one its head totals from too. */
  usage: UsageRead
}) {
  const { openStages, toggleStage, zooms, chooseZoom, pick, choosePick, reload } = stores
  // THE CARD ON SCREEN: the page's one workflow. Everything below that draws
  // or counts a card reads this, so the sample mark describes only it.
  const focusId = focus.id
  const shown = useMemo(() => board.workflows.filter((w) => w.workflow_id === focusId), [focusId, board.workflows])
  // WHERE THE SAME-STEP SCRUBBER MAY LAND: every workflow read, since a scrub
  // opens the other workflow's own page.
  const scrubbable = board.workflows
  // A PICK IN A WORKFLOW NOT ON SCREEN IS PUT DOWN. Otherwise the selection
  // lives on in a card nobody can see, and the scrubber keeps stepping from it.
  useEffect(() => {
    if (pick !== null && !shown.some((w) => w.workflow_id === pick.workflowId)) choosePick(null)
  }, [pick, shown, choosePick])
  // THE RESOURCE-CLASS CATALOGUE, read once for the board (#106): a step's why
  // line weighs its blockers against its class's `units`, as the Agents list
  // does, and forty cards each reading it would be forty reads of one table.
  const classes = useResourceClasses()
  // THE SAME STEP ACROSS THE BOARD, newest workflow first, for the picked
  // step's id -- the second scrubber's whole order, decided once here because
  // only the board holds every workflow. Each occurrence carries its own
  // state's mark, joined through the same `stepState` the nodes use.
  //
  // AND ONLY ACROSS WORKFLOWS OF THE PICKED ONE'S SHAPE (#112): the same id in
  // a differently shaped workflow is a different job, not a comparison.
  const siblings = useMemo(() => {
    if (pick === null) return { refs: [] as SiblingRef[], ids: [] as string[] }
    const anchor = scrubbable.find((w) => w.workflow_id === pick.workflowId)
    if (anchor === undefined) return { refs: [] as SiblingRef[], ids: [] as string[] }
    const found = sameStepAcross(scrubbable, pick.stepId, shapeSignature(anchor.steps))
    return {
      ids: found.map((f) => f.workflowId),
      refs: found.map((f) => {
        const l = rowLook(stepState(f.step, board.taskById))
        return { workflowId: f.workflowId, mark: l.mark, hue: l.hue, word: l.word }
      }),
    }
  }, [scrubbable, board.taskById, pick])

  const onPick = useCallback(
    (workflowId: string, stepId: string) =>
      choosePick(
        pick !== null && pick.workflowId === workflowId && pick.stepId === stepId
          ? null
          : { workflowId, stepId, focus: null },
      ),
    [pick, choosePick],
  )

  // MOVING THE SELECTION TO ANOTHER WORKFLOW OPENS THAT WORKFLOW'S PAGE, IN
  // THE VIEW THE READER WAS IN (WF-10, epic #83): a reader comparing one step's
  // attempts across workflows in the Table is not put back on a canvas at every
  // press, with the step folded into a stage band. A band holding the step is
  // marked and opens to it (`WorkflowGraph`).
  const onSibling = useCallback(
    (delta: -1 | 1) => {
      if (pick === null) return
      const at = siblings.ids.indexOf(pick.workflowId)
      const target = siblings.ids[at + delta]
      if (at < 0 || target === undefined) return
      focus.onOpen(target, focus.view)
      choosePick({ workflowId: target, stepId: pick.stepId, focus: delta < 0 ? 'newer' : 'older' })
    },
    [pick, siblings, choosePick, focus],
  )

  const onFocused = useCallback(() => {
    if (pick !== null && pick.focus !== null) choosePick({ ...pick, focus: null })
  }, [pick, choosePick])

  // THE ATTEMPT READ is the page's (`useWorkflowUsage`), handed in, so the
  // head, the table and the nodes read one set of figures (WF-5).

  // WHAT THE CARD IS DRAWING, stated once: the card below is handed exactly these.
  const viewOf = (): WorkflowView => focus.view
  const zoomOf = (id: string): ZoomChoice => zooms[id] ?? 'auto'

  return (
    <>
      {/* ONE STRIP OF CHROME ABOVE THE BOARD, and the count of sentences in it
          is zero (design-system.md §6.11). The control sits left; everything
          the board could not read sits right, as marks. Both banners this
          replaces were full-width panels that pushed the first row of actual
          data below the fold on a laptop. */}
      {/* DRAWN ONLY WHEN IT HAS SOMETHING TO SAY (walkthrough C, 2026-10-03):
          empty, it was a 32px strip and two gaps -- the ~100px band between
          the page's tabs and its card. The sample mark is in the Steps
          table's own head row now (walkthrough A). */}
      {board.statesDetail !== null && (
      <div className="ctl-toolbar wf-chrome">
        {/* NO BOARD CONTROLS: the page's view is its address. */}
        <span className="is-end wf-caveats">
          <StatesUnavailable detail={board.statesDetail} />
          {/* THE BOARD'S `?` IS NOT IN THIS STRIP (AH-24). It trailed the
              caveats -- `6/8 sampled ?`, a footnote on the figure -- and then
              led them, which followed nothing: the strip has no label. It
              explains a property of the whole screen, so it follows the
              screen's heading; see `help` on the Screen above. */}
        </span>
      </div>
      )}
      <div className="wf-board">
        {shown.map((w) => {
          // The selection, only when it is in THIS workflow. Every other card
          // gets null and draws no inspector.
          const mine = pick !== null && pick.workflowId === w.workflow_id ? pick : null
          return (
            <WorkflowCard
              key={w.workflow_id}
              workflow={w}
              taskById={board.taskById}
              openStages={openStages}
              onToggleStage={toggleStage}
              zoom={zoomOf(w.workflow_id)}
              onZoom={chooseZoom}
              view={viewOf()}
              onView={(_id: string, v: WorkflowView) => focus.onView(v)}
              page
              picked={mine === null ? null : mine.stepId}
              onPick={onPick}
              siblings={mine === null ? undefined : siblings.refs}
              onSibling={onSibling}
              scrubFocus={mine === null ? null : mine.focus}
              onScrubFocused={onFocused}
              reload={reload}
              usage={usage}
              classes={classes}
              tableFilter={focus.filter}
              onStageTable={focus.onStageTable}
              onClearFilter={focus.onClearFilter}
            />
          )
        })}
      </div>
    </>
  )
}

/** What one workflow's page tells the board it focuses. */
interface BoardFocus {
  readonly id: string
  /** The page's tab, from its address. */
  readonly view: WorkflowView
  readonly onView: (v: WorkflowView) => void
  /** Open another workflow's page, in a view: what the same-step scrubber does there. */
  readonly onOpen: (id: string, v: WorkflowView) => void
  /** The Table's stage filter from the address, or null. */
  readonly filter: StageFilter | null
  /** A band count: open the Table at one stage's steps in one state. */
  readonly onStageTable: (level: number, word: string) => void
  readonly onClearFilter: () => void
}

/** One stage's steps in one census word: what a band count opens the Table at. */
export interface StageFilter {
  readonly level: number
  readonly word: string
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

/**
 * The four ways a step ends, counted from the server's census. A derived
 * rollup is terminal only when every step is terminal and none is unstarted
 * (rollup.py `derive`), so on a terminal row these four account for every
 * step.
 */
interface Outcomes {
  succeeded: number
  failed: number
  cancelled: number
  deadLettered: number
}

/**
 * The outcomes, in the order the owner's settlement names them, each with the
 * word the census uses.
 */
const OUTCOME_SEGMENTS: readonly { key: keyof Outcomes; word: string }[] = [
  { key: 'succeeded', word: 'succeeded' },
  { key: 'failed', word: 'failed' },
  { key: 'cancelled', word: 'cancelled' },
  { key: 'deadLettered', word: 'dead_lettered' },
]

function rollupLine(workflow: Workflow, taskById: ReadonlyMap<string, Task> | null = null): Rollup {
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
  // EVERY TERMINAL STATE THE ROLLUP COUNTS, NOT TWO OF THEM. This read
  // `9/30 done · 4 failed` on a workflow where seventeen steps had been
  // cancelled: the census named two of the four ways a step ends, so the other
  // seventeen were simply missing from a line whose whole job is to account
  // for thirty. The words are the ones the node and the stage band print for
  // the same states, so one step reads the same at every level of the board.
  // Whether a finished row should still draw a progress meter was a separate
  // question (design-system.md §6.4), settled on #83.
  const done = roll.counts.SUCCEEDED ?? 0
  const failed = roll.counts.FAILED ?? 0
  const deadLettered = roll.counts.DEAD_LETTERED ?? 0
  const cancelled = roll.counts.CANCELLED ?? 0
  const unstarted = roll.counts.unstarted ?? 0
  const rest: string[] = []
  if (failed > 0) rest.push(...failedClauses(failed, workflow, taskById))
  if (deadLettered > 0) rest.push(`${deadLettered} dead_lettered`)
  if (cancelled > 0) rest.push(`${cancelled} cancelled`)
  if (unstarted > 0) rest.push(`${unstarted} not started`)
  const text = [`${done}/${total} done`, ...rest].join(' · ')

  // A TERMINAL ROW DRAWS WHAT ITS STEPS ENDED AS (WF-1, settled on #83,
  // 2026-09-25). Only when the four outcomes account for EVERY step: the
  // server derives a terminal state only then (rollup.py `derive`), and a
  // composition over fewer steps than the workflow has would leave part of the
  // track empty -- a gap that reads as nothing, for steps nobody counted. In
  // that case, which valid data never produces, the row keeps the meter.
  const outcomes: Outcomes = { succeeded: done, failed, cancelled, deadLettered }
  const ended = OUTCOME_SEGMENTS.reduce((n, s) => n + outcomes[s.key], 0)
  if (TERMINAL_STATES.has(roll.state as TaskState) && total > 0 && ended === total) {
    const parts = OUTCOME_SEGMENTS.filter((s) => outcomes[s.key] > 0).map((s) => `${outcomes[s.key]} ${s.word}`)
    return {
      text,
      trustworthy: true,
      done,
      total,
      why: `All ${total} steps ended: ${parts.join(', ')}.`,
    }
  }
  return {
    text,
    trustworthy: true,
    done,
    total,
    why: `${done} of ${total} steps done${rest.length > 0 ? `, ${rest.join(', ')}` : ''}.`,
  }
}

/**
 * THE ROW'S FAILURES, GROUPED BY CAUSE (#105): `4 failed: input collision`
 * rather than `4 failed`, so a board of ten workflows says which ones broke the
 * same way without opening any of them.
 *
 * The count is the census's; the causes are the task read's (`failureGroups`,
 * normalised by `failureCause`). A failure the read did not return, or whose
 * task wrote no error, is still counted -- in a clause of its own with no
 * cause -- and a read that found MORE failures than the census counted is not
 * trusted over it: the row falls back to the bare count rather than print two
 * numbers that disagree.
 */
function failedClauses(failed: number, workflow: Workflow, taskById: ReadonlyMap<string, Task> | null): string[] {
  const groups = failureGroups(workflow.steps, taskById).filter((g) => g.cause !== null)
  const named = groups.reduce((n, g) => n + g.n, 0)
  if (groups.length === 0 || named > failed) return [`${failed} failed`]
  const out = groups.map((g) => `${g.n} failed: ${g.cause}`)
  if (failed > named) out.push(`${failed - named} failed`)
  return out
}

/**
 * Whether a cancellation somebody asked for is still in flight.
 *
 * Only a DERIVED, COMPLETE rollup in a terminal state ends it. A rollup that
 * could not read every step, or an API that derived nothing, has not shown the
 * workflow is over -- and a request whose outcome was not read is still a
 * request, so the tag stays exactly as it was before this check existed.
 */
function cancelPending(workflow: Workflow): boolean {
  if (!workflow.cancel_requested) return false
  const roll = workflow.rollup
  const finished =
    workflow.state_source === 'derived' &&
    roll !== undefined &&
    roll.complete &&
    TERMINAL_STATES.has(roll.state as TaskState)
  return !finished
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
  usage,
  openStages,
  onToggleStage,
  zoom = 'auto',
  onZoom,
  view: viewProp,
  onView,
  picked: pickedProp,
  onPick,
  siblings,
  onSibling,
  scrubFocus = null,
  onScrubFocused,
  loadAttempts,
  reload,
  classes = null,
  page = false,
  tableFilter = null,
  onStageTable,
  onClearFilter,
}: {
  workflow: Workflow
  taskById: ReadonlyMap<string, Task> | null
  usage: UsageRead
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
  /**
   * How this workflow is drawn once open. OPTIONAL, and every one of the props
   * below it is too, for the reason `zoom` is: the card is exported and a caller
   * with no opinion must not have to state one. A caller that passes no
   * `onView` (or no `onPick`) gets a card that keeps the choice itself -- NOT a
   * control that does nothing, which would be a dead button on exactly the
   * surface that offered it.
   */
  view?: WorkflowView
  onView?: (id: string, v: WorkflowView) => void
  /** The picked step's id when the board's selection is in this workflow. */
  picked?: string | null
  onPick?: (workflowId: string, stepId: string) => void
  /** The picked step's occurrences across the board, newest first. */
  siblings?: readonly SiblingRef[]
  onSibling?: (delta: -1 | 1) => void
  scrubFocus?: ScrubFocus
  onScrubFocused?: () => void
  /** The attempt read the inspector uses. Injectable for the tests; the API's otherwise. */
  loadAttempts?: AttemptLoader
  reload: () => void
  /**
   * `GET /v1/resource-classes`, read once by the board (#106), so a why line
   * can tell a pool too small for a step's weight from a full one. OPTIONAL,
   * null by default: an unread catalogue keeps the pre-#66 reading, exactly as
   * it does on the Agents list.
   */
  classes?: ResourceClasses | null
  /**
   * ONE WORKFLOW'S PAGE (Workflows V2): under the Graph the card also draws
   * the step table, which the page shows below the graph and the step card.
   * The card has no row of its own and is always open -- the page draws the
   * workflow's head (`WorkflowHead`), and the list is `WorkflowList`.
   */
  page?: boolean
  /** The Table's stage filter (a band count opened it), on a workflow's page only. */
  tableFilter?: StageFilter | null
  onStageTable?: (level: number, word: string) => void
  onClearFilter?: () => void
}) {
  const roll = rollupLine(workflow, taskById)
  // WHEN IT STARTED (#376): the earliest step task's `started_at` -- the
  // workflow document records no start -- with `created_at` as its submit.
  const started = workflowStartText(workflow, taskById)
  // WHAT THE WORKFLOW IS CALLED AND WHAT IT OPENED (#330), both off the step
  // tasks the board already joined -- see `workflowLabel` and `workflowPullRequest`.
  const label = workflowLabel(workflow, taskById)
  const pr = workflowPullRequest(workflow, taskById)
  const strays = straysOf(workflow, taskById)
  const unreadable = unreadableOf(workflow, taskById)
  const sectionRef = useRef<HTMLElement | null>(null)
  const narrow = useNarrow()

  // THE LOCAL FALLBACKS, used only when the caller holds no store. The board
  // always passes both, so on the real screen these are never read.
  const [localView, setLocalView] = useState<WorkflowView>(viewProp ?? 'graph')
  const [localPick, setLocalPick] = useState<string | null>(null)
  const view: WorkflowView = onView !== undefined ? (viewProp ?? 'graph') : localView
  const chooseView = (v: WorkflowView) => (onView !== undefined ? onView(workflow.workflow_id, v) : setLocalView(v))
  const picked = onPick !== undefined ? (pickedProp ?? null) : localPick
  // WHETHER A READER HAS PICKED A STEP ON THIS CARD (QA G3-28): the page's
  // own pick on arrival is not one, so its card is not scrolled to.
  const readerPicked = useRef(false)
  const pickStep = (stepId: string) => {
    readerPicked.current = true
    return onPick !== undefined
      ? onPick(workflow.workflow_id, stepId)
      : setLocalPick((p) => (p === stepId ? null : stepId))
  }

  // CLOSING THE GRAPH'S PANEL PUTS FOCUS BACK ON THE NODE THAT OPENED IT, so a
  // keyboard reader is where they were rather than at the top of the page. The
  // node survives the close -- only the panel unmounts -- so it can take focus
  // at once.
  const closePanel = (stepId: string) => {
    pickStep(stepId)
    const node = [
      ...(sectionRef.current?.querySelectorAll<HTMLButtonElement>('button.node, button.wf-pick') ?? []),
    ].find((b) => b.dataset.step === stepId)
    node?.focus()
  }

  const inspector = (stepId: string, onClose: () => void) => (
    <InspectorSlot
      key={`${workflow.workflow_id}/${stepId}`}
      workflow={workflow}
      workflowLabel={label}
      stepId={stepId}
      taskById={taskById}
      classes={classes}
      usage={usage}
      siblings={siblings}
      onSibling={onSibling}
      focus={scrubFocus}
      onFocused={onScrubFocused}
      onClose={onClose}
      loadAttempts={loadAttempts}
      page={page}
      reload={reload}
    />
  )

  return (
    <section
      ref={sectionRef}
      className={`section wf-card is-open${pr !== null ? ' has-pr' : ''}${page ? ' is-page' : ''}`}
    >
      <div className="wf-body">
        {/* THE OPEN CARD'S HEAD STATES BOTH WHOLE (#376), with the UTC
            instant and age in each hover. */}
        {/* ON A WORKFLOW'S PAGE THE COUNT NOTE OVER THE FIRST CARD SAYS BOTH
            (#138): the census and when it started (`pageSummary`), so the
            body card does not say them a second time. */}
        {!page && (
          <ul className="ctl-facts wf-times">
            <li className="ctl-fact" title={started.title}>
              <b>started</b>
              {started.text}
            </li>
            <li className="ctl-fact" title={started.submittedTitle}>
              <b>submitted</b>
              {started.submitted}
            </li>
          </ul>
        )}
        {!page && <CensusFact roll={roll} />}
        <StateDrift drift={workflow.drift} />
        <WorkflowDispatchLine workflow={workflow} taskById={taskById} pr={pr} />
        {/* THE CARD'S OWN VIEW STRIP: which of the three drawings this is,
            and the one mark that belongs to the workflow rather than to any
            view -- staged files no edge can carry. Chrome, no prose (§6.11). */}
        {/* DRAWN ONLY WHEN IT HOLDS SOMETHING (walkthrough C): on a page,
            with no stray or unread mark, it was an empty row and a margin
            between the tabs and the graph. */}
        {(!page || strays.length > 0 || unreadable.length > 0) && (
          <div className="wf-viewbar">
            {/* The page's views are its underline tabs (`WfTabs`), under the title. */}
            {!page && <ViewControl view={view} onChoose={chooseView} />}
            <StrayMark strays={strays} />
            <UnreadableMark counts={unreadable} />
          </div>
        )}
        {view === 'graph' ? (
          // THE GRAPH AND ITS PANEL, SIDE BY SIDE (#330, owner request
          // 2026-09-29). The inspector sat under the canvas, which on a
          // tall graph is a screen away from the node that opened it. It is
          // a column on the right now, the graph narrows to make room, and
          // at phone width it is a bottom sheet (styles.css `.wf-split`).
          //
          // ON A WORKFLOW'S PAGE THE CARD SITS UNDER THE GRAPH (workflows.html
          // B; #503): beside it, the card's same-step strip ran out past its
          // right edge, and with the panel open the graph and the card no
          // longer fit side by side. `is-stack` lays the split as one column.
          <div className={`wf-split${picked !== null ? ' has-panel' : ''}${page ? ' is-stack' : ''}`}>
            <div className="wf-split-main">
              <WorkflowGraph
                workflow={workflow}
                taskById={taskById}
                classes={classes}
                usage={usage}
                openStages={openStages}
                onToggleStage={onToggleStage}
                zoom={zoom}
                onZoom={onZoom}
                picked={picked}
                onPick={pickStep}
                reload={reload}
                onStageTable={page ? onStageTable : undefined}
              />
              {page && <WfPhoneStages workflow={workflow} taskById={taskById} picked={picked} onPick={pickStep} />}
            </div>
            {picked !== null && (
              <StepPanel
                key={`${workflow.workflow_id}/${picked}`}
                onClose={() => closePanel(picked)}
                bring={readerPicked.current || !page}
              >
                {inspector(picked, () => closePanel(picked))}
              </StepPanel>
            )}
          </div>
        ) : null}
        {/* ONE WORKFLOW'S PAGE: THE STEP TABLE UNDER THE GRAPH (workflows.html
            B). The same rows the Table tab sorts, and a row picks the same
            step into the card beside the graph. */}
        {page && view === 'graph' && (
          <div className="wfp-steps">
            <WorkflowSteps
              title="Steps"
              workflow={workflow}
              taskById={taskById}
              classes={classes}
              usage={usage}
              view="table"
              picked={picked}
              onPick={pickStep}
            />
          </div>
        )}
        {/* THE TABLE AND THE TIMELINE, WITH THE INSPECTOR NEXT TO THE PICKED
            STEP (#110). It mounted under the whole view, about 650px below
            row 10 at 1440. At 1100px and up it docks in the right-hand column
            the Graph's panel uses; below that it opens directly under the
            picked row or track. The split is drawn whether or not a step is
            picked, so picking one never remounts the table and its sort. */}
        {view === 'graph' ? null : (
          <div className={`wf-split is-rows${picked !== null && !narrow ? ' has-panel' : ''}`}>
            <div className="wf-split-main">
              <WorkflowSteps
                workflow={workflow}
                taskById={taskById}
                classes={classes}
                usage={usage}
                view={view}
                picked={picked}
                onPick={pickStep}
                detail={picked !== null && narrow ? inspector(picked, () => closePanel(picked)) : null}
                filter={view === 'table' ? tableFilter : null}
                onClearFilter={onClearFilter}
              />
            </div>
            {picked !== null && !narrow && (
              <StepPanel key={`${workflow.workflow_id}/${picked}`} onClose={() => closePanel(picked)} bring={readerPicked.current || !page}>
                {inspector(picked, () => closePanel(picked))}
              </StepPanel>
            )}
          </div>
        )}
      </div>
    </section>
  )
}

/**
 * BELOW THIS WIDTH THE TABLE'S AND THE TIMELINE'S INSPECTOR OPENS UNDER THE
 * PICKED ROW rather than docking beside the view (#110). 1100px is where the
 * view keeps a readable width beside the panel's 300-400px column; below it
 * the docked panel would squeeze ten table columns into what is left.
 */
const DOCK_QUERY = '(max-width: 1099px)'

function subscribeNarrow(onChange: () => void): () => void {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return () => {}
  const query = window.matchMedia(DOCK_QUERY)
  query.addEventListener('change', onChange)
  return () => query.removeEventListener('change', onChange)
}

/** False where there is no `matchMedia` (jsdom), which is the desktop's answer. */
function atNarrowWidth(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia(DOCK_QUERY).matches
}

function useNarrow(): boolean {
  return useSyncExternalStore(subscribeNarrow, atNarrowWidth, () => false)
}

/**
 * The details panel beside the graph (#330): its own scroll, a close control
 * (the inspector's `×`), and Escape. Focus moves INTO it when it opens -- unless
 * the inspector already took it, which is what a same-step scrub does on
 * arrival -- and `onClose` is the caller's, which puts focus back on the node.
 */
function StepPanel({ onClose, children, bring = true }: { onClose: () => void; children: ReactNode; bring?: boolean }) {
  const ref = useRef<HTMLElement | null>(null)
  useEffect(() => {
    const el = ref.current
    if (el === null) return
    if (!el.contains(document.activeElement)) el.focus({ preventScroll: true })
    // THE CARD IS BROUGHT TO THE READER (browser QA, 2026-10-04): on a page
    // it opens under every node, a screen below the node that was clicked.
    // `nearest` moves the page only as far as the card needs.
    // ONLY WHEN A READER OPENED IT (QA G3-28). The page picks the first
    // failure on arrival, and bringing that card into view opened a failed
    // workflow's page ~920px down, past its head and its tabs.
    if (bring && typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
    // On mount only: this is about how the card ARRIVED.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  return (
    <aside
      ref={ref}
      className="wf-panel"
      tabIndex={-1}
      aria-label="Step details"
      onKeyDown={(e) => {
        if (e.key !== 'Escape' || e.defaultPrevented) return
        e.preventDefault()
        e.stopPropagation()
        onClose()
      }}
    >
      {children}
    </aside>
  )
}

/**
 * The row's pull request: `PR #N`, a link only when the result's URL is
 * http(s). No state word -- see `workflowPullRequest` for why the recorded one
 * is not shown.
 */
function PullRequestLink({ pr, className }: { pr: WorkflowPullRequest; className: string }) {
  const title = `Pull request #${pr.number}, opened by ${pr.stepId}. Its current state is not read here.`
  return pr.href !== null ? (
    <a className={`ctl-link ${className}`} href={pr.href} target="_blank" rel="noreferrer" title={title}>
      PR #{pr.number}
    </a>
  ) : (
    <span className={className} title={`${title} The recorded URL is not http(s), so it is not a link.`}>
      PR #{pr.number}
    </span>
  )
}

/** The band chip's tint: a step holding capacity, parked or failed takes its state's; the rest stay neutral. */
function chipTone(look: StepLook): ChipTone | undefined {
  if (look.kind !== 'state') return undefined
  return look.hue === 'live' || look.hue === 'park' || look.hue === 'bad' ? look.hue : undefined
}

/**
 * A STEP'S BRAND MARK (marks.tsx), drawn on the graph's nodes and the stage
 * bands. The mark is the shape channel and the hue the second one; the state
 * word always sits beside it, so neither is the only signal. A step whose task
 * was not read is the ring in AMBER -- the warning colour, never a state's hue.
 */
export function LookMark({ look }: { look: StepLook }) {
  const skipped = look.kind === 'state' && look.skipped === true
  return (
    <NamedMark
      className="wf-mk"
      mark={look.kind === 'unknown' ? 'queued' : skipped ? 'skipped' : look.mark}
      dataMark={look.kind === 'unknown' ? 'unknown' : skipped ? 'skipped' : look.mark}
      hue={look.kind === 'unknown' ? 'warn' : look.hue}
      hidden
    />
  )
}

/** The stage band's per-count modifier: `is-bad` for a failure, `is-unknown` for an unread step. */
function bandClass(look: StepLook): string {
  return look.kind === 'unknown' ? 'is-unknown' : `is-${look.hue}`
}

/**
 * THE CENSUS, WHOLE, AS THE OPEN CARD'S FIRST FACT (#223, owner decision
 * 2026-09-26).
 *
 * The page states the census whole, as a fact, rather than in a cut cell.
 *
 * `roll.text`, the same sentence the page head's facts print, so the two
 * cannot disagree. A plain value, not a `.ctl-mark`: an unread census already
 * says so in its own words ("2 of 6 steps unread").
 */
function CensusFact({ roll }: { roll: Rollup }) {
  return (
    <ul className="ctl-facts wf-census">
      <li className="ctl-fact">
        <b>progress</b>
        {roll.text}
      </li>
    </ul>
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
 * commonest first, at most two named and the rest counted -- fewer named when
 * two whole chips and the count do not fit the column (`foldMix`).
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
  // THE COLUMN'S WIDTH, MEASURED, so whole chips fold into `+N` instead of the
  // column cutting one mid-word (`moc`, half a `+`). `[mix]` is a
  // `minmax(0, 1.1fr)` track, so its width does not depend on what is drawn in
  // it and measuring it cannot feed back into itself. Null until measured --
  // `foldMix` then keeps the plain "two named, rest counted" rather than
  // guessing a width. A layout effect, so the first painted frame is already
  // the folded one.
  const ref = useRef<HTMLSpanElement | null>(null)
  const [room, setRoom] = useState<number | null>(null)
  useLayoutEffect(() => {
    const el = ref.current
    if (el === null) return
    const measure = () => setRoom(el.clientWidth > 0 ? el.clientWidth : null)
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const watch = new ResizeObserver(measure)
    watch.observe(el)
    return () => watch.disconnect()
  }, [])
  if (mix.length === 0) return <span className="wf-mix" ref={ref} />
  const { shown, rest } = foldMix(mix, room)
  const all = mix.map((m) => `${m.profile} ×${m.count}`).join(', ')
  return (
    <span className="wf-mix" ref={ref} title={`Runner profiles: ${all}`}>
      {shown.map((m) => (
        <Chip key={m.profile}>
          {m.profile}
          <span className="wf-mix-n">×{m.count}</span>
        </Chip>
      ))}
      {rest > 0 && <Chip faint>+{rest}</Chip>}
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
function Spend({ spend, short = false }: { spend: WorkflowSpend; short?: boolean }) {
  if (spend.usd === null) {
    // `title` AND `aria-label`. The title was already here and was never the
    // only route -- the word `not reported` is on the surface, in the absent
    // treatment, which is what the acceptance test asks for -- but a hover is
    // not a keyboard, and the sentence is the half that says what it is NOT.
    const why =
      spend.joined === 0
        ? 'No task was joined for this workflow, so nothing could have reported a cost. This is an absent measurement, not $0.00.'
        : `None of the ${spend.joined} joined step${spend.joined === 1 ? '' : 's'} reported a cost. This is an absent measurement, not $0.00.`
    // `short`: the list's narrow Cost column, where "not reported" was cut
    // to "not re…" (visual QA Q9). The honest short form is a dash with the
    // reason as its title and its name -- never a 0, never a cut word.
    return (
      <span className={`wf-spend absent${short ? ' c-dash' : ''}`} title={why} aria-label={why} role={short ? 'img' : undefined}>
        {short ? '—' : 'not reported'}
      </span>
    )
  }
  // THE DENOMINATOR IS THE STEPS THAT COULD HAVE SPENT (QA G3-02, G3-03): a
  // step skipped by its verdict gate, or one that never had an attempt, is a
  // known $0, so it is neither covered nor a gap. Every successful
  // review-gated workflow read `2/3` -- a floor -- over a step whose agent
  // was never started.
  const due = dueSteps(spend)
  const partial = spend.covered < due
  // WHICH RECORD THE FIGURES CAME FROM (WF-5). The total is the sum of the
  // figures the steps themselves show, and some of those are a result's rather
  // than the attempts' -- which the steps mark `from result`, so the total
  // says how many.
  const sources =
    spend.fromResult > 0
      ? ` (${spend.fromResult} from the step’s result summary, where no attempt telemetry read here had one)`
      : ''
  const known = [
    spend.skipped > 0 ? `${spend.skipped} ${SKIPPED_WORD}` : null,
    spend.noAttempt > 0 ? `${spend.noAttempt} with no attempt` : null,
  ].filter((w): w is string => w !== null)
  const none = known.length > 0 ? ` ${known.join(' and ')} spent nothing on an agent, a known $0 rather than a gap.` : ''
  const note = `${spend.covered} of ${due} step${due === 1 ? '' : 's'} that ran reported a cost${sources}.${none}${
    partial ? ' The rest have not reported one, so this is a floor rather than the total.' : ''
  }`
  return (
    <span className="wf-spend" title={note} aria-label={note}>
      {money(spend.usd)}
      {partial && (
        <span className="wf-spend-cov">
          {spend.covered}/{due}
        </span>
      )}
    </span>
  )
}

/**
 * Dollars of token cost, in the one format every workflow figure uses
 * (`measure.usd`; owner QA R10, 2026-10-04): the list printed '$0.4950',
 * '$7.6778' and '$12.92' side by side. Two decimals from a dollar up, three
 * significant figures below it -- $0.0312 keeps what $0.03 would lose.
 */
function money(v: number): string {
  return usd(v)
}

/** A task id cut to its kind and its last five characters: `task_…18680`. */
export function integratorShort(id: string): string {
  const m = /^([A-Za-z]+_)(.*)$/.exec(id)
  const prefix = m === null ? '' : m[1]!
  const rest = m === null ? id : m[2]!
  return rest.length <= 6 ? id : `${prefix}…${rest.slice(-5)}`
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
  pr,
}: {
  workflow: Workflow
  taskById: ReadonlyMap<string, Task> | null
  pr: WorkflowPullRequest | null
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
      pr={pr}
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
  pr,
}: {
  dispatch: TaskDispatch | null
  integratorTaskId: string | null
  steps: number
  joined: boolean
  pr: WorkflowPullRequest | null
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
        {/* `c.opens` IS THE CARD'S OWN SHORT VALUE (WF-13): a lowercase phrase
            that reads after its key -- `dispatch collect · opens no pull request
            and pushes nothing`. It printed `c.headline`, the forms' two-sentence
            statement, so every open card read "opens No pull request. Nothing
            is pushed." The full sentence stays where a choice is being made:
            the Dispatch option count and SubmitWorkflow's outcome (the
            Consequence box stopped repeating it under the option, TS-20). Both
            come out of one switch in `consequenceOf`, so the card and the form
            cannot disagree on the count. */}
        <li className="ctl-fact">
          <b>opens</b>
          {c.opens}
        </li>
        {/* THE INTEGRATOR, NAMED AS A TASK AND LINKED TO ITS AGENT (QA G3-25):
            `via 4fb18680` -- eight hex characters and no link -- read as a
            commit SHA. */}
        {dispatch.strategy === 'integrate' && integratorTaskId !== null && (
          <li className="ctl-fact">
            <b>integrator</b>
            <a
              className="ctl-link wf-integrator"
              href={`#work/task/${encodeURIComponent(integratorTaskId)}`}
              title={integratorTaskId}
              aria-label={`Open the integrator task ${integratorTaskId}`}
            >
              {integratorShort(integratorTaskId)}
            </a>
          </li>
        )}
        {/* THE SAME PULL REQUEST THE ROW NAMES (#330), here too because the
            row's copy sits in `[flags]`, which drops below 900px. */}
        {pr !== null && (
          <li className="ctl-fact">
            <b>pr</b>
            <PullRequestLink pr={pr} className="wf-pr-fact" />
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

// ---------------------------------------------------------------------------
// The timeline, the table and the inspector: what they are handed
// ---------------------------------------------------------------------------

/** No inputs declared and none staged -- the answer for a step the input join
 *  has no entry for, which is none of them, but the map lookup is typed. */
const NO_INPUTS: StepInputs = { declared: new Map(), stray: [], malformed: 0 }

/** Every staged file in this workflow that no edge can carry, for the card's mark. */
function straysOf(
  workflow: Workflow,
  taskById: ReadonlyMap<string, Task> | null,
): { stepId: string; input: StepInputs['stray'][number] }[] {
  const out: { stepId: string; input: StepInputs['stray'][number] }[] = []
  for (const [stepId, inputs] of inputsByStep(workflow.steps, taskById)) {
    for (const input of inputs.stray) out.push({ stepId, input })
  }
  return out
}

/**
 * Every step whose staged-input report held entries that could not be read,
 * with how many -- for the card's mark, so the count reaches every view and not
 * only the table cell.
 */
function unreadableOf(
  workflow: Workflow,
  taskById: ReadonlyMap<string, Task> | null,
): { stepId: string; n: number }[] {
  const out: { stepId: string; n: number }[] = []
  for (const [stepId, inputs] of inputsByStep(workflow.steps, taskById)) {
    if (inputs.malformed > 0) out.push({ stepId, n: inputs.malformed })
  }
  return out
}

/**
 * Attempts used, as a figure. `0 of 3` is a MEASURED zero -- a task that has
 * never been admitted -- and prints as a digit; a step with no task, or whose
 * task was not read, has no count at all.
 */
function attemptsCell(state: StepState): Cell {
  if (state.kind === 'unstarted') return absentCell(NEVER_RAN)
  if (state.kind === 'unknown') return absentCell(STATE_UNREAD)
  const over = attemptsOver(state)
  return measuredCell(
    `${state.task.attempt_count} of ${state.task.max_attempts}`,
    over > 0
      ? `Attempts used, of the attempts this step is allowed: ${over} more than its ceiling of ${state.task.max_attempts}.`
      : 'Attempts used, of the attempts this step is allowed.',
  )
}

/**
 * How many attempts past its ceiling a step has used -- 0 within it, and 0
 * when there is no count to compare.
 *
 * STRICTLY GREATER, not `>=`. `3 of 3` is a step that used every attempt it was
 * allowed, which is the ordinary end of a failing step and not a fault; `83 of
 * 3` is a count the ceiling was supposed to make impossible, and it was drawn
 * in exactly the same ink as `1 of 3`. The Agents row has the same rule for
 * the same two fields.
 */
function attemptsOver(state: StepState): number {
  if (state.kind !== 'state') return 0
  return Math.max(0, state.task.attempt_count - state.task.max_attempts)
}

/**
 * EVERY STEP OF ONE WORKFLOW, BUILT ONCE FOR BOTH VIEWS AND THE INSPECTOR.
 *
 * The figures come from `figuresFor` and the state's look from `present` --
 * the node's own readers -- so a step reads the same in the graph, on the
 * timeline and in the table. Three views of one object with three spellings of
 * its state would be three answers to one question, which is the defect the
 * header derivation was built to end one level up.
 *
 * IN THE GRAPH'S ORDER, level by level, so the table opens in the order the
 * reader just saw on the canvas.
 */
function stepRows(
  workflow: Workflow,
  taskById: ReadonlyMap<string, Task> | null,
  usage: UsageRead,
  now: number,
  classes: ResourceClasses | null = null,
): StepRowModel[] {
  const order = stepOrder(workflow.steps)
  const inputs = inputsByStep(workflow.steps, taskById)
  return workflow.steps
    .map((step) => {
      const state = stepState(step, taskById)
      const f = stepFigures(state, usage, now)
      // THE WAIT SPLITS WHERE THE LAST PARENT FINISHED (#107), read from the
      // same task read as the step's own state.
      const times = stepTimes(state, now, parentsDoneOf(step, workflow.steps, taskById))
      const taskId = state.kind === 'state' ? state.task.id : state.kind === 'unknown' ? state.taskId : null
      // The SORT value of the cost, read off the same usage the cell was built
      // from. Null wherever the cell is an absence, so an unmeasured cost can
      // never be ranked as a small one.
      // THE SAME FIGURE THE CELL SHOWS (WF-5): the telemetry's where it has one,
      // the result's where it does not -- so the cost column sorts on what it
      // prints, whichever record that is.
      const u =
        state.kind === 'state' && usage.kind === 'ready' && usage.usage !== null
          ? usage.usage.byTaskId.get(state.task.id)
          : undefined
      const spent = state.kind === 'state' && usage.kind !== 'reading' ? stepCostOf(state.task, u) : null
      const nothing = state.kind === 'state' && usage.kind !== 'reading' ? noAgentSpend(state.task, u) : null
      const row: StepRowModel = {
        step,
        taskId,
        look: rowLook(state),
        times,
        ran: f.ran,
        attempts: attemptsCell(state),
        attemptsOver: attemptsOver(state),
        cost: f.cost,
        tokens: f.tokens,
        costFrom: f.costFrom,
        tokensFrom: f.tokensFrom,
        // A result belongs to the attempt that FINISHED, so the inspector is
        // offered it only for a finished task (WF-5) -- by the same rule the
        // node, the table and the total borrow it by (`finishedResultOf`).
        result: state.kind === 'state' ? finishedResultOf(state.task) : null,
        pending: usage.kind === 'reading' && state.kind === 'state',
        inputs: inputs.get(step.step_id) ?? NO_INPUTS,
        // The table's `why` (#106): the same reader the graph's node line uses,
        // plus the cascade's named parent on a cancelled step.
        why: stepWhy(step, state, workflow.steps, taskById, classUnits(classes, step.resource_class)),
        sort: {
          order: order.get(step.step_id)?.order ?? Number.MAX_SAFE_INTEGER,
          stateRank: stateRankOf(state),
          waitedMs: times.waitedMs,
          ranMs: times.ranMs,
          attempts: state.kind === 'state' ? state.task.attempt_count : null,
          // A KNOWN $0 RANKS AS ZERO (`noAgentSpend`): the cell says `none`.
          costUsd: spent !== null ? spent.usd : nothing !== null ? 0 : null,
          // THE TOKENS COLUMN SORTS ON WHAT IT PRINTS (QA G3-33), by the cell's
          // own rule: the attempts' total where they reported one, else the
          // finished result's, else nothing -- an absence, which sorts last.
          tokens:
            state.kind !== 'state' || usage.kind === 'reading'
              ? null
              : ((u !== undefined && u.attempts > 0
                  ? tokenKindsTotal({ input: u.inputTokens, output: u.outputTokens, cacheRead: u.cacheReadTokens, cacheWrite: u.cacheCreationTokens })
                  : null) ??
                (() => {
                  const r = finishedResultOf(state.task)
                  return r === null ? null : tokenKindsTotal(resultTokenKinds(r))
                })() ??
                (nothing !== null ? 0 : null)),
        },
      }
      return row
    })
    .sort((a, b) => a.sort.order - b.sort.order)
}

/**
 * The timeline or the table for one open workflow.
 *
 * Its own component for the reason `WorkflowGraph` is one: it owns a 1Hz clock,
 * and that clock should tick only while a view that shows moving time is
 * mounted -- never on the collapsed rows.
 */
function WorkflowSteps({
  workflow,
  taskById,
  classes,
  usage,
  view,
  picked,
  onPick,
  detail = null,
  filter = null,
  onClearFilter,
  title = null,
}: {
  workflow: Workflow
  taskById: ReadonlyMap<string, Task> | null
  classes: ResourceClasses | null
  usage: UsageRead
  /** The table's head-row title, where it sits under the Graph. */
  title?: string | null
  view: Exclude<WorkflowView, 'graph'>
  picked: string | null
  onPick: (stepId: string) => void
  /** The inspector, drawn under the picked row or track (#110); null when it is docked or closed. */
  detail?: ReactNode
  /** Only one stage's steps in one census word (a band count opened the Table at them). */
  filter?: StageFilter | null
  onClearFilter?: () => void
}) {
  const now = useNow()
  const all = stepRows(workflow, taskById, usage, now, classes)
  // THE STAGE FILTER READS THE BAND'S OWN WORDS (`censusWord`) and its own
  // levels (`stepOrder`), so "3 running" on the band opens exactly those 3.
  const order = filter === null ? null : stepOrder(workflow.steps)
  const rows =
    filter === null || order === null
      ? all
      : all.filter((r) => order.get(r.step.step_id)?.level === filter.level && censusWord(stepState(r.step, taskById)) === filter.word)
  if (all.length === 0) return <span className="ctl-mark">no steps</span>
  if (view === 'table') {
    // THE TOTAL, SUMMED FROM THE FIGURES THE ROWS ABOVE IT SHOW (WF-5). The
    // page head sums the same read (`useWorkflowUsage`, handed to both), and
    // the list row sums its own read by the same rule, so it moves with the
    // cells: a sampled
    // step's figure is its attempts' sum, an unsampled finished step's is its
    // result's, and the total says how many of the latter.
    const spend = workflowSpend(workflow.steps, taskById, telemetryOf(usage))
    return (
      <>
        {filter !== null && (
          <p className="wf-filter">
            <Chip onRemove={onClearFilter} removeLabel="Clear the stage filter">
              stage {filter.level + 1} · {filter.word}
            </Chip>
            <span className="wf-filter-n">
              {rows.length} of {all.length} steps
            </span>
          </p>
        )}
        <WorkflowTable
          rows={rows}
          picked={picked}
          onPick={onPick}
          detail={detail}
          title={title}
          // THE SAMPLE MARK QUALIFIES THESE FIGURES, SO IT IS IN THIS TABLE'S
          // HEAD ROW, inside the card (walkthrough A, 2026-10-03): it was a
          // chip in a strip above the card, floating apart from what it qualifies.
          head={usage.kind === 'ready' && usage.usage !== null ? <SampleNote usage={usage.usage} workflow={workflow} /> : null}
        />
        <p className="wf-table-total">
          <span>Total</span> <Spend spend={spend} />
        </p>
      </>
    )
  }
  const created = new Date(workflow.created_at).getTime()
  return (
    <WorkflowTimeline
      rows={rows}
      axis={axisOf(
        rows.map((r) => r.times),
        now,
        Number.isFinite(created) ? created : null,
      )}
      picked={picked}
      onPick={onPick}
      detail={detail}
    />
  )
}

/**
 * The inspector for the picked step, with its own clock.
 *
 * KEYED BY WORKFLOW AND STEP where it is mounted, so picking a different step
 * starts a fresh attempt read and a fresh attempt position rather than showing
 * the last step's third attempt against this step's name.
 */
function InspectorSlot({
  workflow,
  workflowLabel: label,
  stepId,
  taskById,
  classes,
  usage,
  siblings,
  onSibling,
  focus,
  onFocused,
  onClose,
  loadAttempts,
  page = false,
  reload,
}: {
  workflow: Workflow
  /** The spec's label, or null: the inspector's head names the workflow by it (#330). */
  workflowLabel: string | null
  stepId: string
  taskById: ReadonlyMap<string, Task> | null
  classes: ResourceClasses | null
  usage: UsageRead
  siblings: readonly SiblingRef[] | undefined
  onSibling: ((delta: -1 | 1) => void) | undefined
  focus: ScrubFocus
  onFocused: (() => void) | undefined
  onClose: () => void
  loadAttempts: AttemptLoader | undefined
  /** On a workflow's page the card carries the step's Stop and its review or merge card. */
  page?: boolean
  reload: () => void
}) {
  const now = useNow()
  const row = stepRows(workflow, taskById, usage, now, classes).find((r) => r.step.step_id === stepId)
  // A picked step that has left the workflow -- the board re-read and it is
  // gone -- has nothing to inspect. Drawing nothing is right: the selection is
  // stale, and there is no step whose facts could be shown.
  if (row === undefined) return null
  const state = stepState(row.step, taskById)
  // WITHOUT A BOARD, THE ONLY OCCURRENCE IS THIS ONE: "workflow 1 of 1", with
  // both directions disabled, rather than a scrubber that pretends to have
  // somewhere to go.
  const refs: readonly SiblingRef[] =
    siblings !== undefined && siblings.length > 0
      ? siblings
      : [{ workflowId: workflow.workflow_id, mark: row.look.mark, hue: row.look.hue, word: row.look.word }]
  const index = Math.max(
    0,
    refs.findIndex((s) => s.workflowId === workflow.workflow_id),
  )
  return (
    <StepInspector
      workflowId={workflow.workflow_id}
      workflowLabel={label}
      workflowStarted={workflowStartText(workflow, taskById, now)}
      row={row}
      taskState={state.kind === 'state' ? state.state : null}
      siblings={refs}
      siblingIndex={index}
      shape={shapeOf(workflow.steps)}
      onSibling={onSibling ?? (() => {})}
      focus={focus}
      onFocused={onFocused ?? (() => {})}
      onClose={onClose}
      now={now}
      load={loadAttempts}
    >
      {page ? <StepCardExtras workflow={workflow} step={row.step} state={state} taskById={taskById} reload={reload} now={now} /> : null}
    </StepInspector>
  )
}

/**
 * WHAT THE PAGE'S STEP CARD ADDS (workflows.html B, agent-detail-2.html A3):
 * Stop step beside Open agent, then a review step's verdict card and the merge
 * step's checklist, each drawn only from what the step's tasks recorded.
 */
function StepCardExtras({
  workflow,
  step,
  state,
  taskById,
  reload,
  now,
}: {
  workflow: Workflow
  step: WorkflowStep
  state: StepState
  taskById: ReadonlyMap<string, Task> | null
  reload: () => void
  now: number
}) {
  const tasks =
    taskById === null
      ? []
      : workflow.steps.flatMap((s) => {
          const t = s.task_id === null || s.task_id === undefined ? undefined : taskById.get(s.task_id)
          return t === undefined ? [] : [{ ...t, step_id: t.step_id ?? s.step_id }]
        })
  const task = state.kind === 'state' ? state.task : null
  const verdict = task === null ? null : verdictFor(task.id, tasks)
  const reviewed = task !== null && verdict === null && isReviewedBy(task.id, tasks)
  // A merge-profile step draws the MS4 card (WorkflowViews `MergeStepCard`)
  // in place of the checklist below, which reads the retired design's block.
  const mergeCard = task === null ? null : mergeCardOf(task, tasks)
  const merge = task === null || mergeCard !== null ? null : mergeOf(task)
  return (
    <>
      {task !== null && (
        <div className="wf-inspect-acts" role="group" aria-label="Stop step">
          <StopRun task={task} what={`step ${step.step_id}`} workflow={workflow} step={step} reload={reload} variant="inline" />
        </div>
      )}
      {verdict !== null && <VerdictCard verdict={verdict} />}
      {reviewed && (
        <p className="wf-verdict is-pending">
          {/* ONE SENTENCE, THE MARK INLINE (QA G3-19): a colon after the
              dashed mark started the next line as `: no step that…`. */}
          <b>Review verdict</b> <span className="wf-cell is-absent">not read yet</span> — no step that gates on this review
          has run, and the review's verdict file is read only by them.
        </p>
      )}
      {mergeCard !== null && <MergeStepCard card={mergeCard} now={now} />}
      {merge !== null && <MergeCard merge={merge} />}
    </>
  )
}

/**
 * THE REVIEW VERDICT (agent-detail-2.html A3): MERGE or NOT_YET, and the
 * findings grouped Blocker, Major, Minor, then Not graded. The findings are the
 * review agent's own words, printed as plain text; a finding without a
 * severity goes under Not graded, never Blocker.
 */
function VerdictCard({ verdict }: { verdict: VerdictRead }) {
  const tone = verdict.verdict === 'MERGE' ? 'is-merge' : verdict.verdict === 'NOT_YET' ? 'is-notyet' : 'is-other'
  return (
    <section className="wf-verdict" aria-label="Review verdict">
      <div className="wf-card-h">
        <b>Review verdict</b>
        <span className={`wf-verdict-pill ${tone}`}>{verdict.verdict}</span>
        <span className="wf-card-r">
          read by {verdict.readBy}
          {verdict.file !== null && ` · ${verdict.file}`}
        </span>
      </div>
      {verdict.groups.map((g) => (
        <div key={g.key} className={`wf-sev is-${g.key}`}>
          <span className="wf-sev-h">
            {g.label} <span className="wf-sev-n">{g.items.length}</span>
          </span>
          {g.items.map((text, i) => (
            <p key={i} className="wf-finding">
              {text}
            </p>
          ))}
        </div>
      ))}
      <p className="wf-card-foot">
        The review agent's own words, as plain text.{' '}
        {verdict.groups.length === 1
          ? 'No finding carries a severity, so none is graded.'
          : 'A finding without a severity goes under Not graded, never Blocker.'}{' '}
        {verdict.dropped !== null && verdict.dropped > 0 ? `${verdict.dropped} more were not kept.` : ''}
      </p>
    </section>
  )
}

const CHECK_GLYPH: Readonly<Record<MergeRead['checks'][number]['state'], string>> = {
  passed: '✓',
  failed: '✕',
  waiting: '⏸',
  not_read: '—',
}

/**
 * THE MERGE STEP'S CHECKLIST (agent-detail-2.html A3, A3b): its outcome, the
 * refusal code, then every check in the design's row order with its state.
 * A check past the first refusal reads "not read", not a cross.
 */
function MergeCard({ merge }: { merge: MergeRead }) {
  return (
    <section className="wf-merge" aria-label="Merge checks">
      <div className="wf-card-h">
        <b>Merge</b>
        <span className={`wf-merge-out${merge.refusal !== null ? ' is-bad' : ''}`}>{merge.outcome}</span>
        {merge.label !== null && <span className="wf-merge-label">{merge.label}</span>}
        {merge.refusal !== null && <span className="wf-card-r">refusal {merge.refusal.code}</span>}
      </div>
      {merge.headline !== null && (
        <p className="wf-merge-head">
          <b>{merge.headline}</b>
          {merge.refusal?.message != null && <> {merge.refusal.message}</>}
        </p>
      )}
      {merge.checks.length > 0 && (
        <ol className="wf-checks">
          {merge.checks.map((c, i) => (
            <li key={i} data-check={c.state} className={`wf-check is-${c.state}`}>
              <span className="wf-check-m" aria-hidden>
                {CHECK_GLYPH[c.state]}
              </span>
              <span className="wf-check-t">
                {c.name}
                {c.message !== null && <small>{c.message}</small>}
                {c.state === 'not_read' && <small>not read</small>}
              </span>
              {c.code !== null && (c.state === 'failed' || c.state === 'not_read') && <span className="wf-check-c">{c.code}</span>}
            </li>
          ))}
        </ol>
      )}
      <p className="wf-card-foot">
        {merge.mergedByThisTask === null
          ? 'Whether this step merged was not recorded.'
          : merge.mergedByThisTask
            ? 'This step merged the pull request.'
            : 'This step did not merge the pull request.'}{' '}
        From the step's result_summary.merge. The console shows the gate and the label; it changes neither.
      </p>
    </section>
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
 * `Segmented`, THE SAME CANONICAL CONTROL AS THE CARD'S VIEW CONTROL, one level
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
    <Segmented
      className="wf-zoom-seg"
      label="How much each step says"
      value={choice}
      options={options.map(([value, label, title]) => ({ key: value, label, title }))}
      onChange={(value) => onChoose(workflowId, value)}
    />
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
 * SO IT LIVES ON THE CANVAS, ONCE, in the board's own chrome. It is the same
 * argument the single `?` on this screen makes: the fact is a property of the
 * zoom, not of any one row, and drawn per node it would be one mark per step.
 *
 * A ZOOM IS A VIEW CHOICE, NOT A READ, SO IT TAKES NO DATA MARK (WF-14, epic
 * #83). This was a `.ctl-mark.is-partial` -- the amber, one-sided-dash mark
 * that says a READ came back partial, and which `SampleNote` still wears for
 * exactly that. On a zoom it made the reader's own choice, or the automatic
 * one made for them, look like something the platform had failed to report.
 * It is `.wf-zoom-note` now: the same words, faint mono type, no border, no
 * dash and no hue. The explanatory sentence is its accessible name and its
 * title, so nothing it said is lost.
 *
 * SILENT AT THE FULL TIER, because `TIER_DROPS.figures` is empty, and shown
 * whenever the drawn tier drops fields, under Auto as well as under a chosen
 * tier. A note saying "nothing is hidden" on every canvas that fits would be
 * noise of the kind §8.4 removed from this screen twice already.
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
  const sentence =
    `This canvas is drawn at the ${TIER_LABEL[tier]} zoom` +
    (choice === 'auto' ? ', chosen automatically so that the widest stage fits the column' : ', which you chose') +
    `, so no step is drawing its ${list}. That is a decision about the zoom and not a fact about the steps: every one of those fields still exists and is still measured or still absent exactly as it was. Choose Figures above, or open a step, to see them.`
  return (
    <span className="wf-caveat wf-zoom-note" role="status" aria-label={sentence} title={sentence}>
      {list} not drawn
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
 * touch, the scrollbar), every node is a `<button>` in the tab order and focusing
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
 * ITS HEIGHT IS 45px: one line of summary that clears the 44px touch target.
 * It was `BAND_H` when a band was one line; the band has three rows since the
 * wide-stage pick (2026-10-02) and the map kept its one. Its width is the
 * wrapper's, so nothing about it is a number somebody chose.
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
          // A FAILURE, NOT A CANCELLATION: the same rule, for the same reason,
          // as the band's own `.has-failure` below.
          const broken = stageCensus(b.steps, taskById).failed > 0
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
            // THE NODE'S OWN TINT (#405, owner 2026-10-01): the state pair the
            // card, the table row and the timeline span are drawn in.
            className={`wf-mini-node ${lookClass(stepLook(stepState(n.step, taskById)))}`}
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
/**
 * At most this many event reads per poll. A wide stage of parked steps would
 * otherwise be one request per step every time the task record moved; the
 * rest keep their lower bound (`stepDuration`'s `≥`) until a later poll.
 */
const ENTERED_READS = 24

/** One wait, as the record read now says it: a new park is a new key. */
const enteredKey = (t: Task): string => `${t.id}:${t.state}:${t.updated_at}`

/**
 * WHEN EACH WAITING STEP ENTERED ITS STATE (#503), from its newest events
 * page, for the steps that are PARKED, LEASED or DISPATCHED. The record keeps
 * no such time and the API serves none (types.ts `Task`), so the node's
 * `parked 7m` was timed from `updated_at`, and a slot held read `queued`.
 * Read once per wait: a key is the task, its state and its last write.
 */
function useEnteredAt(
  steps: readonly WorkflowStep[],
  taskById: ReadonlyMap<string, Task> | null,
): (task: Task) => number | null {
  const [entered, setEntered] = useState<ReadonlyMap<string, number | null>>(() => new Map())
  const asked = useRef(new Set<string>())
  const waiting: Task[] = []
  for (const s of steps) {
    const t = s.task_id ? taskById?.get(s.task_id) : undefined
    if (t !== undefined && ENTRY_EVENT[t.state] !== undefined) waiting.push(t)
  }
  const want = waiting.map(enteredKey).join('|')
  // A READ OUTLIVES THE EFFECT THAT ASKED FOR IT: a poll that moves one
  // step's record re-runs the effect, and the reads already in flight for the
  // others still land. Only unmounting drops them.
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])
  useEffect(() => {
    const fresh = waiting.filter((t) => !asked.current.has(enteredKey(t))).slice(0, ENTERED_READS)
    for (const t of fresh) {
      const key = enteredKey(t)
      asked.current.add(key)
      // A failed read leaves the lower bound, which is still true.
      void loadTaskEventsPage(t.id, { order: 'desc' })
        .then((r) => (r.status === 'ok' ? stateEnteredAt(t.state, r.data.events) : null))
        .catch(() => null)
        .then((at) => {
          if (mounted.current) setEntered((m) => new Map(m).set(key, at))
        })
    }
    // `want` is the waits' identity; `waiting` is rebuilt every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [want])
  return (task) => entered.get(enteredKey(task)) ?? null
}

function WorkflowGraph({
  workflow,
  taskById,
  classes,
  usage,
  openStages,
  onToggleStage,
  zoom,
  onZoom,
  picked,
  onPick,
  reload,
  onStageTable,
}: {
  workflow: Workflow
  taskById: ReadonlyMap<string, Task> | null
  classes: ResourceClasses | null
  usage: UsageRead
  openStages: Record<string, boolean>
  onToggleStage: (key: string, expanded: boolean) => void
  zoom: ZoomChoice
  onZoom?: (id: string, choice: ZoomChoice) => void
  /** The step the inspector is on, outlined so the canvas says which one it is. */
  picked: string | null
  /** Clicking a node puts its step in the inspector (WF-7). */
  onPick: (stepId: string) => void
  reload: () => void
  /**
   * Open the Table filtered to one stage and one census word (a band count's
   * click, wide-workflows.html A). Only a workflow's page has a Table to open;
   * elsewhere the counts are words, not controls.
   */
  onStageTable?: (level: number, word: string) => void
}) {
  const now = useNow()
  const enteredAt = useEnteredAt(workflow.steps, taskById)
  // THE STEP UNDER THE POINTER OR FOCUS, whose own edges are lit with the
  // picked step's (wide-workflows.html A: "lit on hover/select"). Focus does
  // what hover does, so it is not a mouse-only reading.
  const [hovered, setHovered] = useState<string | null>(null)
  const levels = levelsOf(workflow.steps)
  // WHAT EACH STEP DECLARED AND WHAT ARRIVED, joined once per render. Every
  // edge's style and every node's `↑` line read this, so the two cannot
  // disagree about whether a file landed.
  const inputs = inputsByStep(workflow.steps, taskById)
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
  //
  // WHAT EACH NODE SAYS UNDER ITS STATE (#105, #106): a failure's first error
  // line, or why a waiting step is not running. Decided BEFORE the layout,
  // because the layout has to measure the line into the node's height --
  // a card that drew a row its box was not given overlaps the one beneath it.
  // And before the tier now, because a noted card is taller and `autoTier`
  // asks whether the whole graph fits one screen (#330 item 4).
  const notes = new Map<string, StepWhy>()
  for (const s of workflow.steps) {
    const n = nodeNote(s, stepState(s, taskById), workflow.steps, taskById, classUnits(classes, s.resource_class))
    if (n !== null) notes.set(s.step_id, n)
  }
  const noted = new Set(notes.keys())
  // A SETTLED STEP RESERVES NO STOP STRIP (browser QA D29): its task is
  // terminal, so `StopRun` can never draw on its card again.
  const settled = new Set(
    workflow.steps
      .filter((s) => {
        const st = stepState(s, taskById)
        return st.kind === 'state' && TERMINAL_STATES.has(st.task.state)
      })
      .map((s) => s.step_id),
  )
  // THE DETAILS PANEL TAKES ITS COLUMN FROM THE GRAPH (#330 item 5). While a
  // step is picked the panel stands on the right, so under Auto the graph is
  // laid out for what is left beside it rather than scrolling sideways under
  // it. ONLY WHEN THAT CANNOT FOLD A STAGE: a graph with a band at full width
  // keeps its full-width layout, and one without is laid out with `band`
  // false, so no stage -- the picked step's included -- vanishes into a band
  // because the panel opened. A tier the reader chose is left as chosen.
  const fullAuto = autoTier(workflow.steps, CANVAS_COLUMN, noted, true, settled)
  const beside =
    picked !== null &&
    zoom === 'auto' &&
    !layoutOf(workflow.steps, undefined, fullAuto, noted, CANVAS_COLUMN, settled).wide.some(Boolean)
  const column = beside ? CANVAS_COLUMN - PANEL_COLUMN : CANVAS_COLUMN
  const auto = beside ? autoTier(workflow.steps, column, noted, false, settled) : fullAuto
  const tier: ZoomTier = zoom === 'auto' ? auto : zoom
  const layout = layoutOf(workflow.steps, expandedStages, tier, noted, column, settled)
  // HOW EACH EDGE IS PAINTED. Not always the pair's own kind: every edge into
  // or out of a COLLAPSED stage shares one path, so those are painted as one
  // edge with the weakest claim any of them can support -- see `edgeKinds`.
  const kinds = edgeKinds(layout, inputs)
  // WHICH EDGES ARE LIT: the hovered step's, else the picked step's, own edges
  // in and out. With either, every other edge fades; with neither, none does.
  const focusStep = hovered ?? picked
  const edgeLight = (e: { from: string; to: string }): string =>
    focusStep === null ? '' : e.from === focusStep || e.to === focusStep ? ' is-lit' : ' is-faded'

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

  // A STAGE BAND HOLDING THE PICKED STEP OPENS TO IT (WF-10, epic #83). When
  // the scrubber carries a step into this workflow, or a step picked in the
  // Table is looked at on the Graph, the step may be one of thirteen folded
  // into a band -- picked, and nowhere on the canvas. So the stage opens, and
  // the canvas is brought round to the step (below). Only when the pick
  // ARRIVES: a reader who closes the stage again while the step is still
  // picked is not overruled on the next render.
  const pickedLevel = picked === null ? -1 : levels.findIndex((l) => l.some((s) => s.step_id === picked))
  const pickedFolded = pickedLevel >= 0 && layout.wide[pickedLevel] === true && !expandedStages.has(pickedLevel)
  const bringIntoView = useRef<'picked' | null>(null)
  useEffect(() => {
    if (!pickedFolded) return
    bringIntoView.current = 'picked'
    onToggleStage(stageKey(workflow.workflow_id, pickedLevel), false)
    // On the pick's arrival only -- see above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [picked, workflow.workflow_id])

  // THE CANVAS OPENS ON ITS START, NOT ON AN EMPTY LANE (WF-9, epic #83).
  // Levels are centred against the widest one -- kept, by the owner's decision
  // -- so on a canvas wider than its column the start node sits in the middle
  // and the wrapper, opening at scrollLeft 0, showed the empty left of the
  // widest level instead: at 390 an empty `starts` lane with the start node
  // off to the right, and an opened 13-wide stage showed blank levels. So on
  // mount, and whenever a stage is OPENED (the canvas widens and the start
  // moves to its new middle), the wrapper scrolls the roots into view, centred
  // in the part of the column the sticky rail does not cover. When the stage
  // opened because the picked step was in it, the picked step is brought into
  // view instead.
  const opened = [...expandedStages].sort((a, b) => a - b).join(',')
  const seenOpen = useRef<string | null>(null)
  useLayoutEffect(() => {
    const before = seenOpen.current
    seenOpen.current = opened
    const grew =
      before === null ||
      opened
        .split(',')
        .some((k) => k !== '' && !before.split(',').includes(k))
    if (!grew) return
    const el = wrapRef.current
    if (el === null || el.clientWidth <= 0) return
    const wantPicked = bringIntoView.current === 'picked'
    bringIntoView.current = null
    const pickedNode = wantPicked ? layout.nodes.find((n) => n.step.step_id === picked) : undefined
    let span: readonly [number, number] | null = null
    if (pickedNode !== undefined) {
      span = [pickedNode.x, pickedNode.x + layout.nodeW]
    } else if (layout.wide[0] === true) {
      const band = layout.bands.find((b) => b.level === 0)
      if (band !== undefined) span = [band.x, band.x + band.w]
    } else {
      const roots = layout.nodes.filter((n) => n.level === 0)
      if (roots.length > 0) {
        span = [Math.min(...roots.map((n) => n.x)), Math.max(...roots.map((n) => n.x)) + layout.nodeW]
      }
    }
    if (span === null) return
    // The canvas sits after the sticky rail and the graph's gap; both are read
    // off the rendered boxes (0 where nothing is laid out, as in jsdom).
    const rail = el.querySelector<HTMLElement>('.wf-levels')
    const graph = el.querySelector<HTMLElement>('.wf-graph')
    const railW = rail?.offsetWidth ?? 0
    const gap = graph === null ? 0 : Number.parseFloat(getComputedStyle(graph).columnGap) || 0
    const canvasLeft = railW + gap
    const visible = el.clientWidth - railW
    const [from, to] = span
    const left =
      to - from <= visible
        ? canvasLeft + (from + to) / 2 - railW - visible / 2
        : canvasLeft + from - railW - PAD
    el.scrollLeft = Math.max(0, Math.min(left, el.scrollWidth - el.clientWidth))
    syncView()
    // On mount and when a stage opens, and on nothing else: the 1Hz clock
    // re-renders this canvas every second, and a scroll that followed every
    // render would take the canvas out of the reader's hands.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opened])

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
      {(zoomable || layout.edges.length > 0) && (
        <div className="wf-zoom">
          {zoomable && (
            <>
              <ZoomControl
                workflowId={workflow.workflow_id}
                choice={zoom}
                auto={auto}
                onChoose={onZoom ?? (() => {})}
              />
              <ZoomNotice tier={tier} choice={zoom} />
              <Minimap layout={layout} taskById={taskById} view={view} onJump={jump} />
            </>
          )}
          <EdgeKey />
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
                {/* USER-SPACE UNITS, 10.5px. It was 7 in the default
                    `strokeWidth` units against the 1.5px edge -- 10.5px -- and a
                    data edge is drawn heavier, which would have scaled its head
                    with it. One arrowhead size for every kind of edge; the
                    stroke is what differs. */}
                <marker
                  id={`arrow-${workflow.workflow_id}`}
                  viewBox="0 0 8 8"
                  refX="7"
                  refY="4"
                  markerUnits="userSpaceOnUse"
                  markerWidth="10.5"
                  markerHeight="10.5"
                  orient="auto-start-reverse"
                >
                  <path className="wf-arrowhead" d="M 0 1 L 7 4 L 0 7 z" />
                </marker>
              </defs>
              {/* TWO PASSES: EVERY HALO, THEN EVERY STROKE.

                  THE HALO IS WHY THE NODES LOST THEIR SHADOWS. Fourteen drop
                  shadows on one screen bought one thing: an edge crossing a
                  card read as passing under it rather than into it. A node is
                  always at least one level below every parent, but it can be
                  MORE than one, so a long edge does cross the band between --
                  the separation is real and had to go somewhere. It is on the
                  EDGE, which is the thing doing the crossing: a wider stroke in
                  the canvas's own colour, under the line, so the line carries
                  its own clearance.

                  UNDER EVERY LINE, NOT JUST ITS OWN. Each edge used to be one
                  group of halo-then-stroke, and SVG paints in document order,
                  so a later edge's halo painted straight over an earlier edge's
                  arrowhead wherever two converged on one join -- the arrows into
                  a fan-in were erased by their own siblings. The halos are now
                  one layer beneath all the strokes, so a halo can only ever
                  clear space UNDER a line, never through one.

                  The halo layer's groups carry the same kind classes as the
                  strokes, because the sheet widens a data edge's halo through
                  `.wf-link.is-data .wf-edge-halo`; they carry no `data-edge`,
                  because they are clearance and not a mark. */}
              <g className="wf-edge-halos">
                {layout.edges.map((e) => (
                  <g
                    key={`${e.from}->${e.to}`}
                    className={linkClass(kinds.get(`${e.from}->${e.to}`) ?? 'order')}
                  >
                    <path className="wf-edge-halo" d={edgePath(e)} />
                  </g>
                ))}
              </g>
              {layout.edges.map((e) => (
                // THE EDGE'S KIND IS ON THE GROUP (viz #1, #9). `is-order` is a
                // dependency that only orders the two steps; `is-data` also
                // stages a file, and then `is-staged` / `is-declared` says
                // whether anything has REPORTED the file arriving. Weight and
                // dash carry it, never hue alone, so a greyscale screenshot keeps
                // all three apart. Still one group per pair: a data edge is a
                // kind of edge, not a second one drawn over the first. One `<g>`
                // per (parent, child) pair keyed by that pair, so the guarantee
                // that one dependency draws one mark is unchanged.
                <g
                  key={`${e.from}->${e.to}`}
                  className={`${linkClass(kinds.get(`${e.from}->${e.to}`) ?? 'order')}${edgeLight(e)}`}
                  data-edge={`${e.from}->${e.to}`}
                >
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
            {/* `_level` because the STEPS of a level are no longer read here: the
                cards come from `layout.nodes` filtered by level, and whether the
                stage is banded comes from `layout.wide`. It was `level.length`
                passed to `stageIsWide`, which is the full tier's threshold and
                would have wrapped a six-step stage the `details` tier draws in
                full. The map is over the levels so that the index is the level
                index and nothing has to be recovered. */}
            {layout.levels.map((_level, i) => {
              const cards = layout.nodes
                .filter((n) => n.level === i)
                .map((n) => (
                  <StepNode
                    key={n.step.step_id}
                    step={n.step}
                    state={stepState(n.step, taskById)}
                    inputs={inputs.get(n.step.step_id) ?? NO_INPUTS}
                    note={notes.get(n.step.step_id) ?? null}
                    noteId={`wf-note-${workflow.workflow_id}-${n.step.step_id}`}
                    picked={picked === n.step.step_id}
                    onPick={onPick}
                    workflow={workflow}
                    now={now}
                    enteredAt={enteredAt}
                    usage={usage}
                    tier={layout.tier}
                    x={n.x}
                    y={n.y}
                    w={layout.nodeW}
                    h={n.h}
                    reload={reload}
                    onHover={setHovered}
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
                mix={stageMix(b.steps, taskById)}
                cause={stageCause(b.steps, notes)}
                controls={stageDomId(workflow.workflow_id, b.level)}
                picked={picked !== null && b.steps.some((s) => s.step_id === picked) ? picked : null}
                onToggle={() => onToggleStage(stageKey(workflow.workflow_id, b.level), b.expanded)}
                onPick={onPick}
                onCount={onStageTable === undefined ? undefined : (word) => onStageTable(b.level, word)}
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
  mix,
  cause,
  controls,
  picked,
  onToggle,
  onPick,
  onCount,
}: {
  band: DagBand
  census: StageCensus
  /** The stage's state mix, slots held and named steps (`stageMix`; wide-workflows.html A). */
  mix: StageMix
  /** What broke in this stage, when something did (#105). `stageCause`. */
  cause: StageCause | null
  controls: string
  /**
   * The picked step's id when it is one of this stage's steps (WF-10). The
   * band is marked with it -- a picked step folded into a band was picked and
   * nowhere on the canvas -- and `WorkflowGraph` opens the stage to it.
   */
  picked: string | null
  onToggle: () => void
  /** A named chip picks its step, as its card would. */
  onPick: (stepId: string) => void
  /** A count opens the Table filtered to this stage and that word; absent, counts are words. */
  onCount?: (word: string) => void
}) {
  // `failed` ONLY -- which already counts DEAD_LETTERED (`stageCensus`). A
  // stage of cancelled and succeeded steps was painted with the failure rule
  // and tint, so a workflow somebody stopped on purpose looked, down its whole
  // collapsed graph, like one that broke. A cancellation is something a person
  // asked for; it keeps its own count, first after the failures, in its own
  // tone, and the sentence still names it -- it just does not paint the band.
  const broken = census.failed > 0
  const cls = [
    'wf-band',
    // `has-unread` first so that a stage that is BOTH unread and broken keeps
    // the dashed edge (from this rule) and takes the bad colour (from the one
    // declared after it). Two facts, two properties, one border.
    census.unread > 0 ? 'has-unread' : '',
    broken ? 'has-failure' : '',
    // THE SELECTION IS NOT A STATE, so it takes no hue: the same three-sided
    // `--text` outline `.node.is-picked` draws, and a word (WF-10).
    picked !== null ? 'holds-picked' : '',
    band.expanded ? 'is-opened' : '',
  ]
    .filter((c) => c !== '')
    .join(' ')
  const first = band.steps[0]?.step_id ?? ''
  const last = band.steps[band.steps.length - 1]?.step_id ?? ''
  // SLOTS HELD AND WAITING (invariant 1): only LEASED to RUNNING hold
  // capacity, so a parked or ready step is named as waiting and never counted
  // as running. With unread steps both are floors, and the line says so.
  const hold = holdLine(mix)
  const chips = mix.chips.slice(0, BAND_CHIPS)
  const more = mix.chips.length - chips.length
  return (
    // A GROUP, NOT A BUTTON, SINCE THE WIDE-STAGE PICK: the band now holds
    // controls of its own -- each count opens the Table, each chip picks its
    // step -- and a button may not hold a button. The disclosure is the first
    // row, `.wf-band-toggle`, and it keeps the census as its name.
    <div
      className={cls}
      style={{ left: band.x, top: band.y, width: band.w, height: band.h }}
      role="group"
      aria-label={`Stage ${band.level + 1}`}
      data-level={band.level}
    >
      <button
        type="button"
        className="wf-band-toggle"
        aria-expanded={band.expanded}
        // Only when the stage is open, because only then does the element exist.
        // `aria-controls` pointing at an id that is not in the document is worse
        // than omitting it: it tells a screen reader there is somewhere to go.
        aria-controls={band.expanded ? controls : undefined}
        aria-label={`${census.sentence}${hold === null ? '' : ` It ${hold.said}.`}${cause === null ? '' : ` ${cause.said}`}${picked === null ? '' : ` It holds the picked step, ${picked}.`} ${
          band.expanded
            ? 'Activate to collapse this stage back to one band.'
            : `Activate to draw all ${census.steps} steps.`
        }`}
        onClick={onToggle}
      >
        <span className="wf-band-n">
          {census.steps} step{census.steps === 1 ? '' : 's'}
        </span>
        {first !== '' && <span className="wf-band-range">{first === last ? first : `${first} … ${last}`}</span>}
        <MixBar parts={mix.parts} className="wf-band-mix" />
        {hold !== null && (
          <span className="wf-band-hold" title={hold.title}>
            {hold.text}
          </span>
        )}
        {/* The same glyph and the same column as the workflow row's own caret,
            because it is the same gesture one level down. */}
        <span className="wf-caret">
          {band.expanded ? 'close' : `show all ${census.steps}`} <span aria-hidden>{band.expanded ? '▾' : '▸'}</span>
        </span>
      </button>
      <span className="wf-band-counts">
        {census.counts.map((c) =>
          onCount === undefined ? (
            <span key={c.word} className={`wf-band-count ${bandClass(c.look)} ${lookClass(c.look)}`}>
              <LookMark look={c.look} />
              {c.n} {c.word}
            </span>
          ) : (
            <button
              key={c.word}
              type="button"
              className={`wf-band-count ${bandClass(c.look)} ${lookClass(c.look)}`}
              aria-label={`Open the Table at the ${c.n} ${c.word} step${c.n === 1 ? '' : 's'} of stage ${band.level + 1}`}
              onClick={() => onCount(c.word)}
            >
              <LookMark look={c.look} />
              {c.n} {c.word}
            </button>
          ),
        )}
        {/* WHY IT BROKE, ON ONE LINE (#105). The commonest cause, as the first
            step that failed of it wrote it, and how many other causes there
            are; every failed step's whole error is the title. After the
            counts, because the counts are what says a failure is here at all. */}
        {cause !== null && (
          <span className="wf-band-cause" title={cause.full}>
            {cause.text}
          </span>
        )}
      </span>
      {/* THE STEPS THAT NEED A LOOK, BY NAME: failed, then holding capacity,
          then parked, each a pick target, so a failure is visible without
          opening anything, by name and not only by count. */}
      <span className="wf-band-chips">
        {chips.map((c) => (
          <Chip key={c.stepId} tone={chipTone(c.look)} pressed={c.stepId === picked} onClick={() => onPick(c.stepId)}>
            <LookMark look={c.look} />
            {c.stepId}
          </Chip>
        ))}
        {more > 0 && <Chip faint>+{more} more</Chip>}
        {/* WHICH STEP IS PICKED IN HERE, by name, on the band itself (WF-10),
            when it is not one of the chips already. */}
        {picked !== null && !chips.some((c) => c.stepId === picked) && <span className="wf-band-pick">{picked}</span>}
      </span>
    </div>
  )
}

/** How many steps a phone stage card names before it counts the rest: the frame's two. */
const PHONE_NAMED = 2

/**
 * THE GRAPH AT PHONE WIDTH (workflows.html B, phone frame): the stages as
 * stacked cards, top to bottom -- `first · 1 step`, then `then · 4 steps` --
 * each naming the steps that need a look (failed, holding capacity, parked,
 * as a band names them; else the first in stage order) and counting the rest.
 * A step picks into the card under it, as a node does. The stylesheet shows
 * this, and hides the canvas, only below 561px; a level still never wraps.
 */
function WfPhoneStages({
  workflow,
  taskById,
  picked,
  onPick,
}: {
  workflow: Workflow
  taskById: ReadonlyMap<string, Task> | null
  picked: string | null
  onPick: (stepId: string) => void
}) {
  const levels = levelsOf(workflow.steps)
  if (levels.length === 0) return null
  return (
    <ol className="wf-ph-stages" aria-label="Stages">
      {levels.map((level, i) => {
        const urgent = stageMix(level, taskById).chips.map((c) => c.stepId)
        const named = [...urgent, ...level.map((s) => s.step_id).filter((id) => !urgent.includes(id))].slice(0, PHONE_NAMED)
        const more = level.length - named.length
        return (
          <li key={i} className="wf-ph-stage">
            <span className="wf-ph-h">
              {i === 0 ? 'first' : 'then'} · {level.length} step{level.length === 1 ? '' : 's'}
            </span>
            {named.map((id) => {
              const s = level.find((x) => x.step_id === id)!
              const state = stepState(s, taskById)
              const l = rowLook(state)
              return (
                <button
                  key={id}
                  type="button"
                  className={`wf-ph-step ${lookClass(stepLook(state))}${id === picked ? ' is-picked' : ''}`}
                  data-ph-step={id}
                  aria-pressed={id === picked}
                  title={l.title}
                  onClick={() => onPick(id)}
                >
                  <WfStepMark look={l} />
                  <span className="wf-ph-name">{id}</span>
                  <span className="wf-ph-state">{l.word}</span>
                </button>
              )
            })}
            {more > 0 && <span className="wf-ph-more">+{more} more in this stage</span>}
          </li>
        )
      })}
    </ol>
  )
}

/** How many steps a band names before it counts the rest: the frame's five. */
const BAND_CHIPS = 5

/** `holds 3 slots · 2 waiting hold none`, its title, and the clause the band's name gains; null with neither. */
function holdLine(mix: StageMix): { text: string; title: string; said: string } | null {
  if (mix.holding === 0 && mix.waiting === 0) return null
  const slots = `holds ${mix.holding} slot${mix.holding === 1 ? '' : 's'}`
  const wait = mix.waiting === 0 ? '' : ` · ${mix.waiting} waiting hold none`
  const floor = mix.unread > 0 ? ` · ${mix.unread} not read` : ''
  return {
    text: `${slots}${wait}${floor}`,
    title: `${mix.holding} step${mix.holding === 1 ? ' is' : 's are'} leased, dispatched, starting or running, and each holds its capacity. ${mix.waiting} ${mix.waiting === 1 ? 'is' : 'are'} queued, ready or parked and hold${mix.waiting === 1 ? 's' : ''} none.${mix.unread > 0 ? ` ${mix.unread} step state${mix.unread === 1 ? ' was' : 's were'} not read, so both figures are a floor.` : ''}`,
    said: `${slots}${mix.waiting === 0 ? '' : `, and ${mix.waiting} waiting hold none`}${mix.unread > 0 ? `, counting only the steps that were read` : ''}`,
  }
}

/** The words for a mix part, as the bar's hover says them. */
const MIX_WORD: Readonly<Record<MixPart['kind'], string>> = {
  bad: 'failed',
  live: 'holding capacity',
  park: 'parked',
  wait: 'waiting',
  done: 'succeeded',
  skip: 'skipped by verdict',
  can: 'cancelled',
  unk: 'not read',
}

/**
 * A STATE-MIX BAR (wide-workflows.html A): proportions at a glance, in the
 * brand order. Decoration over the counts beside it, which say every figure,
 * so it is hidden from assistive technology; each part's hover names it.
 */
function MixBar({ parts, className }: { parts: readonly MixPart[]; className: string }) {
  return (
    <span className={className} aria-hidden>
      {parts.map((p) => (
        <i key={p.kind} className={`mx-${p.kind}`} style={{ flexGrow: p.n }} title={`${p.n} ${MIX_WORD[p.kind]}`} />
      ))}
    </span>
  )
}

/** A band's failure line (#105): `text` on the band, `full` its title, `said`
 *  the sentence its accessible name gains. */
interface StageCause {
  readonly text: string
  readonly full: string
  readonly said: string
}

/**
 * What broke in a stage, from the same notes its nodes would draw: the causes
 * of its failed steps, grouped by `failureCause`, the commonest first (ties to
 * the first failed step in stage order). The band prints that group's first
 * error line and counts the other causes; null for a stage with no failure.
 */
function stageCause(steps: readonly WorkflowStep[], notes: ReadonlyMap<string, StepWhy>): StageCause | null {
  const failed = steps.flatMap((s) => {
    const n = notes.get(s.step_id)
    return n !== undefined && n.kind === 'cause' ? [{ stepId: s.step_id, note: n }] : []
  })
  if (failed.length === 0) return null
  const groups = new Map<string, { first: StepWhy; n: number }>()
  for (const f of failed) {
    // `text` is the error's first line, or `no error recorded` for a failure
    // that wrote none -- which then groups as its own cause.
    const key = failureCause(f.note.text) ?? f.note.text
    const g = groups.get(key)
    if (g === undefined) groups.set(key, { first: f.note, n: 1 })
    else g.n += 1
  }
  // `Map` keeps insertion order, so a stable sort on the count leaves ties in
  // stage order.
  const ordered = [...groups.values()].sort((a, b) => b.n - a.n)
  const lead = ordered[0]!
  const others = ordered.length - 1
  const text = `${lead.first.text}${others > 0 ? ` (+${others} other cause${others === 1 ? '' : 's'})` : ''}`
  return {
    text,
    full: failed.map((f) => `${f.stepId}: ${f.note.full}`).join('\n\n'),
    said: `It failed with: ${lead.first.text}${others > 0 ? `, and ${others} other cause${others === 1 ? '' : 's'}` : ''}.`,
  }
}

/** How each of the three step-state kinds presents. Kept together so the
 *  difference between "not started" and "not read" stays deliberate.
 *
 *  `derived` says whether a state was computed at all. At STEP level it is always true: a step whose task was not
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
    case 'state': {
      // A STEP ITS VERDICT GATE KEPT FROM RUNNING (#264) ends SUCCEEDED like
      // one whose agent did the work. It never reads like one: its word says
      // the agent was skipped, and its title says by which verdict.
      if (state.state === 'SUCCEEDED' && skippedByVerdict(state.task)) {
        const verdict = gateVerdictOf(state.task)
        return {
          tone: stateTone(state.state),
          glyph: stateGlyph(state.state),
          word: SKIPPED_WORD,
          title: `Task ${state.task.id} SUCCEEDED without running its agent: the review verdict was ${verdict ?? 'not recorded'}, which this step's gate does not run on. The step still published the reviewed work.`,
          derived: true,
        }
      }
      return {
        tone: stateTone(state.state),
        glyph: stateGlyph(state.state),
        word: state.state.toLowerCase(),
        title: `Task ${state.task.id}, attempt ${state.task.attempt_count} of ${state.task.max_attempts}`,
        derived: true,
      }
    }
  }
}

/** How a step's state is drawn in the Table, the Timeline and the inspector: the node's own mark (#503). */
function rowLook(state: StepState): RowLook {
  const p = present(state)
  const look = stepLook(state)
  return {
    tone: p.tone,
    word: p.word,
    title: p.title,
    mark: look.kind === 'unknown' ? 'unknown' : look.skipped === true ? 'skipped' : look.mark,
    hue: look.kind === 'unknown' ? 'warn' : look.hue,
  }
}






function StepNode({
  step,
  state,
  inputs,
  note,
  noteId,
  picked,
  onPick,
  workflow,
  now,
  enteredAt,
  usage,
  tier,
  x,
  y,
  w,
  h,
  reload,
  onHover,
}: {
  step: WorkflowStep
  state: StepState
  /** What this step declared from each parent and whether it arrived. */
  inputs: StepInputs
  /**
   * The line under the state (#105, #106): a failure's first error line, or
   * why the step is not running. `layoutOf` was told about it (`noted`), so
   * the card's height already counts it. Null draws nothing.
   */
  note: StepWhy | null
  /** The id of the whole note, which the card names as its description. */
  noteId: string
  /** The inspector is on this step. */
  picked: boolean
  /** Put this step in the inspector, or take it out again (WF-7). */
  onPick: (stepId: string) => void
  workflow: Workflow
  now: number
  /** When the step's task entered its state, from its events (`useEnteredAt`); null: not read. */
  enteredAt: (task: Task) => number | null
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
  /** The pointer or focus is on this card (null: it left), so the graph lights its edges. */
  onHover?: (stepId: string | null) => void
}) {
  const p = present(state)
  // THE BRAND LOOK (rebrand 2026-10-01): the node's mark and its tint, from
  // marks.tsx through `stepLook`, the one place a step's state becomes a hue.
  const look = stepLook(state)
  // SKIPPED BY ITS VERDICT GATE: no agent ran, so no figure on this card may
  // be read as the agent's run time (wide-workflows.html A).
  const skipped = look.kind === 'state' && look.skipped === true
  const dur = stepDuration(state, now, state.kind === 'state' ? enteredAt(state.task) : null)
  // THE FIGURES ARE THE TIER, so they are only computed at the tier that draws
  // them. `figuresFor` is pure and cheap, but computing four cells per node per
  // second for a canvas that is not drawing them is work done to be thrown
  // away, and on a 30-step run that is 120 cells a second.
  const showFigures = tier === 'figures'
  const f = showFigures ? stepFigures(state, usage, now) : null
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

  // THE WHOLE CARD IS ONE TARGET, AND IT PICKS THE STEP (WF-7, epic #83).
  //
  // The owner's words, which made the card one target: "The agent task node
  // should be clicable not have links inisde it if all of them point to the
  // same page or section." There were three anchors on this node and all three
  // landed on the same drawer, so they became one: the card. That target was
  // an anchor to `#work/task/<id>` -- so clicking a step on the Graph left the
  // workflow for the Work › Agents drawer, while the Timeline and the Table
  // picked the same step into the inspector. redesign-v2 §2.3 says "Selecting
  // any node in any mode fills the inspector. Nothing navigates", and the
  // owner decided for it: the card is a BUTTON that fills the inspector in
  // place, pressed while its step is the one inspected, and the inspector
  // carries `open agent →` to the drawer. A step with no task is pickable too
  // -- it has facts to show -- and its inspector offers no link, so there is
  // no dead one.
  //
  // HOW THE STOP CONTROL SURVIVES THAT, which is the constraint this shape was
  // built around: a `<button>` inside a `<button>` is invalid HTML exactly as
  // one inside an `<a>` was. So the stop control stays the card's SIBLING
  // inside `.node-slot`, absolutely positioned into a strip the card reserves
  // with padding. Nothing is nested in anything it may not be, and every
  // `title` on this card still belongs to a real ancestor of the element it
  // explains rather than sitting under a transparent overlay. That last point
  // is load-bearing: `.node-num`'s `title` is the CAUSE of an absence, and a
  // stretched overlay would have swallowed every one of them.
  //
  // The card's contents, written once. Not a component: it closes over
  // everything already computed here, and a second component would be a second
  // place to forget a fact.
  const body = (
    <>
      <div className="node-id">
        {/* The step NAME. */}
        <span className="node-name">{step.step_id}</span>
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
      {/* AT THE FIGURES TIER THE `ran` FIGURE BELOW ALREADY SAYS IT, for three
           of the five kinds. `ran 1m 8s` on this line over `ran 1m 8s` in the
           figures was one duration printed twice on one card; so were
           `running …` over `… so far`, and `never started` over `never
           started`. A WAIT is different: `queued 2m 33s` and `parked 7m 11s`
           are time spent NOT running, which the `ran` figure cannot express
           (it says `not started` or `between attempts`), so those two stay. */}
      <div className="node-line">
        <LookMark look={look} />
        <span className="node-state">{p.word}</span>
        {showFigures && (dur.kind === 'queued' || dur.kind === 'parked' || dur.kind === 'held') && <StepTime dur={dur} />}
      </div>
      {/* WHY, ON ONE LINE, AT EVERY TIER (#105, #106). A failed node's cause
          was only in the inspector, and a waiting node said `queued` with
          nothing on what it waited for. The line is the first line of the
          error or the reason, ellipsed -- an error is as unbounded as a step
          id and no width holds it -- and the WHOLE text is its title and the
          card's description, shown in full under the card on focus
          (`.node-note-full`). The inspector's error stays untruncated. Its ink
          is `whyNeedsAction`'s, the Agents list's rule: warn only for what
          asks a person to act.

          IN EVERY TIER, because a failure is never invisible and neither is
          what it failed on; `heightOf` counts the line wherever it is drawn. */}
      {note !== null && (
        <div className={`node-note is-${note.kind}${note.warn ? ' is-warn' : ''}`} title={note.full}>
          {note.text}
        </div>
      )}
      {/* The duration on a row of its own. Dropped entirely at `names`, where
          the canvas mark beside the zoom control says so -- never drawn blank,
          because an empty slot where `not started` belongs is the exact
          confusion between "no measurement" and "no problem" that this screen
          exists to prevent. */}
      {tier === 'details' && (skipped ? <div className="node-dur is-skip">agent not run</div> : <StepTime dur={dur} />)}
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
          {skipped ? (
            <NodeNum
              label="agent"
              cell={{ kind: 'absent', text: 'not run', note: 'The verdict gate kept this step’s agent from running; the attempt only published the reviewed work, so it has no run time.' }}
              pending={false}
            />
          ) : (
            <NodeNum label="ran" cell={f.ran} pending={false} />
          )}
          <NodeNum label="cost" cell={f.cost} pending={pending} />
          <NodeNum label="tokens" cell={f.tokens} pending={pending} />
          <NodeNum label="ckpts" cell={f.checkpoints} pending={pending} />
        </dl>
      )}
      {f !== null && <NodeSource f={f} pending={pending} />}

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
      {/* A DEPENDENCY THAT STAGES A FILE NAMES IT: `↑ plan (plan.md)`. The
          file carries its own arrival state -- solid when a result reported it
          staged, a dashed rule when nothing has (the absence mark), with which
          kind of not-yet on its title.

          DRAWN AS UNITS THE LINE MAY MOVE BUT NOT SPLIT (WF-19). Every parent
          id and every `(file)` is one `.node-dep-item`, an inline-block, with
          the separating comma inside the unit it follows; a plain space goes
          between units. The line wrapped at hyphens -- `check-` / `3`, `(cc-` /
          `checkpoint.md` -- and a split id is two things to rejoin and a copied
          half is not an id. A unit longer than a whole line still wraps inside
          its own box, so nothing is truncated. The list is `depUnits`, which is
          also what `heightOf` packs, so the line and the box it is measured
          into break in the same places. */}
      {step.depends_on.length > 0 && (
        <div className="node-dep" title={`depends on ${step.depends_on.join(', ')}`}>
          ↑{' '}
          {depUnits(step).map((u, i) => (
            <Fragment key={`${u.kind}:${u.parent}`}>
              {i > 0 ? ' ' : ''}
              <span className="node-dep-item">
                {u.file === null ? (
                  u.text
                ) : (
                  <>
                    <DepFile file={u.file} provenance={inputs.declared.get(u.parent) ?? null} />
                    {u.text.endsWith(',') ? ',' : ''}
                  </>
                )}
              </span>
            </Fragment>
          ))}
        </div>
      )}
    </>
  )

  return (
    <div className="node-slot" style={{ left: x, top: y, width: w, height: h }}>
      <button
        type="button"
        className={`node ${p.tone} ${lookClass(look)} zoom-${tier}${picked ? ' is-picked' : ''}${skipped ? ' is-skipped' : ''}${state.kind === 'state' && TERMINAL_STATES.has(state.task.state) ? ' is-settled' : ''}`}
        data-step={step.step_id}
        title={p.title}
        aria-pressed={picked}
        aria-describedby={note === null ? undefined : noteId}
        onClick={() => onPick(step.step_id)}
        onMouseEnter={onHover === undefined ? undefined : () => onHover(step.step_id)}
        onMouseLeave={onHover === undefined ? undefined : () => onHover(null)}
        onFocus={onHover === undefined ? undefined : () => onHover(step.step_id)}
        onBlur={onHover === undefined ? undefined : () => onHover(null)}
      >
        {body}
      </button>
      {/* THE WHOLE NOTE, for focus: hidden until the card is focused, then
          laid over the canvas under the card rather than inside it, so it
          grows nothing `layoutOf` measured. It is the card's description
          either way, so a screen reader hears all of it on focus.

          A SIBLING OF THE CARD, NOT A CHILD OF IT: inside the `<button>` it
          was part of the button's name from content once shown, and the
          error was read twice, once as the name and once as the description. */}
      {note !== null && (
        <span className="node-note-full" id={noteId}>
          {note.full}
        </span>
      )}
      {/* B28, on the node. Only when the step's TASK was actually joined: a
          step whose state is `unknown` was not in the task read, and offering
          to stop something this screen could not read would be acting on a
          guess. `StopRun` then decides for itself whether the state is one the
          cancel route accepts, so a terminal node draws nothing at all.

          A SIBLING OF THE CARD, NOT A CHILD OF IT. The card is a `<button>`
          (WF-7) and a button may not live inside one. It sits in the strip `.node`
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

/** The key's three entries, in the order an edge gains weight: its word and its sentence. */
const EDGE_KEY: readonly { kind: EdgeKind; word: string; title: string }[] = [
  { kind: 'order', word: 'order', title: 'Thin: an order dependency. The lower step waits for the upper one; no file passes.' },
  {
    kind: 'staged',
    word: 'staged',
    title: "Heavy, solid: a file staged. The upper step's file was reported staged into the lower step's workspace.",
  },
  {
    kind: 'declared',
    word: 'declared',
    title: 'Heavy, dashed: a file declared and not yet reported staged. The dash means not a measurement.',
  },
]

/**
 * THE GRAPH'S KEY (#108). The canvas draws three kinds of edge and its SVG is
 * `aria-hidden`, so nothing on screen named them. Each entry is a short edge
 * drawn with THE CANVAS'S OWN CLASSES -- `linkClass` and `.wf-edge` -- so the
 * key cannot come to disagree with what it explains; the word is the label
 * and the hover is the sentence. Weight and dash, not hue, as on the canvas.
 *
 * It sits at the end of the zoom strip, which is drawn whenever the graph has
 * an edge even when there is nothing to zoom: a chain needs its key as much
 * as a fan-out does. Chrome, not prose (§6.11): three words.
 */
function EdgeKey() {
  return (
    <ul className="wf-key" aria-label="Edge key">
      {EDGE_KEY.map((k) => (
        <li key={k.kind} className="wf-key-item" title={k.title}>
          <svg className="wf-key-sample" width="24" height="8" viewBox="0 0 24 8" aria-hidden="true" focusable="false">
            <g className={linkClass(k.kind)}>
              <path className="wf-edge" d="M1 4 H23" />
            </g>
          </svg>
          <span className="wf-key-word">{k.word}</span>
        </li>
      ))}
    </ul>
  )
}

/** The edge group's classes: its kind, and for a data edge whether it arrived. */
function linkClass(kind: EdgeKind): string {
  return kind === 'order' ? 'wf-link is-order' : `wf-link is-data is-${kind}`
}

/**
 * The `(plan.md)` on a dependency line, carrying whether the file arrived.
 *
 * The WORD on the line is the same either way -- the filename, which is what
 * the step declared and what `heightOf` measured -- and the arrival is the
 * treatment: solid for a reported staging, the dashed absence rule for every
 * kind of not-yet. The title is the precise answer, because four different
 * not-yets send a reader to four different places.
 */
function DepFile({ file, provenance }: { file: string; provenance: EdgeProvenance | null }) {
  if (provenance === null || provenance.kind === 'order') {
    return <span className="node-dep-file">({file})</span>
  }
  const title =
    provenance.kind === 'staged'
      ? `Staged into this step's workspace: ${provenance.file}, ${
          provenance.bytes === null ? 'size not reported' : bytesLabel(provenance.bytes)
        }${provenance.fromCheckpoint ? ', already in the restored checkpoint' : ''}.`
      : `${DECLARED_WORDS[provenance.why].text}: ${DECLARED_WORDS[provenance.why].note}`
  return (
    <span className={`node-dep-file is-${provenance.kind}`} title={title}>
      ({file})
    </span>
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

/**
 * The attempt figures a total sums, once the read has landed; null before it
 * has or when it failed, which `workflowSpend` reads as "results only". Every
 * total on a surface goes through this with the surface's one read (WF-5).
 */
function telemetryOf(usage: UsageRead): ReadonlyMap<string, StepUsage> | null {
  return usage.kind === 'ready' ? (usage.usage?.byTaskId ?? null) : null
}


/** Every figure one node shows, and what to say where there is none. */
interface StepFigures {
  ran: Cell
  cost: Cell
  tokens: Cell
  checkpoints: Cell
  /**
   * `result` where `cost` / `tokens` is the step's result summary's figure
   * rather than its attempts' (WF-5). Every view marks such a figure
   * `from result`; null for a telemetry figure and for an absence.
   */
  costFrom: 'result' | null
  tokensFrom: 'result' | null
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
 * parked" there was no click that reached `draft`. It became an `<a>` to
 * `#work/task/<id>`, and since WF-7 it is a button that picks the step into the
 * inspector, whose `open agent →` is that same anchor -- the route App.tsx
 * already resolves to the full agent run: runtime environment, attempts,
 * spend, duration, logs, checkpoints, artifacts and outputs. An anchor rather
 * than a click handler on purpose: it is middle-clickable, copyable, and
 * reachable by keyboard without this file reimplementing any of that.
 *
 * A step with NO TASK gets no link, and says why. A dead link to a task that
 * does not exist would be the same defect one level down.
 *
 * A step whose task id we hold but whose task the read did not return DOES get
 * the link: the task exists, the run page fetches it by id, and the fact that
 * this board's page of 200 tasks did not include it says nothing about whether
 * the run can be opened.
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
    // TERMINAL WITH NO START: it never got to STARTING, and the word is the one
    // the node, the timeline and the inspector print (`NEVER_STARTED_WORD`).
    // "not started" here read as "not yet" about a step that is over.
    if (terminal) {
      return absentCell({
        text: NEVER_STARTED_WORD,
        note: `This step is ${task.state.toLowerCase()} and never started: started_at is written on DISPATCHED → STARTING, and it never got that far.`,
      })
    }
    return absentCell({
      text: 'not started',
      note: Number.isFinite(created)
        ? `started_at is written on DISPATCHED → STARTING. This step has not begun; it was submitted ${durationText(now - created)} ago.`
        : 'started_at is written on DISPATCHED → STARTING. This step has not begun.',
    })
  }
  if (Number.isFinite(completed)) {
    // WHICH TIMESTAMPS, AND THE OTHER FIGURE THEY ARE NOT (WF-21). The task's
    // `started_at` is rewritten at each attempt's DISPATCHED -> STARTING
    // (control.py:794) and `completed_at` is written at the end
    // (control.py:1073); the inspector's `took` reads one attempt's own
    // timestamps instead (control.py:816, :930). Separate utcnow() reads, so
    // the two can differ by about a second. This said "and any park", which
    // was false: a park ENDS the attempt and the next start rewrites
    // started_at, which is why the Timeline counts earlier attempts as waiting.
    return measuredCell(
      durationText(completed - started),
      'From this task’s latest started_at, rewritten at each attempt’s start, to its completed_at. The inspector’s “took” times one attempt from its own start and end instead -- separate writes, so for a single attempt the two can differ by about a second.',
    )
  }
  if (terminal) {
    return absentCell(FINISH_NOT_RECORDED)
  }
  // A START THAT BELONGS TO AN ATTEMPT THAT IS OVER. `started_at` is written on
  // DISPATCHED -> STARTING and overwritten per attempt (control.py:410-413); it
  // is not cleared when an attempt parks or is reclaimed. So a task that is
  // parked, queued, leased or dispatched AND carries a start time ran once and
  // is waiting for its next attempt -- and "7m 30s so far" for it said a step
  // holding nothing had been running for seven minutes. The timeline and the
  // table draw that span as waiting; this figure agrees with them.
  if (!RUNNING_NOW.has(task.state)) {
    return absentCell({
      text: 'between attempts',
      note: `This task is ${task.state.toLowerCase()} with ${task.attempt_count} of ${task.max_attempts} attempts used. Its start time belongs to an attempt that is over, so there is no run in progress to time.`,
    })
  }
  return measuredCell(`${durationText(now - started)} so far`, 'Still running. This figure moves.')
}

/** The only two states in which the time since `started_at` is time run. */
const RUNNING_NOW: ReadonlySet<TaskState> = new Set<TaskState>(['STARTING', 'RUNNING'])

/**
 * The four figures, for one step.
 *
 * FOUR DIFFERENT ABSENCES, and the whole value of this function is keeping them
 * apart. A step with no task has nothing to measure; a step whose task was not
 * in the task read has figures nobody fetched; a step outside the attempt
 * sample has figures this board chose not to fetch; a step whose attempt read
 * failed has figures that could not be fetched. One "—" for all four sends an
 * operator to four different places at random.
 *
 * AND A SECOND SOURCE FOR TWO OF THEM (WF-5, epic #83). Where the attempt
 * telemetry has no cost or no tokens for a step -- outside the sample, a read
 * that failed, or attempts that carry no typed figure -- the step's result
 * summary may still have reported one, and the row's total was already
 * counting it. That figure is shown, and marked `from result` wherever it is
 * drawn, because the result describes only the attempt that finished and the
 * telemetry sums every attempt: two records, not one. Only where NEITHER has a
 * figure does the absence word stand, and it is the word for why the
 * telemetry is missing. Checkpoints have no second source.
 *
 * TWO RULES THE FIRST VERSION OF THIS BROKE (#160 review). The result is
 * borrowed only for a FINISHED task (`finishedResultOf`): a step running its
 * second attempt still carries its failed first attempt's result, and the
 * inspector never offered that one. And the figure's note says what THIS
 * board read (`boardResultNote`): outside the sample, or where the read failed,
 * the board read no attempt document, so it may not say what one carries.
 */
function stepFigures(state: StepState, usage: UsageRead, now: number): StepFigures {
  const f = figuresFor(state, usage, now)
  if (state.kind !== 'state' || usage.kind === 'reading') return f
  // A KNOWN $0 IS NOT AN ABSENCE (QA G3-02, G3-03, G3-10). A step whose
  // verdict gate skipped its agent, or that never had an attempt, spent
  // nothing -- the same rule the row's total leaves out of its coverage
  // (`noAgentSpend`) -- so its cost and tokens say `none`, never `not
  // reported` or a clipped `no attempt …`. A figure either record DID carry
  // is kept: this only replaces an absence.
  const none = noAgentSpend(state.task, telemetryOf(usage)?.get(state.task.id))
  if (none === null) return f
  const cell = noSpendCell(none, state.task)
  return {
    ...f,
    cost: f.cost.kind === 'absent' ? cell : f.cost,
    tokens: f.tokens.kind === 'absent' ? cell : f.tokens,
  }
}

/** The cost and tokens cell of a step that spent nothing on an agent. */
function noSpendCell(none: NoSpend, task: Task): Cell {
  if (none === 'skipped') {
    return measuredCell(
      'none',
      `${SKIPPED_WORD}: the verdict gate kept this step’s agent from running, so it spent nothing on an agent. A known $0, not a missing figure.`,
    )
  }
  if (TERMINAL_STATES.has(task.state)) {
    return measuredCell('none', 'This step ended without any attempt running its agent, so it spent nothing. A known $0, not a missing figure.')
  }
  return measuredCell('none yet', 'No attempt has run under this step yet, so it has spent nothing so far.')
}

function figuresFor(state: StepState, usage: UsageRead, now: number): StepFigures {
  const allAbsent = (a: Absence): StepFigures => ({
    ran: absentCell(a),
    cost: absentCell(a),
    tokens: absentCell(a),
    checkpoints: absentCell(a),
    costFrom: null,
    tokensFrom: null,
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
    return {
      ran,
      cost: absentCell(pending),
      tokens: absentCell(pending),
      checkpoints: absentCell(pending),
      costFrom: null,
      tokensFrom: null,
      why: null,
    }
  }

  // THE RESULT'S FIGURES, for wherever the telemetry has none -- and only once
  // the task has finished, which is when the result is its newest attempt's.
  // `gap` is why the telemetry has none, and the note says so.
  const result = finishedResultOf(state.task)
  // BEFORE THE RESULT, the API's total over every attempt (lane review P1):
  // the result is the last attempt's alone, and a retried step drawn from it
  // under-reported every earlier attempt. Not marked `from result` -- it is
  // the attempts' sum, as the telemetry is -- and the last attempt's figure
  // is its secondary text (`totalCostCell`).
  const served = totalCostCell(state.task)
  const costOrResult = (own: Cell, gap: BoardTelemetryGap): { cell: Cell; from: 'result' | null } =>
    own.kind === 'absent' && served !== null
      ? { cell: served, from: null }
      : own.kind === 'absent' && result !== null && result.usd !== null
        ? { cell: costCell(result.usd, boardResultNote(gap)), from: 'result' }
        : { cell: own, from: null }
  const tokensOrResult = (own: Cell, gap: BoardTelemetryGap): { cell: Cell; from: 'result' | null } =>
    own.kind === 'absent' && result !== null && hasTokenKind(result)
      ? { cell: tokenKindsCell(resultTokenKinds(result), boardResultNote(gap)), from: 'result' }
      : { cell: own, from: null }

  const absentUsage = (a: Absence, gap: BoardTelemetryGap): StepFigures => {
    const cost = costOrResult(absentCell(a), gap)
    const tokens = tokensOrResult(absentCell(a), gap)
    return {
      ran,
      cost: cost.cell,
      tokens: tokens.cell,
      checkpoints: absentCell(a),
      costFrom: cost.from,
      tokensFrom: tokens.from,
      why: a.note,
    }
  }

  if (usage.kind === 'failed') {
    return absentUsage({ text: USAGE_NOT_READ.text, note: `${USAGE_NOT_READ.note} (${usage.detail})` }, 'not-read')
  }
  if (usage.usage === null) return absentUsage(USAGE_NOT_SAMPLED, 'not-sampled')

  const failed = usage.usage.failed.get(taskId)
  if (failed !== undefined) {
    return absentUsage({ text: USAGE_NOT_READ.text, note: `${USAGE_NOT_READ.note} (${failed})` }, 'not-read')
  }
  const u = usage.usage.byTaskId.get(taskId)
  if (u === undefined) return absentUsage(USAGE_NOT_SAMPLED, 'not-sampled')
  // The read succeeded and there is nothing to sum. A FOURTH thing, and not
  // "the runner reported no cost": nothing has run.
  if (u.attempts === 0) return absentUsage(NO_ATTEMPT_YET, 'no-attempt')

  // Read, and summed: where it has no figure, no attempt document carried one,
  // which is the inspector's own note and true here for the same reason.
  const cost = costOrResult(
    costCell(
      u.costUsd,
      `Summed over ${u.attemptsWithCost} of ${u.attempts} attempt${u.attempts === 1 ? '' : 's'} that reported one. Token cost only — no infrastructure cost is recorded anywhere.`,
    ),
    'untyped',
  )
  const tokens = tokensOrResult(tokensOf(u), 'untyped')
  return {
    ran,
    cost: cost.cell,
    costFrom: cost.from,
    tokens: tokens.cell,
    tokensFrom: tokens.from,
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
 * A step's tokens as one cell: the total of all four kinds its attempts
 * reported, each kind's sum in the note (#322, `tokenKindsCell`).
 *
 * EACH KIND SUMS SEPARATELY (`rollUpAttempts`), so a step whose runner
 * reported input and no output shows the kinds it has and names them --
 * `(input ?? 0) + (output ?? 0)` counts the missing kind as a zero, which is
 * the same defect `AgentDetail.tsx` records having already been fixed once at
 * the tile level.
 */
function tokensOf(u: StepUsage): Cell {
  return tokenKindsCell(
    { input: u.inputTokens, output: u.outputTokens, cacheRead: u.cacheReadTokens, cacheWrite: u.cacheCreationTokens },
    `Summed over ${u.attemptsWithTokens} of ${u.attempts} attempt${u.attempts === 1 ? '' : 's'} that reported tokens.`,
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
      {/* THE FIGURE AND NOTHING ELSE, in a column budgeted for exactly that.
          Where it came from is `.node-src`, the line under the four (WF-5);
          sharing this column, the note ellipsed to `…` beside a token figure
          and the figure itself was clipped past 173px. */}
      <dd>{pending ? <span className="node-reading" aria-label="reading" /> : cell.text}</dd>
    </div>
  )
}

/**
 * WHICH OF THE NODE'S FIGURES ARE THE STEP'S RESULT'S (WF-5): `cost · tokens
 * from result`, on a line of its own under the four.
 *
 * IT WAS BESIDE EACH FIGURE, in the figure's own column, and that column has
 * room for a 20-character figure and nothing else (#160 review): beside `21.4k
 * in · 3.2k out` the note showed as `…`, and a wider figure was cut with no
 * mark. Widening every Figures-tier node by the note's 84px would take
 * `STAGE_FITS` from 3 to 2 and send a three-wide stage to `details`, where no
 * figure is drawn at all. So the note has a row, which names the figures it is
 * about -- the labels on their own rows above -- and the words are the one
 * spelling the table and the inspector print beside theirs (`SourceNote`).
 *
 * RENDERED ON EVERY FIGURES-TIER NODE, EMPTY WHEN NOTHING IS BORROWED, and
 * `nodeHeightAt('figures')` counts it. Whether a figure is the result's is
 * known only when the attempt read lands, and a card that grew a row then
 * would push every level beneath it down the page. Silent while that read is
 * in flight: a placeholder makes no claim, so there is nothing to source.
 */
function NodeSource({ f, pending }: { f: StepFigures; pending: boolean }) {
  const sourced = pending
    ? []
    : [
        f.costFrom === 'result' && f.cost.kind === 'measured' ? 'cost' : null,
        f.tokensFrom === 'result' && f.tokens.kind === 'measured' ? 'tokens' : null,
      ].filter((x): x is string => x !== null)
  return (
    <div className="node-src">
      {sourced.length > 0 && (
        <>
          {sourced.join(' · ')} <SourceNote />
        </>
      )}
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
function SampleNote({ usage, workflow }: { usage: WorkflowUsage; workflow: Workflow }) {
  // THIS WORKFLOW'S TASKS ONLY (QA G3-05). The read is the board's -- every
  // workflow's tasks, this one's first under the ceiling -- and the mark
  // printed `12/296 sampled` over a three-step table whose three steps all
  // had figures. It qualifies THESE figures, so it counts these tasks, and it
  // is silent when every one of them was read.
  const ids = [...new Set(workflow.steps.flatMap((st) => (st.task_id ? [st.task_id] : [])))]
  const sampled = ids.filter((id) => usage.byTaskId.has(id)).length
  const uncovered = ids.filter((id) => usage.notSampled.has(id)).length
  const failed = ids.filter((id) => usage.failed.has(id)).length
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
        aria-label={`Step figures cover ${sampled} of this workflow’s ${ids.length} tasks.${ceiling}${unread}`}
      >
        {sampled}/{ids.length} sampled
      </span>
    </span>
  )
}

// ===========================================================================
// WORKFLOWS V2: the list and one workflow's page (owner's pick, 2026-10-01;
// workflows.html sections A, B and F)
// ===========================================================================

/**
 * A WORKFLOW'S STATE MARK: the brand mark of its derived state, or -- when this
 * read derived none -- the amber ring with the header's own words (`state
 * unread`, `state not derived`). Amber is the warning colour, never a state.
 */
function WorkflowStateMark({ workflow }: { workflow: Workflow }) {
  const s = derivedStateOf(workflow)
  if (s !== null) return <StateMark state={s} />
  const h = workflowHeaderState(workflow)
  return <NamedMark mark="queued" dataMark="unknown" hue="warn" word={h.word} title={h.title} />
}

/**
 * THE LIST'S COLUMNS, named once, so the loading skeleton draws the head the
 * loaded table will (#113) and the first row lands where the skeleton's was.
 */
const LIST_COLUMNS: readonly { col: string; label: string; num?: boolean }[] = [
  { col: 'state', label: 'State' },
  { col: 'workflow', label: 'Workflow' },
  { col: 'shape', label: 'Shape' },
  { col: 'done', label: 'Steps done' },
  { col: 'runners', label: 'Runners' },
  { col: 'cost', label: 'Cost', num: true },
  { col: 'owner', label: 'Owner' },
  // SUBMITTED, NOT STARTED (QA G3-20, 2026-10-07): every order sorts by
  // submission, and a column of first-step starts under "newest first" read
  // 15:33:10, 15:33:09, 15:35:37. Duration is measured from submission too.
  // The first step's start is the cell's title.
  { col: 'submitted', label: 'Submitted' },
  { col: 'duration', label: 'Duration', num: true },
]

function ListHead() {
  return (
    <thead>
      <tr>
        {LIST_COLUMNS.map((c) => (
          // `data-col` is what styles/workflows.css gives each column its share of the width by.
          <th key={c.label} data-col={c.col} className={c.num === true ? 'num' : undefined}>
            {c.label}
          </th>
        ))}
      </tr>
    </thead>
  )
}

/**
 * THE LIST'S TOOLBAR, loaded or not (#113). While the read is in flight it is
 * drawn DISABLED, with the address's choices already showing and no counts,
 * so nothing above the table moves when the data lands -- it used to appear
 * only then and push the rows down by its own height.
 */
function WorkflowListFilters({
  query,
  choose,
  counts,
  owners,
  profiles,
}: {
  query: WorkflowQuery
  choose: (q: WorkflowQuery) => void
  /** Null while loading: no count is drawn rather than a zero nobody read. */
  counts: Record<BucketFilter, number> | null
  owners: readonly string[]
  profiles: readonly string[]
}) {
  const off = counts === null
  return (
    <div className="wfl-filters">
      <Segmented
        className="wfl-seg"
        label="Which workflows"
        disabled={off}
        value={query.state}
        options={BUCKET_FILTERS.map((b) => ({ key: b, label: BUCKET_LABEL[b], ...(counts !== null ? { count: counts[b] } : {}) }))}
        onChange={(b) => choose({ ...query, state: b })}
      />
      <input
        className="wfl-search"
        type="search"
        aria-label="Find a workflow or step"
        placeholder="Find a workflow or step"
        disabled={off}
        value={query.q}
        onChange={(e) => choose({ ...query, q: e.target.value })}
      />
      <label className="wfl-pick">
        owner
        <select disabled={off} value={query.owner} onChange={(e) => choose({ ...query, owner: e.target.value })}>
          <option value="">anyone</option>
          {/* The name before the @, as the Owner cell prints it (QA G3-14): a
              service account's whole address set the select 424px wide in
              a 354px column at 390. The address is the option's title. */}
          {owners.map((o) => (
            <option key={o} value={o} title={o}>
              {ownerShort(o)}
            </option>
          ))}
        </select>
      </label>
      <label className="wfl-pick">
        profile
        <select disabled={off} value={query.profile} onChange={(e) => choose({ ...query, profile: e.target.value })}>
          <option value="">any</option>
          {profiles.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
      </label>
      {/* THE ORDER (#111): by state as before, by age, or by how many steps
          failed -- the triage order. In the address like every other choice. */}
      <label className="wfl-pick">
        sort
        <select
          disabled={off}
          value={query.sort}
          onChange={(e) => choose({ ...query, sort: e.target.value as WorkflowSort })}
        >
          {WORKFLOW_SORTS.map((o) => (
            <option key={o} value={o}>
              {o === 'state' ? 'by state' : SORT_LABEL[o]}
            </option>
          ))}
        </select>
      </label>
    </div>
  )
}

/** How many rows the loading skeleton draws: about one laptop fold of the list. */
const SKELETON_ROWS = 6
/** Each skeleton cell's bar width, in em, per column; the name cell draws two lines as a row does. */
const SKELETON_WIDTHS: readonly number[] = [5, 12, 4, 7, 6, 4, 6, 5, 4]

/**
 * THE LIST WHILE ITS READ IS IN FLIGHT (#113): the real toolbar, disabled, the
 * real table head, and skeleton rows in the table's own cells -- the same
 * `.wfl-row` and padding, the name cell two lines tall as a loaded row's is --
 * so the first loaded row lands where the first skeleton row was. The bars are
 * `.wfl-skel`, a step darker than the shared `.skeleton`, which sat at about
 * 1.03:1 in light. They carry no text: a skeleton answers nothing.
 */
export function WorkflowListSkeleton({ query }: { query: WorkflowQuery }) {
  const owners = query.owner === '' ? [] : [query.owner]
  const profiles = query.profile === '' ? [] : [query.profile]
  return (
    <div className="wfl is-loading" aria-busy="true">
      <WorkflowListFilters query={query} choose={() => {}} counts={null} owners={owners} profiles={profiles} />
      <div className="table-wrap is-scroll wfl-scroll" aria-hidden="true">
        <table className="wfl-table">
          <ListHead />
          <tbody>
            {Array.from({ length: SKELETON_ROWS }, (_, r) => (
              <tr key={r} className="wfl-row is-skel">
                {SKELETON_WIDTHS.map((w, c) => (
                  <td
                    key={c}
                    data-col={LIST_COLUMNS[c]?.col}
                    className={c === 1 ? 'wfl-name' : LIST_COLUMNS[c]?.num === true ? 'num' : undefined}
                  >
                    <span className="wfl-skel" style={{ width: `${w}em` }} />
                    {c === 1 && <span className="wfl-skel is-sub" style={{ width: `${w - 4}em` }} />}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/**
 * THE LIST, FULL WIDTH (workflows.html A). One row per workflow: the rollup
 * mark, the name, the shape, steps done, the runner mix, cost, owner, when it
 * started and how long it ran (#111). A failed row's second line names the
 * failing step and why; a parked one what it waits on; an unread one says so.
 * Everything chosen above the table, the order included, is in the address
 * (`workflowlist.ts`).
 */
function WorkflowList({
  board,
  query,
  choose,
}: {
  board: WorkflowBoard
  query: WorkflowQuery
  choose: (q: WorkflowQuery) => void
}) {
  const reads = useRowReads(board)
  const taskById = reads.taskById
  const labels = useMemo(
    () => new Map(board.workflows.map((w) => [w.workflow_id, workflowLabel(w, taskById)] as const)),
    [board.workflows, taskById],
  )
  // The segment's counts follow the OTHER filters, so "Failed 2" means two
  // failed workflows of priya's, when owner is priya.
  const filtered = board.workflows.filter((w) => matchesFilters(w, labels.get(w.workflow_id) ?? null, query))
  const counts = bucketCounts(filtered)
  const rows = sortWorkflows(
    query.state === 'all' ? filtered : filtered.filter((w) => bucketOf(w) === query.state),
    query.sort,
    taskById,
  )
  // HOW MANY ROWS KNOW THEIR STEPS (QA G3-06), said once above the table
  // rather than as a dash in every cell of every row the window missed.
  const unread = rows.filter((w) => !stepsRead(w, taskById))
  const failedReads = unread.filter((w) => reads.failed.has(w.workflow_id)).length
  // A filter value the read no longer holds stays offered, so the control
  // never silently shows "anyone" while the list is still filtered by one.
  const owners = withChosen(ownersOf(board.workflows), query.owner)
  const profiles = withChosen(profilesOf(board.workflows), query.profile)
  const filteredAtAll = query.q !== '' || query.owner !== '' || query.profile !== ''
  // THE ROWS' COST, BY THE TABLE'S RULE (WF-5): attempt telemetry where the
  // read carried a cost, the step's result where it did not. Read for the rows
  // drawn, in their order, so the cap covers the top of the list; a step
  // outside the cap totals from its result and its row says how many did.
  const telemetry = telemetryOf(useWorkflowUsage(rows, null))
  // One clock for every running row's `so far`, on the shared age tick: the
  // started and duration columns move on the same beat as every other age on
  // the screen, and a running row's duration advances between polls. The
  // canvas's own 1Hz `useNow` above is scoped to an open card; holding it
  // here would re-render every row once a second.
  const now = useSharedClock(AGE_TICK_MS)

  // RECENT (5) BEFORE ANYTHING WAS OPENED (#503): the list read's five newest,
  // by name, with the state this read derived, fill the panel's switcher
  // behind the workflows opened in this browser. The WHOLE read is offered, so
  // an opened workflow outside the newest five is refreshed too (QA G3-04).
  // No read of its own.
  useEffect(() => {
    const newest = [...board.workflows].sort((a, b) => (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0))
    offerNewestWorkflows(
      newest.map((w) => ({ id: w.workflow_id, state: derivedStateOf(w), name: labels.get(w.workflow_id) ?? null })),
    )
  }, [board.workflows, labels])

  return (
    <div className="wfl">
      <WorkflowListFilters query={query} choose={choose} counts={counts} owners={owners} profiles={profiles} />
      {unread.length > 0 && (
        <CountNote>
          {rows.length - unread.length} of {rows.length} rows have their steps read; older rows show ids only until they
          scroll into view{failedReads > 0 ? `; ${failedReads} could not be read` : ''}
        </CountNote>
      )}
      {rows.length === 0 ? (
        <p className="wfl-none">
          {/* WHAT WAS SEARCHED (QA G3-21): the filters run over the rows this
              read holds, the newest 100, so "no match" is never a claim about
              older workflows. */}
          {filteredAtAll
            ? `No ${query.state === 'all' ? '' : `${BUCKET_LABEL[query.state].toLowerCase()} `}match among the ${board.workflows.length} newest workflows read; older ones are not searched.`
            : `No ${query.state === 'all' ? '' : `${BUCKET_LABEL[query.state].toLowerCase()} `}workflow in this read.`}
          {filteredAtAll && (
            <Button size="sm" onClick={() => choose({ ...query, q: '', owner: '', profile: '' })}>
              Clear the filters
            </Button>
          )}
        </p>
      ) : (
        <div className="table-wrap is-scroll wfl-scroll">
          <table className="wfl-table">
            <ListHead />
            <tbody>
              {rows.map((w) => (
                <WorkflowListRow
                  key={w.workflow_id}
                  workflow={w}
                  label={labels.get(w.workflow_id) ?? null}
                  taskById={taskById}
                  telemetry={telemetry}
                  query={query}
                  now={now}
                  onSeen={stepsRead(w, board.taskById) ? null : reads.want}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="wfl-foot">
        {rows.length} of {board.workflows.length} read · {SORT_LABEL[query.sort]} · 100 per read
      </p>
    </div>
  )
}

/**
 * ONE ROW'S STEPS, READ AS IT SCROLLS IN (QA G3-06, 2026-10-07). The board
 * joins states through a window of the tenant's newest tasks, so 55 of 100
 * rows read `wf_… · — · start not read · not read` while the workflow's own
 * page showed its title, start and $0.62. A row the window missed reads
 * `GET /v1/workflows/{id}` -- the page's own read, so the row and the page
 * agree, cost totals included -- once it is near the viewport, and its tasks
 * join the board's. The window's copy wins where both hold a task: it is the
 * fresher read. A finished row reads once; a running one again on each board
 * read (each read is new `Workflow` objects), so its cost does not freeze at
 * the first look.
 */
function useRowReads(board: WorkflowBoard): {
  taskById: ReadonlyMap<string, Task> | null
  failed: ReadonlySet<string>
  want: (w: Workflow) => void
} {
  const [extra, setExtra] = useState<ReadonlyMap<string, Task>>(() => new Map())
  const [failed, setFailed] = useState<ReadonlySet<string>>(() => new Set())
  const askedIds = useRef(new Set<string>())
  const askedReads = useRef(new WeakSet<Workflow>())
  const live = useRef(true)
  useEffect(() => {
    live.current = true
    return () => {
      live.current = false
    }
  }, [])
  const want = useCallback((w: Workflow) => {
    const id = w.workflow_id
    if (bucketOf(w) === 'running') {
      if (askedReads.current.has(w)) return
      askedReads.current.add(w)
    } else {
      if (askedIds.current.has(id)) return
      askedIds.current.add(id)
    }
    void loadWorkflow(id).then((r) => {
      if (!live.current) return
      if (r.status === 'ok' || r.status === 'stale') {
        setExtra((m) => {
          const next = new Map(m)
          for (const t of r.data.tasks) next.set(t.id, t)
          return next
        })
        setFailed((f) => {
          if (!f.has(id)) return f
          const next = new Set(f)
          next.delete(id)
          return next
        })
      } else {
        setFailed((f) => (f.has(id) ? f : new Set(f).add(id)))
      }
    })
  }, [])
  const taskById = useMemo(() => {
    if (extra.size === 0) return board.taskById
    const out = new Map(extra)
    for (const [k, t] of board.taskById ?? []) out.set(k, t)
    return out
  }, [extra, board.taskById])
  return { taskById, failed, want }
}

function withChosen(options: string[], chosen: string): string[] {
  return chosen === '' || options.includes(chosen) ? options : [...options, chosen].sort()
}

function WorkflowListRow({
  workflow,
  label,
  taskById,
  telemetry,
  query,
  now,
  onSeen,
}: {
  workflow: Workflow
  label: string | null
  taskById: ReadonlyMap<string, Task> | null
  /** The list's attempt read (`useWorkflowUsage`): the row totals by the table's rule (WF-5). */
  telemetry: ReadonlyMap<string, StepUsage> | null
  query: WorkflowQuery
  now: number
  /** Set when the board's window missed this row's steps: read them once it is in view (G3-06). */
  onSeen: ((w: Workflow) => void) | null
}) {
  const [seenRef, inView] = useInView<HTMLTableRowElement>()
  useEffect(() => {
    if (onSeen !== null && inView) onSeen(workflow)
  }, [onSeen, inView, workflow])
  const why = rowWhy(workflow, taskById)
  const roll = rollupLine(workflow, taskById)
  const spend = workflowSpend(workflow.steps, taskById, telemetry)
  const failed = why !== null && (why.kind === 'failed' || why.kind === 'failed-unread')
  // SUBMITTED is when the workflow was submitted, the instant every order
  // sorts by (QA G3-20), with the first step's start in its hover -- the
  // same words the open card's head uses (#376). DURATION is wall clock from
  // submission (`workflowDuration`), so the two columns share one origin.
  const started = workflowStartText(workflow, taskById, now)
  const dur = workflowDuration(workflow, taskById, now)
  return (
    <tr ref={seenRef} className={`wfl-row${failed ? ' is-failed' : ''}`} data-workflow={workflow.workflow_id}>
      {/* EVERY CELL NAMES ITS COLUMN (browser QA D8): the sheet sizes State,
          Steps done and Duration to what they hold, and cuts the name and the
          shape with their whole text as the title. */}
      <td className="wfl-state" data-col="state">
        <WorkflowStateMark workflow={workflow} />
      </td>
      <td className="wfl-name" data-col="workflow">
        <a href={workflowHref(workflow.workflow_id, query)} title={label ?? workflow.workflow_id}>
          {label ?? workflow.workflow_id}
        </a>
        {label !== null && <Id title={workflow.workflow_id}>{workflow.workflow_id}</Id>}
        {/* Drawn at ≤560 only, where the done, failed and age columns are
            off the right edge (#109); the cells say it wide, so hidden from
            assistive technology to keep one reading of each fact. */}
        <span className="wfl-phone" aria-hidden>
          {phoneSummary(workflow, now)}
        </span>
        {/* THE RUNNER MIX ON THE NAME'S SECOND LINE below 1680 (QA G3-09),
            where the Runners and Shape columns give the name their width;
            hidden where the column draws it, so it is read once. */}
        <span className="wfl-mix">
          <Mix steps={workflow.steps} />
        </span>
        {why !== null && <RowWhyLine why={why} />}
        {cancelPending(workflow) && <span className="tag wait">cancel requested</span>}
      </td>
      <td className="wfl-shape" data-col="shape" title={shapeOf(workflow.steps).label}>
        <StageGlyph steps={workflow.steps} taskById={taskById} />
        <Shape shape={shapeOf(workflow.steps)} />
        {partialDeps(workflow.steps) && (
          <Chip title="Some steps depend on part of the level above, not all of it">partial</Chip>
        )}
      </td>
      <td data-col="done">
        <StepsDone workflow={workflow} roll={roll} />
      </td>
      <td data-col="runners">
        <Mix steps={workflow.steps} />
      </td>
      <td className="num" data-col="cost">
        <Spend spend={spend} short />
      </td>
      <td className="wfl-owner" data-col="owner" title={workflow.submitted_by ?? undefined}>
        {ownerShort(workflow.submitted_by)}
      </td>
      <td
        className="wfl-submitted"
        data-col="submitted"
        title={`${started.submittedTitle}; first step ${startedPhrase(started)}`}
      >
        {started.submitted}
      </td>
      <td className={`num wfl-dur${dur.ms === null ? ' is-absent' : ''}`} data-col="duration" title={dur.title}>
        {dur.text}
      </td>
    </tr>
  )
}

/**
 * THE OWNER, AS THE NAME BEFORE THE @ (#503: the whole address made Owner
 * 369px wide at 1440 and pushed Started and Duration off the table). The
 * address is the cell's title, whole; a value with no @ is printed as it is.
 */
export function ownerShort(owner: string | null | undefined): string {
  if (owner === null || owner === undefined || owner === '') return 'not recorded'
  const at = owner.indexOf('@')
  return at > 0 ? owner.slice(0, at) : owner
}

/**
 * THE STAGE-SHAPE GLYPH (wide-workflows.html §6): one column per stage, taller
 * for a wider one, filled with that stage's state mix (`stageGlyph`). Beside
 * the text shape, which stays and says it in words; so this is decoration and
 * hidden from assistive technology.
 */
function StageGlyph({ steps, taskById }: { steps: readonly WorkflowStep[]; taskById: ReadonlyMap<string, Task> | null }) {
  const cols = stageGlyph(steps, taskById)
  if (cols.length === 0) return null
  return (
    <span className="wf-sgl" aria-hidden="true">
      {cols.map((c, i) => (
        <i key={i} style={{ height: `${c.h}px` }}>
          {c.mix.map((m) => (
            <b key={m.kind} className={`mx-${m.kind}`} style={{ height: `${(100 * m.n) / c.n}%` }} />
          ))}
        </i>
      ))}
    </span>
  )
}

/** The row's second line. The step is bold; the cause is the task's own words. */
function RowWhyLine({ why }: { why: RowWhy }) {
  switch (why.kind) {
    case 'failed':
      return (
        <span className="wfl-why is-bad">
          failed at <b>{why.step}</b>: {why.why}
        </span>
      )
    case 'failed-unread':
      return <span className="wfl-why is-bad">failed: {why.why}</span>
    case 'parked':
      return (
        <span className="wfl-why">
          <b>{why.step}</b> parked · {why.why}
        </span>
      )
    case 'unread':
      return <span className="wfl-why is-warn">{why.why}</span>
  }
}

/**
 * STEPS DONE, as a bar in the workflow's own hue and `n/m`. An untrusted
 * census draws no fill -- a hatched track and the census's own words --
 * because a filled meter is a claim the numbers are a census, and a failed
 * read turned into a width would be a measurement of a census that failed.
 */
function StepsDone({ workflow, roll }: { workflow: Workflow; roll: Rollup }) {
  const s = derivedStateOf(workflow)
  const hue = s === null ? 'neu' : STATE_MARK[s].hue
  if (!roll.trustworthy) {
    // DONE OF TOTAL EVEN WHEN THE CENSUS IS PARTIAL (visual QA Q9,
    // 2026-10-02): every row drew the same whole grey hatch, whatever it had
    // counted. What the census DID count is drawn as the fill; the steps it
    // could not read are hatched after it; with no census at all the whole
    // track is hatched. No figure is invented -- the fill is the steps the
    // census read as succeeded, and the words still say it is unread.
    const counted = workflow.rollup?.counts.SUCCEEDED ?? 0
    const unread = workflow.rollup?.unreadable_steps.length ?? roll.total
    return (
      <span className="wfl-done" title={roll.why}>
        <ProgressBar done={workflow.rollup ? counted : 0} total={workflow.rollup ? roll.total : 0} unread={unread} tone={hue} label={roll.why} />
        <small>{roll.text}</small>
      </span>
    )
  }
  return (
    <span className="wfl-done" title={roll.why}>
      <ProgressBar done={roll.done} total={roll.total} tone={hue} label={roll.why} />
      <small>
        {roll.done}/{roll.total}
      </small>
    </span>
  )
}

/**
 * ONE WORKFLOW'S PAGE (workflows.html B): its head with Copy link and Cancel
 * workflow, then the Graph beside the picked step's card with the step table
 * under them, or the Table, or the Timeline -- the board machinery focused on
 * this one workflow, so every node, row and inspector reads exactly as it did.
 */
function WorkflowPage({
  board,
  id,
  query,
  choose,
  stores,
}: {
  board: WorkflowBoard
  id: string
  query: WorkflowQuery
  choose: (q: WorkflowQuery) => void
  stores: BoardStores
}) {
  const workflow = board.workflows.find((w) => w.workflow_id === id) ?? null
  const found = workflow !== null
  const derived = workflow === null ? null : derivedStateOf(workflow)
  // ONE ATTEMPT READ FOR THE PAGE: the head's total and the board's (the
  // nodes, the step table and its total) come from it, so the page never
  // shows two totals (WF-5). This workflow's steps go first under the cap.
  const usage = useWorkflowUsage(board.workflows, id)

  // OPENING A WORKFLOW RECORDS IT in the panel's Recent (5), with the state it
  // was read in, so the switcher can draw its mark without a read of its own.
  const label = workflow === null ? null : workflowLabel(workflow, board.taskById)
  useEffect(() => {
    if (found) rememberWorkflow(id, derived, label)
  }, [id, found, derived, label])

  // THE STEP CARD BESIDE THE GRAPH: on arrival, the step that most needs a
  // look -- the first failure, else the first step holding capacity -- is
  // picked, once per workflow, so closing the card is not undone by a poll.
  const { pick, choosePick } = stores
  const arrived = useRef<string | null>(null)
  useEffect(() => {
    if (workflow === null || arrived.current === id) return
    arrived.current = id
    if (pick !== null && pick.workflowId === id) return
    const first = firstStepToShow(workflow, board.taskById)
    if (first !== null) choosePick({ workflowId: id, stepId: first, focus: null })
  }, [workflow, id, board.taskById, pick, choosePick])

  const focus = useMemo<BoardFocus>(
    () => ({
      id,
      view: query.tab,
      // A tab change puts the Table's stage filter down: it belongs to the
      // band count that opened it, not to the Table.
      onView: (v) => choose({ ...query, tab: v, stage: null, stepState: null }),
      onOpen: (target, v) => choose({ ...query, wf: target, tab: v, stage: null, stepState: null }),
      filter: query.tab === 'table' && query.stage !== null && query.stepState !== null ? { level: query.stage, word: query.stepState } : null,
      onStageTable: (level, word) => choose({ ...query, tab: 'table', stage: level, stepState: word }),
      onClearFilter: () => choose({ ...query, stage: null, stepState: null }),
    }),
    [id, query, choose],
  )

  if (workflow === null) {
    return (
      <p className="wfp-missing">
        Workflow <Id title={id}>{id}</Id> is not in this read: the list reads the newest 100 workflows, and it could
        not be read on its own either. <a href={listHref(query)}>Back to the list</a>
      </p>
    )
  }
  return (
    <div className="wfp">
      <WorkflowHead workflow={workflow} taskById={board.taskById} reload={stores.reload} usage={usage} />
      <WfTabs id={workflow.workflow_id} query={query} view={query.tab} steps={workflow.steps.length} onView={focus.onView} />
      <Board board={board} stores={stores} focus={focus} usage={usage} />
    </div>
  )
}

/** Each head chip's bar width while the page reads, in em: state, failure policy, cost. */
const PAGE_SKELETON_CHIPS: readonly number[] = [5, 9, 4]
/** Each line of the body card's skeleton, as a percentage of the card. */
const PAGE_SKELETON_LINES: readonly number[] = [42, 88, 76, 64, 82, 58]

/**
 * ONE WORKFLOW'S PAGE WHILE ITS READ IS IN FLIGHT (#113). It drew the shell's
 * generic full-width rows, and then the head row and the view tabs appeared
 * above the board and pushed it down. Now the same `.wfp` frame is drawn: the
 * head row with its chips as bars and Copy link and Cancel workflow DISABLED
 * (there is no link to a workflow not yet read, and nothing to cancel), the
 * real tabs -- each view is an address, so choosing one during the load is
 * already meaningful -- with no step count, and a body card where the board's
 * will be -- its own `.wfp-skel-body` in the card's frame, not a `.wf-card`,
 * which everything that finds the loaded card keys on. The bars are
 * `.wfl-skel`, the list's contrast step and sweep.
 */
function WorkflowPageSkeleton({ id, query, choose }: { id: string; query: WorkflowQuery; choose: (q: WorkflowQuery) => void }) {
  return (
    <div className="wfp is-loading" aria-busy="true">
      <div className="wfp-head">
        <div className="wfp-chips" aria-hidden="true">
          {PAGE_SKELETON_CHIPS.map((w, i) => (
            <span key={i} className="c-chip">
              <span className="wfl-skel" style={{ width: `${w}em` }} />
            </span>
          ))}
        </div>
        <span className="wfp-actions">
          <Button disabled>Copy link</Button>
          <Button kind="danger" disabled>
            Cancel workflow
          </Button>
        </span>
      </div>
      <WfTabs id={id} query={query} view={query.tab} steps={null} onView={(v) => choose({ ...query, tab: v, stage: null, stepState: null })} />
      <div className="wfp-skel-body" aria-hidden="true">
        {PAGE_SKELETON_LINES.map((w, i) => (
          <span key={i} className="wfl-skel" style={{ width: `${w}%` }} />
        ))}
      </div>
    </div>
  )
}

/** The page's tabs, in the frame's order: the Graph, the Table with its count, the Timeline. */
const PAGE_TABS: readonly WorkflowView[] = ['graph', 'table', 'timeline']

/**
 * UNDERLINE TABS UNDER THE TITLE (workflows.html B; components.html A
 * `.c-tabs`), with the step count on Table. They were a boxed segmented control
 * inside the body card (#503). Each tab is a route of its own, so the one on
 * screen is `aria-current="page"`, not a pressed toggle.
 */
function WfTabs({
  id,
  query,
  view,
  steps,
  onView,
}: {
  id: string
  query: WorkflowQuery
  view: WorkflowView
  /** Null while the page reads: no count is drawn rather than one nobody read. */
  steps: number | null
  onView: (v: WorkflowView) => void
}) {
  // THE CANONICAL UNDERLINE TABS (components.html A; workflows.html B): each
  // view is its own address, so each tab is a link that opens in a new tab,
  // and a plain click switches the view in place.
  const href = (v: WorkflowView) => workflowHref(id, query, v)
  return (
    <Tabs
      label="Views of this workflow"
      current={view}
      tabs={PAGE_TABS.map((v) => ({ key: v, label: VIEW_LABEL[v], href: href(v), ...(v === 'table' && steps !== null ? { count: steps } : {}) }))}
      onGo={(to) => {
        const v = PAGE_TABS.find((t) => href(t) === to)
        if (v !== undefined) onView(v)
      }}
    />
  )
}

/** The step the card opens on: the first failure, else the first live step, else none. */
function firstStepToShow(workflow: Workflow, taskById: ReadonlyMap<string, Task> | null): string | null {
  let live: string | null = null
  for (const s of workflow.steps) {
    const st = stepState(s, taskById)
    if (st.kind !== 'state') continue
    if (st.state === 'FAILED' || st.state === 'DEAD_LETTERED') return s.step_id
    if (live === null && STATE_MARK[st.state].hue === 'live') live = s.step_id
  }
  return live
}

function WorkflowHead({
  workflow,
  taskById,
  reload,
  usage,
}: {
  workflow: Workflow
  taskById: ReadonlyMap<string, Task> | null
  reload: () => void
  /** The page's attempt read: the head totals from the figures the table does (WF-5). */
  usage: UsageRead
}) {
  const spend = workflowSpend(workflow.steps, taskById, telemetryOf(usage))
  const label = workflowLabel(workflow, taskById)
  const pr = workflowPullRequest(workflow, taskById)
  // ONE ROW, CHIPS LEFT AND ACTIONS RIGHT (workflows.html B; #503). The title
  // is the shell's head above, and its meta line the count note over this
  // row (`pageSummary`, #138); this row
  // carries what is not a sentence -- the state, the pull request, the failure
  // policy, the cost, the id when the title is the name -- and Copy link and
  // Cancel workflow, centred on the row so nothing leaves blank space under them.
  return (
    <div className="wfp-head">
      <div className="wfp-chips">
        <WorkflowStateMark workflow={workflow} />
        {pr !== null && <PullRequestLink pr={pr} className="c-chip is-link" />}
        <Chip>on failure: {workflow.on_step_failure.toLowerCase() === 'continue' ? 'continue' : 'fail the workflow'}</Chip>
        <span className="wfp-cost">
          <Chip>{spend.usd === null ? <span className="wf-cell is-absent">cost not reported</span> : <Spend spend={spend} />}</Chip>
        </span>
        {label !== null && <Id title={workflow.workflow_id}>{workflow.workflow_id}</Id>}
        {cancelPending(workflow) && <span className="tag wait">cancel requested</span>}
      </div>
      <span className="wfp-actions">
        <CopyLink />
        <CancelWorkflow workflow={workflow} taskById={taskById} onDone={reload} />
      </span>
    </div>
  )
}

function CopyLink() {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    try {
      void globalThis.navigator?.clipboard?.writeText(globalThis.location.href).then(
        () => setCopied(true),
        () => setCopied(false),
      )
    } catch {
      /* no clipboard here; the address bar still holds the link */
    }
  }
  return (
    <Button onClick={copy}>{copied ? 'Link copied' : 'Copy link'}</Button>
  )
}

/** What cancelling would do, step by step, from the states on this read. */
function cancelConsequence(workflow: Workflow, taskById: ReadonlyMap<string, Task> | null): string {
  let live = 0
  let waiting = 0
  let unstarted = 0
  let unread = 0
  for (const s of workflow.steps) {
    const st = stepState(s, taskById)
    if (st.kind === 'unstarted') unstarted += 1
    else if (st.kind === 'unknown') unread += 1
    else if (TERMINAL_STATES.has(st.state)) continue
    else if (STATE_MARK[st.state].hue === 'live') live += 1
    else waiting += 1
  }
  const n = (k: number, one: string) => `${k} ${one}${k === 1 ? '' : 's'}`
  const parts: string[] = []
  if (unstarted > 0) parts.push(`${n(unstarted, 'step')} not started will never start`)
  if (waiting > 0) parts.push(`${n(waiting, 'waiting step')} ${waiting === 1 ? 'is' : 'are'} cancelled now`)
  if (live > 0) {
    parts.push(
      `${n(live, 'running step')} ${live === 1 ? 'is' : 'are'} asked to stop and keep${live === 1 ? 's its slot' : ' their slots'} until ${live === 1 ? 'its worker exits' : 'their workers exit'}`,
    )
  }
  if (unread > 0) parts.push(`${n(unread, 'step')} whose state was not read ${unread === 1 ? 'is' : 'are'} asked to stop too`)
  const head = parts.length === 0 ? 'Every step has already ended; nothing is left to cancel.' : `${parts.join('; ')}.`
  return `${head} Steps that finished keep their results.`
}

/**
 * CANCEL WORKFLOW, WITH AN INLINE CONFIRM (workflows.html F): the first click
 * only asks, naming what it cancels and what keeps running; the confirm sends
 * `POST /v1/workflows/{id}/cancel`; "Keep it running" puts the question away
 * and sends nothing. Shown only while the workflow has not ended -- a finished
 * workflow has nothing left to cancel -- and replaced by the request's own tag
 * once one is in flight.
 */
export function CancelWorkflow({
  workflow,
  taskById,
  onDone,
  cancel = cancelWorkflow,
}: {
  workflow: Workflow
  taskById: ReadonlyMap<string, Task> | null
  onDone: () => void
  /** Injectable for the tests; the API's otherwise. */
  cancel?: (id: string) => Promise<Result<CancelWorkflowResult>>
}) {
  const [phase, setPhase] = useState<'idle' | 'asking' | 'sending' | 'failed'>('idle')
  const [error, setError] = useState<string | null>(null)
  const derived = derivedStateOf(workflow)
  if (derived !== null && TERMINAL_STATES.has(derived)) return null
  if (cancelPending(workflow) && phase === 'idle') return null
  // THE ID IS TYPED WHILE A STEP RUNS (states.html §12, picked C): stopping a
  // step that holds capacity ends work in flight, so the confirm unlocks only
  // on the exact workflow id. With nothing running, the two-click confirm
  // stays: cancelling work that has not started loses nothing.
  const typedConfirm = holdsCapacity(workflow, taskById)

  const send = async () => {
    setPhase('sending')
    const r = await cancel(workflow.workflow_id)
    if (r.status === 'ok' || r.status === 'empty') {
      setPhase('idle')
      onDone()
      return
    }
    setError(r.status === 'error' || r.status === 'stale' ? r.error.message : 'The cancel request did not complete.')
    setPhase('failed')
  }

  const trigger = (
    <Button kind="danger" onClick={() => setPhase('asking')}>
      Cancel workflow
    </Button>
  )
  if (phase === 'idle') return trigger
  const reset = () => {
    setError(null)
    setPhase('idle')
  }
  // WHILE A STEP RUNS: the canonical typed confirm (components.html A,
  // states.html C §12), with this page's own account of what cancelling does.
  if (typedConfirm) {
    return (
      <>
        {trigger}
        <TypedConfirm
          title="Cancel this workflow?"
          name={workflow.workflow_id}
          verb="Cancel the workflow"
          keep="Keep it running"
          onConfirm={() => void send()}
          onClose={reset}
          busy={phase === 'sending' ? 'Cancelling…' : false}
          error={phase === 'failed' && error !== null ? `The cancel was not recorded: ${error}` : undefined}
        >
          <p>{cancelConsequence(workflow, taskById)}</p>
        </TypedConfirm>
      </>
    )
  }
  return (
    <div className="wfp-confirm" role="group" aria-label="Confirm cancelling this workflow">
      <p>{cancelConsequence(workflow, taskById)}</p>
      {phase === 'failed' && error !== null && (
        <p className="wfp-confirm-err" role="alert">
          The cancel was not recorded: {error}
        </p>
      )}
      <span className="wfp-confirm-do">
        <Button kind="danger-filled" busy={phase === 'sending' ? 'Cancelling…' : false} onClick={() => void send()}>
          Yes, cancel the workflow
        </Button>
        <Button kind="ghost" disabled={phase === 'sending'} onClick={reset}>
          Keep it running
        </Button>
      </span>
    </div>
  )
}

/** Whether any step of the workflow holds capacity on this read: LEASED, DISPATCHED, STARTING or RUNNING. */
function holdsCapacity(workflow: Workflow, taskById: ReadonlyMap<string, Task> | null): boolean {
  return workflow.steps.some((s) => {
    const st = stepState(s, taskById)
    return st.kind === 'state' && STATE_MARK[st.state].hue === 'live'
  })
}

