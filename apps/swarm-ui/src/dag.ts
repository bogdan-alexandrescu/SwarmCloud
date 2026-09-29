// The workflow board's arithmetic: shape, layout, timing and spend.
//
// PURE ON PURPOSE. No React, no DOM, no dates read from the clock -- `now` is
// always a parameter. `Workflows.tsx` is then only pixels, and every claim this
// board makes can be asserted without rendering anything. It is the same split
// `charts/series.ts` makes for the charts, and for the same reason: the hard
// part of this screen is deciding WHAT IS KNOWN, and that decision should not
// be reachable only through a component tree.
//
// THE RULE THIS FILE EXISTS TO ENFORCE. An absent measurement must never
// become a number, and a measured zero must stay a digit. Both halves have a
// type here rather than a convention:
//
//   * `StepDuration`'s `none` arm carries NO `seconds` field, so a renderer
//     that tries to print a number for a step that never started does not
//     compile. A step with no task has no duration; `0s` would be a claim that
//     it ran instantly.
//   * `WorkflowSpend.usd` is `null` when nothing reported a cost, and a
//     reported `0` counts as MEASURED and comes back as `0`. "No attempt
//     reported a cost" and "this cost nothing" are different sentences.

import {
  TERMINAL_STATES,
  formatDuration,
  stagedInputsOf,
  stateTone,
  stepState,
  type StagedInputs,
  type StepState,
  type Task,
  type TaskState,
  type Tone,
  type WorkflowStep,
  usageOf,
} from './types'
import type { StepUsage } from './api'

// ---------------------------------------------------------------------------
// Depth
// ---------------------------------------------------------------------------

/**
 * Depth of each step: 0 for roots, else 1 + max(depth of dependencies).
 *
 * Moved here from `Workflows.tsx` unchanged. A cycle should be impossible --
 * the scheduler rejects one at submission -- but a UI that hangs on malformed
 * data is worse than one that draws it flat, so this terminates rather than
 * trusting that.
 */
export function levelsOf(steps: readonly WorkflowStep[]): WorkflowStep[][] {
  const byId = new Map(steps.map((s) => [s.step_id, s]))
  const depth = new Map<string, number>()

  const resolve = (id: string, seen: Set<string>): number => {
    const cached = depth.get(id)
    if (cached !== undefined) return cached
    if (seen.has(id)) return 0
    const step = byId.get(id)
    if (!step || step.depends_on.length === 0) {
      depth.set(id, 0)
      return 0
    }
    seen.add(id)
    const d = 1 + Math.max(...step.depends_on.map((p) => resolve(p, seen)))
    seen.delete(id)
    depth.set(id, d)
    return d
  }

  steps.forEach((s) => resolve(s.step_id, new Set()))
  if (steps.length === 0) return []
  const max = Math.max(0, ...steps.map((s) => depth.get(s.step_id) ?? 0))
  return Array.from({ length: max + 1 }, (_, lvl) =>
    steps.filter((s) => (depth.get(s.step_id) ?? 0) === lvl),
  )
}

// ---------------------------------------------------------------------------
// Shape -- the topology, compressed enough to survive a collapsed row
// ---------------------------------------------------------------------------

/**
 * What kind of graph this is, in one word.
 *
 * THIS IS THE DEFECT THE GRAPH WORK EXISTS TO FIX, and it has to survive being
 * collapsed. Five steps that run in parallel and five steps that run one after
 * another are the same count, the same progress fraction and the same spend;
 * they are completely different runs. A reader choosing which of ten workflows
 * to open must be able to tell them apart without opening any of them.
 */
export type DagKind = 'empty' | 'single' | 'chain' | 'fan-out' | 'fan-in' | 'diamond' | 'mixed'

export interface DagShape {
  /** Steps per dependency level, in order. `[1, 5, 1]` is a fan-out and a join. */
  readonly widths: readonly number[]
  readonly kind: DagKind
  /** `1 → 5 → 1`. The one-line rendering. */
  readonly text: string
  /** A sentence for the `title`, e.g. "1 step fans out to 5, then joins into 1." */
  readonly label: string
  readonly levels: number
  readonly widest: number
  readonly steps: number
  /**
   * The step every parallel branch converges INTO, when there is exactly one.
   * Named rather than counted: "converge into 1" and "converge into synthesis"
   * cost the same line and only one of them can be acted on. null when the
   * last level holds more than one step, because then there is no single join
   * to name and inventing one would be a claim about the graph.
   */
  readonly joinStep: string | null
  /** The step the fan-out opens FROM, on the same terms. */
  readonly forkStep: string | null
}

export function shapeOf(steps: readonly WorkflowStep[]): DagShape {
  const levelList = levelsOf(steps)
  const widths = levelList.map((l) => l.length)
  const total = steps.length
  const firstLevel = levelList[0] ?? []
  const lastLevel = levelList[levelList.length - 1] ?? []
  const forkStep = firstLevel.length === 1 ? (firstLevel[0]?.step_id ?? null) : null
  const joinStep = lastLevel.length === 1 ? (lastLevel[0]?.step_id ?? null) : null
  const levels = widths.length
  const widest = widths.length === 0 ? 0 : Math.max(...widths)
  const text = widths.join(' → ')

  if (total === 0)
    return { widths, kind: 'empty', text: '—', label: 'This workflow has no steps.', levels, widest, steps: total, joinStep, forkStep }
  if (total === 1)
    return { widths, kind: 'single', text, label: 'A single step.', levels, widest, steps: total, joinStep, forkStep }
  if (widest === 1) {
    return {
      widths,
      kind: 'chain',
      text,
      label: `A chain: ${total} steps, each waiting for the one before it. Nothing here runs in parallel.`,
      levels,
      widest,
      steps: total,
      joinStep,
      forkStep,
    }
  }

  const first = widths[0] ?? 0
  const last = widths[widths.length - 1] ?? 0
  const kind: DagKind =
    first === 1 && last === 1 && levels >= 3
      ? 'diamond'
      : last === 1 && last < widest
        ? 'fan-in'
        : first === 1 && first < widest
          ? 'fan-out'
          : 'mixed'

  // The join and the fork are named where one exists. `${last}` alone said
  // "converge into 1", which is a count of something the reader cannot then
  // go and look at -- and naming the step is the whole difference between a
  // shape the collapsed row describes and a shape it merely measures.
  const into = joinStep ?? `${last}`
  const from = forkStep ?? `${first}`
  const label =
    kind === 'diamond'
      ? `A fan-out and a join: ${from} opens into ${widest} running in parallel, and they converge into ${into}.`
      : kind === 'fan-in'
        ? `A join: ${widest} steps at the widest point converge into ${into}.`
        : kind === 'fan-out'
          ? `A fan-out: ${from} opens into ${widest} running in parallel.`
          : `${total} steps over ${levels} levels, up to ${widest} in parallel.`

  return { widths, kind, text, label, levels, widest, steps: total, joinStep, forkStep }
}

// ---------------------------------------------------------------------------
// Layout -- a real canvas with real edges
// ---------------------------------------------------------------------------
//
// FLOW IS TOP TO BOTTOM, AND THIS IS THE AXIS TRANSPOSE. It was left to
// right: the dependency LEVEL drove x and a step's position within its level
// drove y, so five parallel steps stacked into one tall column. The owner's
// reading of that was the reason this pass exists --
//
//   "workflows when expanded should have all nodes at the same stage displayed
//    horizontally not vertically has it is now and the natural flow of the
//    workflow should be top to bottom rendered rather than left to right."
//
// So: LEVEL DRIVES Y, POSITION WITHIN A LEVEL DRIVES X. Siblings sit side by
// side on one band; the graph descends. Two consequences are deliberate rather
// than accidental, because they are the trade this axis makes:
//
//   * A WIDE FAN-OUT NOW PUSHES THE CANVAS SIDEWAYS. Five parallel steps at
//     NODE_W each is wider than any card on this page, and `.wf-canvas-wrap`
//     scrolls for exactly that (and is pinned to, by
//     test_the_dag_fills_the_width_it_is_given). Scaling a fan down to fit
//     turns the step names into texture, which is the one thing a node may not
//     become. Width is the axis that gives.
//   * A CHAIN NOW FITS A PHONE. The commonest shape in this product is a chain,
//     and left-to-right made the commonest shape the one that always scrolled:
//     at 390px a four-step chain was four horizontal scrolls. Descending, it is
//     a list, which is what a reader on a phone already knows how to read.
//
// The numbers are computed here rather than measured in the browser so the
// layout is testable and so the first paint is not a reflow. A node is sized
// for its four facts -- name, status, runner profile, duration -- with room
// for the stop control.

// ---------------------------------------------------------------------------
// The type metrics every width on this canvas is derived from
// ---------------------------------------------------------------------------
//
// ONE MEASUREMENT, AND EVERY NODE WIDTH BELOW IS ARITHMETIC OVER IT. `NODE_W`
// was already derived from a MEASURED character advance rather than an assumed
// `0.6em`, and its docblock records the measurement: `measureText('no attempt
// yet')` against the rendered `.node-num dd` font is **118.0px at 14px**. That
// string is 14 characters, so the advance is 118/14 = 8.4286px at 14px, or
// 118/14/14 = 0.60204 of the font size.
//
// THE FIGURE IS NEAR 0.6em AND THAT IS NOT A LICENCE TO ASSUME 0.6em. It came
// out near the guess; a second face, a second weight or a fallback further
// down `--mono`'s stack would not. It is written as the division that produced
// it so the next reader can see which number was measured and which were
// computed from it.
//
// TWO INDEPENDENT CHECKS THAT THE RATIO IS THE RIGHT ONE, both taken from
// figures other passes measured in the browser and wrote into this file before
// this constant existed:
//
//   `scan-terraform`          14 chars x 8.4286 = 118.0  (NODE_W's docblock: 118px)
//   `21.4k in · 3.2k out`     19 chars x 8.4286 = 160.1  (NODE_W's docblock: 160px)
//   `99.9k in · 99.9k out`    20 chars x 8.4286 = 168.6  (NODE_W's docblock: 169px)
//   `claude-code` at 12px     11 chars x 7.2245 =  79.5  (StepNode's comment:  79px)
//
// Four figures, measured separately, all reproduced to within a pixel. That is
// what makes this a derivation rather than a fit.
export const MONO_ADVANCE_EM = 118 / 14 / 14

/** The advance of `chars` characters of `--mono` at `sizePx`. */
export function monoW(chars: number, sizePx: number): number {
  return chars * MONO_ADVANCE_EM * sizePx
}

/** `--t-micro` and `--t-body`, the only two steps of the scale a node uses. */
const T_MICRO = 12
const T_BODY = 14

/**
 * Everything between a node's outer edge and its content, across.
 *
 * `.node` declares `padding: 10px 12px 42px` and `border: 1px solid` with a
 * `border-left: 3px`, under the sheet's global `box-sizing: border-box`. So a
 * node of width W has W - 24 - 4 of content.
 *
 * THE FOUR PIXELS OF BORDER WERE MISSING FROM THE LAST DERIVATION, and this is
 * the correction that moves NODE_W. Its docblock reads "248 - 24px of `.node`
 * padding ... 224 of content", which counts the padding and not the border:
 * the real content box at 248 is 220. On the cell that figure was chosen for
 * -- `21.4k in · 3.2k out`, needing 160px in a value column of 220 - 46 - 8 --
 * the slack was 6px rather than the 10px recorded, which still clears. On the
 * one the same docblock says it "also clears", `99.9k in · 99.9k out` at
 * 168.6px, it does NOT: 46 + 8 + 168.6 = 222.6 against 220, so that string
 * ellipses today by 2.6px. It is a compound of two measured figures, which is
 * the class of truncation the overflow inventory calls F1 and the worst one
 * this screen can produce.
 */
export const NODE_CHROME_W = 12 * 2 + (3 + 1)

/** `.ctl-dot`: 8px square, and it never shrinks -- it is the state channel
 *  that survives greyscale. */
const DOT_W = 8
/** `--ctl-s2`, the gap inside `.node-line` and between a figure's label and
 *  its value. */
const CTL_S2 = 8
/** `.node-num`'s label column, declared as `grid-template-columns: 46px ...`. */
const NUM_LABEL_W = 46

/**
 * `opens the PR`, plus the 6px `.node-id .tag` margin that carries it.
 *
 * Taken from the browser figure NODE_W's docblock already records -- "the
 * widest that also carries the `opens the PR` tag is `synthesis` at 76 + 6 +
 * ~90 = 172px" -- so the tag and its margin are 96px. It is a measured figure
 * reused rather than a fresh guess, and it is the reason the tag is one of the
 * fields semantic zoom DROPS: 96px on every node in a stage, to mark the one
 * node in the workflow that opens the pull request, is the worst ratio of any
 * field on this card.
 */
const PR_TAG_W = 96

/**
 * The longest state word a node can print, in characters.
 *
 * `present()` prints `not started` (11), `state unread` (12), or the task's
 * own state lowercased. The longest of the twelve `TaskState`s is
 * `dead_lettered` at 13.
 *
 * DEAD_LETTERED IS COUNTED ALTHOUGH `types.ts` RECORDS THAT `finish()` NEVER
 * WRITES IT. The scheduler still lists it in `_FAILED_PARENT_STATES`, so if one
 * ever reaches a task document it must render -- and `stageCensus` already
 * ranks it with the failures on exactly that argument. Sizing it out buys four
 * pixels and buys them by assuming an unreachable branch stays unreachable.
 */
const STATE_CHARS = 13

/**
 * The longest duration text that can share a line with the longest state word.
 *
 * `stepDuration` prints at most `duration unread` (15) for a step whose task
 * went terminal with no completion time -- which is the arm a `dead_lettered`
 * step lands in, so 13 + 15 is a PAIR that can really happen. The longer
 * strings it can print (`running 365d 23h`, 16) belong to a RUNNING task,
 * whose state word is 7 characters, so 7 + 16 = 23 never binds.
 */
const DUR_WITH_STATE_CHARS = 15

/**
 * The longest duration text on a line of its OWN, which is what the reduced
 * tiers give it. Here the running arm does bind: `running 365d 23h` is 16.
 *
 * PAST A YEAR IT GAINS A CHARACTER AND OVERFLOWS RATHER THAN WRAPPING, and
 * that is the failure deliberately chosen. `layoutOf` commits to each card's
 * height before it is painted, so a wrapped line pushes a node past the box
 * drawn for it and every node beneath it is overlapped -- invisibly, and on
 * the one screen that happens to hold a year-old step. Overflow is visible and
 * local. A step that has been running for a year is a platform failure this
 * console should not be quietly tidying up anyway.
 */
const DUR_CHARS = 16

/**
 * The longest figure a `.node-num dd` may not truncate, in characters.
 *
 * `99.9k in · 99.9k out` is 20. NODE_W's docblock measured it at 169px and
 * named it as the bound the node "also clears"; at the real content box it did
 * not, which is the defect this file's chrome correction fixes.
 */
const FIGURE_CHARS = 20

/** `.node-line` carrying the dot, the state word and nothing else. */
const STATE_ROW_W = DOT_W + CTL_S2 + monoW(STATE_CHARS, T_MICRO)
/** `.node-line` carrying the dot, the state word AND the duration -- the row
 *  the full tier draws, and the one that sets NODE_W. */
const STATE_AND_DUR_ROW_W = STATE_ROW_W + CTL_S2 + monoW(DUR_WITH_STATE_CHARS, T_MICRO)
/** The duration on a row of its own, which is how the reduced tiers draw it. */
const DUR_ROW_W = monoW(DUR_CHARS, T_MICRO)
/** One `.node-num`: the 46px label column, its `--ctl-s2` gap, and a value. */
const FIGURES_ROW_W = NUM_LABEL_W + CTL_S2 + monoW(FIGURE_CHARS, T_BODY)

/**
 * The longest `.node-src` line, in characters: `cost · tokens from result`, 25.
 *
 * WF-5's source note, ON A LINE OF ITS OWN BECAUSE THE VALUE COLUMN HAS NO ROOM
 * FOR IT (#160 review). It first shared the figure's row, in a column budgeted
 * for a 20-character figure and nothing else: `from result` is 11 characters
 * of --t-micro (79.5px) plus a `--ctl-s1` gap, so beside `21.4k in · 3.2k out`
 * it showed as `…`, and past 173px the figure itself was clipped. The two ways
 * to pay for it were width -- about 84px on every Figures-tier node, which
 * takes `STAGE_FITS` from 3 to 2 and sends a three-wide stage to `details`,
 * where no figure is drawn at all -- or one micro row of height. Height is the
 * axis this canvas has not run out of, so the note has a row, and the row
 * names the figures it is about rather than sitting beside them.
 */
const SRC_CHARS = 25
/** `.node-src` at --t-micro. Well inside the content box; counted anyway. */
const SRC_ROW_W = monoW(SRC_CHARS, T_MICRO)

/** `--ctl-s1`, the gap between a node card's flex children. Declared up here
 *  rather than beside `depLines` because `nodeHeightAt` sums it at module
 *  initialisation, and a `const` read before its own declaration is a
 *  temporal-dead-zone throw rather than a compile error. */
const GAP = 4

/** `--lh-body` and `--lh-micro`, the two leadings a node's rows use. */
const LH_BODY = 1.5
const LH_MICRO = 1.45
/** One `.node-id` row, at --t-body / --lh-body. */
const ROW_BODY_H = T_BODY * LH_BODY
/** One `.node-line`, `.node-dur` or `.node-meta` row, at --t-micro / --lh-micro. */
const ROW_MICRO_H = T_MICRO * LH_MICRO
/**
 * `.node-nums`, the whole four-figure block.
 *
 * `8 pad + 4 x (21 + 1) + 3 x 2` -- NODE_H's own arithmetic, unchanged. The
 * `+ 1` per row is the transparent dashed border every cell declares so that a
 * figure turning out to be unmeasured does not grow the card after `layoutOf`
 * has committed to its height.
 */
const NUMS_H = CTL_S2 + 4 * (ROW_BODY_H + 1) + 3 * 2
/** `.node`'s vertical chrome: 10px of padding above, the 42px stop strip
 *  below, and the 1px border top and bottom. */
const NODE_CHROME_H = 10 + 42 + 2

// ---------------------------------------------------------------------------
// Semantic zoom -- which fields a node stops drawing when the column runs out
// ---------------------------------------------------------------------------
//
// WHAT STAGE-COLLAPSING LEFT UNPAID. A stage wider than the column is drawn as
// one band, which made a 30-step run POSSIBLE to read; it did not make the
// canvas good. The band is a summary, and opening one puts the reader straight
// back into the 3,580px row the band was standing in for. The measured run's
// widest stage is 13 steps and the honest answer for it is still a band -- but
// a FIVE-step fan is not 13, and collapsing it was only ever necessary because
// a node is 255px wide whatever it has to say.
//
// SO THE NODE SHEDS FIELDS INSTEAD OF SHRINKING. The ux plan's option 1 (§1.1)
// is "the canvas scales to the column and nodes shed fields as they shrink",
// and the first half of that sentence is the half this implementation refuses:
// scaling shrinks type, `typescale.test.ts` holds a floor under the type, and
// the owner has twice said this console is hard to read. A node scaled to fit
// is texture. A node that has stopped saying which runner profile it used is
// still a node.
//
//   every tier         the step name, and its state
//   details and up     + the runner profile, + how long it has taken
//   figures only       + the four run figures, + the `opens the PR` tag
//
// EVERY WIDTH BELOW IS DERIVED FROM THE MEASURED ADVANCE AT THE TOP OF THIS
// FILE AND FROM THE WORKFLOW'S OWN STRINGS. Nothing here is a round number
// chosen because it looked right: a tier's width is the widest row that tier
// still draws, and a row's width is its longest possible string times a
// character advance that four independent browser measurements agree on.
//
// THE THREE MARKS SURVIVE AT EVERY TIER. A measured figure, a figure that was
// not measured (a word on a dashed rule, never a blank and never a zero) and a
// duration that does not exist (`not started`, never `0s`) are all still drawn
// wherever their field is drawn at all. What a tier removes is the whole FIELD,
// never the field's value -- an empty cell where an unmeasured figure was would
// turn "nobody measured this" into "this is fine", which is the one thing this
// screen exists to prevent. `Workflows.tsx` pairs every reduced tier with a
// visible mark naming the dropped fields, so the absence reads as a decision
// about the zoom rather than a fact about the step.
//
// AND A FAILURE IS NEVER INVISIBLE. The state word, the `.ctl-dot` in its tone
// and the card's own left accent are in the `names` tier -- the smallest one --
// so a failed or cancelled step is findable at every zoom without expanding
// anything. A stage still too wide for the smallest tier keeps its band, and
// the band's census puts failures at the front of the line.

/**
 * How much a node is saying, from most to least.
 *
 * NAMED FOR WHAT THEY KEEP, NOT FOR HOW BIG THEY ARE. `large`/`medium`/`small`
 * would describe the pixels, and the pixels are an OUTPUT here -- two tiers can
 * come out the same width on a workflow whose step names are what was setting
 * the width, and when they do that is information (no further zoom will help)
 * rather than a bug.
 */
export type ZoomTier = 'figures' | 'details' | 'names'

/** Widest first. `autoTier` walks this in order and takes the first that fits. */
export const ZOOM_TIERS: readonly ZoomTier[] = ['figures', 'details', 'names']

/**
 * What each tier has stopped drawing, in the words the node itself uses.
 *
 * THIS IS RENDERED, not only documented. `Workflows.tsx` puts it on a mark
 * beside the canvas so that a reader looking at a node with no profile on it
 * can tell that the profile was dropped by the zoom rather than missing from
 * the data. Empty for the full tier, which is how the mark knows to be silent.
 */
export const TIER_DROPS: Readonly<Record<ZoomTier, readonly string[]>> = {
  figures: [],
  details: ['figures'],
  names: ['profile', 'duration', 'figures'],
}

/**
 * The height of a node at this tier, with no dependency line.
 *
 * SUMMED FROM THE ROWS THE TIER ACTUALLY DRAWS, which is the discipline NODE_H
 * had to be rewritten to after 244 turned out to be three pixels short of what
 * `.node` rendered. `figures` reproduces NODE_H's own arithmetic exactly:
 *
 *   chrome .................................. 54
 *   .node-id      21 + .node-line 17.4 ...... 38.4
 *   .node-meta    17.4 ...................... 17.4
 *   .node-nums    8 + 4 x 22 + 3 x 2 ........ 102
 *   .node-src     17.4 ...................... 17.4
 *   4 x --ctl-s1 between five children ...... 16
 *                                            -----
 *                                             245.2, ceil 246
 *
 * `.node-src` IS RESERVED ON EVERY FIGURES-TIER NODE, empty unless a figure is
 * the result's (WF-5; `SRC_CHARS` has why it is a row). Whether a figure is the
 * result's is known only once the attempt read lands, and a card that grew a
 * row at that moment would push every level beneath it down the page.
 *
 * `details` drops `.node-nums` and gains a row, because the duration moves off
 * `.node-line` onto one of its own -- which is the trade that makes the tier
 * narrow at all, and it is paid in height rather than in a truncated figure.
 * `names` drops the profile row too.
 *
 * THE 42px STOP STRIP IS RESERVED AT EVERY TIER. A card that grew by 39px the
 * moment its step started running would shove every level beneath it down the
 * page mid-poll, and that is as true of a 97px card as of a 246px one.
 */
export function nodeHeightAt(tier: ZoomTier): number {
  const rows =
    tier === 'figures'
      ? // .node-id, .node-line (state AND duration), .node-meta, .node-nums, .node-src
        [ROW_BODY_H, ROW_MICRO_H, ROW_MICRO_H, NUMS_H, ROW_MICRO_H]
      : tier === 'details'
        ? // .node-id, .node-line (state), .node-dur, .node-meta
          [ROW_BODY_H, ROW_MICRO_H, ROW_MICRO_H, ROW_MICRO_H]
        : // .node-id, .node-line (state)
          [ROW_BODY_H, ROW_MICRO_H]
  const content = rows.reduce((t, n) => t + n, 0) + (rows.length - 1) * GAP
  return Math.ceil(NODE_CHROME_H + content)
}

/**
 * The width of a node at this tier, for THIS workflow.
 *
 * IT TAKES THE STEPS, and that is the part that makes the number honest rather
 * than merely derived. A step id is unbounded, so any fixed width is a bet that
 * nobody names a step something long -- and `.node-id` has no `text-overflow`
 * and no `white-space`, so losing that bet WRAPS the name onto a second line,
 * on a card whose height `layoutOf` has already committed to, overlapping the
 * node beneath it. Measuring the workflow's own longest name removes the bet:
 * the name always fits, at every tier, and nothing is ever ellipsed to make it.
 *
 * WHAT IS BOUNDED BY A CONSTANT INSTEAD, AND WHY EACH ONE HAS TO BE:
 *
 *  * the STATE WORD and the DURATION are bounded by their longest possible
 *    strings, not by what this workflow happens to be showing. A width taken
 *    from the current text would change as a step moved from `queued 9s` to
 *    `queued 10s` -- the canvas reflowing once a second, under a reader's
 *    cursor, because a digit was added;
 *  * the FIGURES row is bounded the same way and for a stronger version of the
 *    same reason: the figures arrive from a SECOND read, after the canvas is
 *    already on screen, so a width that depended on them would relayout the
 *    whole graph at the moment the attempt read lands.
 *
 * The runner profile is neither: it comes off the workflow document with the
 * steps, it does not tick, and it is as unbounded as a step id -- so it is
 * measured from the steps exactly as the name is.
 */
export function nodeWidthAt(tier: ZoomTier, steps: readonly WorkflowStep[]): number {
  const nameW = Math.max(0, ...steps.map((s) => monoW(s.step_id.length, T_BODY)))
  const profileW = Math.max(0, ...steps.map((s) => monoW(s.runner_profile.length, T_MICRO)))
  if (tier === 'names') {
    return Math.ceil(NODE_CHROME_W + Math.max(nameW, STATE_ROW_W))
  }
  if (tier === 'details') {
    return Math.ceil(NODE_CHROME_W + Math.max(nameW, STATE_ROW_W, DUR_ROW_W, profileW))
  }
  // The full tier, where the `opens the PR` tag is drawn and the state and the
  // duration share one line. `NODE_W` is that arithmetic over the constants;
  // this takes the larger of it and what this workflow's own names need.
  return Math.max(
    NODE_W,
    Math.ceil(
      NODE_CHROME_W +
        Math.max(nameW + PR_TAG_W, STATE_AND_DUR_ROW_W, profileW, FIGURES_ROW_W, SRC_ROW_W),
    ),
  )
}

/**
 * The width of a node card.
 *
 * THE REASON IT WAS 236 IS DELETED AND THE REASON IT IS 248 WAS MEASURED.
 * 236 came from `.node-links` -- `input & output →` and `attempts →` on one
 * non-wrapping row, about 199px of advance. Both anchors pointed into the same
 * task drawer the node itself now opens, so that row is gone (see `StepNode`).
 *
 * The width is now set by THE WIDEST STRING A FIGURE CELL MUST NOT TRUNCATE,
 * and the first attempt at this pass got it wrong in a way worth recording.
 * The obvious candidate is the longest absence word, `no attempt yet`, which
 * measures 118.0px at the rendered `.node-num dd` font (14px ui-monospace,
 * `measureText`, not an assumed 0.6em advance). Sized to that, NODE_W came out
 * at 212 -- and a browser sweep of the rendered cells then found TEN of
 * fifty-six clipped, every one of them the `tokens` cell:
 *
 *   `21.4k in · 3.2k out` ... needs 160px, had 130
 *   `8.2k in · 1.1k out`  ... needs 152px, had 130
 *
 * That cell is a COMPOUND of two measured figures and it is the long one, not
 * the absences. `3.2…` standing where `3.2k out` was is a prefix of a number
 * in the place the number was -- the worst class in the overflow inventory
 * (F1), and one no gate in this repository can see. So:
 *
 *   248 - 24px of `.node` padding ............ 224 of content
 *   - 46px `.node-num` label column ..........
 *   - 8px  `--ctl-s2` column gap .............
 *                                             ---
 *                                             170 for the value, vs 160 needed
 *
 * That also clears `99.9k in · 99.9k out` (169px). Beyond that -- a single
 * step reporting a hundred million tokens on one side -- it would ellipse
 * again, and there is no width at which that is not true of an unbounded
 * figure. `.node-id` is the other claimant and is not close: the widest step
 * id in any fixture is `scan-terraform` at 118px, and the widest that also
 * carries the `opens the PR` tag is `synthesis` at 76 + 6 + ~90 = 172px.
 *
 * TWELVE PIXELS WIDER THAN THE 236 IT REPLACES, which is paid for: at 236 the
 * value column was 154px and `21.4k in · 3.2k out` was already clipping. This
 * fixes a defect that predates the transpose rather than trading one away.
 *
 * ---------------------------------------------------------------------------
 * IT IS 255 NOW, AND IT IS COMPUTED RATHER THAN WRITTEN DOWN. Semantic zoom
 * needs a node width per tier, and a ladder of tiers whose top rung is a
 * literal while the rungs below it are derived is a ladder that drifts at the
 * top. So the same arithmetic that sizes the reduced tiers sizes this one, and
 * running it turned up two things the literal had been hiding:
 *
 *   1. THE BORDER WAS NEVER COUNTED. `NODE_CHROME_W` above has the correction
 *      in full: 248 gives 220 of content, not the 224 this docblock claims, so
 *      `99.9k in · 99.9k out` (168.6px into a 166px value column) ellipses
 *      today. That is an F1 truncation -- a prefix of a number standing where
 *      the number was -- in the cell this width exists to protect.
 *   2. THE FIGURES ROW WAS NOT THE WIDEST ROW. `.node-line` carries the state
 *      word AND the duration, and its worst pair is `dead_lettered` beside
 *      `duration unread`: 8 + 8 + 13 chars + 8 + 15 chars at --t-micro is
 *      226.3px, against the figures row's 222.6px. Nothing had measured it,
 *      because the row was introduced by the pass that merged two stacked
 *      12px rows into one and the width had been settled before that.
 *
 *   NODE_CHROME_W ........................................ 28
 *   max(STATE_AND_DUR_ROW_W 226.29, FIGURES_ROW_W 222.57)  226.29
 *                                                         ------
 *                                                          254.29, ceil 255
 *
 * SEVEN PIXELS, AND THEY CHANGE NOTHING ELSE. `STAGE_FITS` is still 3 --
 * floor((1054 - 20 + 28) / (255 + 28)) = floor(1062 / 283) -- and
 * `depCharsPerLine(255)` is still 31, the figure the old literal
 * `DEP_CHARS_PER_LINE` carried. Both of those are now computed from this
 * value, so neither can go stale behind it again, which is the failure mode
 * `DEP_CHARS_PER_LINE`'s own docblock was written to warn about.
 *
 * A LONGER STEP ID OR THE `opens the PR` TAG WIDENS IT FURTHER, per workflow.
 * `nodeWidthAt` takes the steps and returns the larger of this constant and
 * what that workflow's own longest name needs, so a 30-character step id no
 * longer wraps `.node-id` onto a second line -- which, on a card whose height
 * `layoutOf` has already committed to, overlaps the node beneath it.
 */
export const NODE_W = Math.ceil(
  // `SRC_ROW_W` (180.6) never binds; it is here so that it would if it grew.
  NODE_CHROME_W + Math.max(STATE_AND_DUR_ROW_W, FIGURES_ROW_W, SRC_ROW_W),
)

/**
 * The height of a node with NO dependency line.
 *
 * THE SUM OF THE DECLARED ROWS, and it is written out because the last value
 * here was not: 244 was short of what `.node` actually rendered, so the stop
 * control at the foot of a node sat below the box `layoutOf` had drawn. The
 * arithmetic, against the `.node` rules in styles.css §B17:
 *
 *   padding 10px top + 42px bottom .................. 52
 *   border 1px top + 1px bottom ......................  2
 *   .node-id      --t-body / --lh-body (14 x 1.5) .... 21
 *   .node-line    --t-micro / --lh-micro (12 x 1.45) . 17.4
 *   .node-meta    --t-micro / --lh-micro ............. 17.4
 *   .node-nums    8 pad + 4 x (21 + 1) + 3 x 2 ...... 102
 *   .node-src     --t-micro / --lh-micro ............. 17.4
 *   4 x --ctl-s1 gap between the five children ....... 16
 *                                                    -----
 *                                                     245.2
 *
 * 246 is that, rounded up by the one pixel the fractional line boxes need. It
 * was 224 until `.node-src` took the WF-5 source note off the figures' own
 * rows (`SRC_CHARS` has why that row is height rather than width).
 *
 * THE `+ 1` IN THE FIGURES TERM IS THE ABSENCE RULE, AND IT IS IN EVERY ROW
 * NOW. `.node-num dd` declares a TRANSPARENT dashed bottom border and
 * `.is-absent` only recolours it -- §13.5's pattern, applied here because the
 * rule used to be declared only on the absent cells. A cell therefore grew by
 * a pixel at the moment its figure turned out not to have been measured, which
 * is after `layoutOf` has committed to the card's height and while the attempt
 * read is landing. Up to 4px a node, and it was measured as 2px of lost
 * clearance under one node's dependency list.
 *
 * TWENTY PIXELS CAME OFF 240, AND BOTH REASONS ARE STRUCTURAL RATHER THAN
 * TUNED. `.node-links` was a whole row (17.4 + its 4px gap) and it is gone:
 * the node itself is the link now. `.node-nums`'s 1px top rule is gone under
 * design-system.md §13.3 -- a repeated block inside a panel is separated by
 * nothing, and the `--ctl-s2` of padding above it was already doing the work.
 *
 * THE STOP STRIP IS RESERVED, NOT LAID OUT, AND ITS SIZE WAS MEASURED BECAUSE
 * THE LAST FIGURE WAS NOT. `.node-stop` is no longer a flex child of the card;
 * it is an absolutely positioned SIBLING (a `<button>` may not live inside an
 * `<a>`, and the card is the anchor now), so the card reserves the strip with
 * `padding-bottom`. The old arithmetic called that strip 25px -- "4 pad +
 * .stop-btn.inline (13+6+2)". `getBoundingClientRect` on the shipped control
 * says **28**, so the node had been three pixels taller than the box the
 * layout drew for it, and the stop button sat on the card's bottom border. In
 * the rebuilt card the same error showed up as a 4.2px OVERLAP between the
 * stop control and the dependency list above it -- measured, on two nodes, in
 * the browser, because no gate in this repository has a layout engine.
 *
 *   28 (the control) + 10 (its inset from the card's edge, matching the card's
 *   own 10px vertical padding) + 4 (the sheet's separator floor) = 42, less
 *   the 1px border the offset is measured through.
 *
 * THE STRIP IS RESERVED UNCONDITIONALLY, on nodes that have nothing to stop
 * too. A card that grew by 39px the moment its step started running would
 * shove every level beneath it down the page mid-poll; the node is a fixed box
 * and this is part of what makes it one.
 */
export const NODE_H = nodeHeightAt('figures')

/** One wrapped line of the `↑ depends on` list, at --t-micro/--lh-micro:
 *  12 x 1.45 = 17.4, rounded up. It was 16, which under-counted every
 *  wrapped line by 1.4px. */
const DEP_LINE_H = 18
/**
 * Characters of the dependency list that fit on one line inside a node of
 * width `w`.
 *
 * (w - NODE_CHROME_W) / the measured --t-micro advance. At the 255px full tier
 * that is floor(227 / 7.2245) = 31, which is exactly the literal this function
 * replaces -- so the figure is unchanged and the way it is obtained is not.
 *
 * IT USED TO BE A CONSTANT AND THAT IS THE WHOLE REASON IT IS A FUNCTION NOW.
 * Its own docblock warned about this: "IT MOVES WITH NODE_W AND IT HAS TO. It
 * was 29 against a 236px node ... It happened once already, when 34 survived a
 * narrowing to 188px of box." Semantic zoom draws nodes at THREE widths on one
 * canvas, so a single number could not be right for more than one of them, and
 * a list measured against a stale figure is reported short -- which overlaps
 * the node beneath it by exactly the difference.
 *
 * Floored at 1 so a pathologically narrow node still terminates `depLines`.
 */
function depCharsPerLine(w: number): number {
  return Math.max(1, Math.floor((w - NODE_CHROME_W) / monoW(1, T_MICRO)))
}

/**
 * How many lines the `↑ a, b, c` list will actually occupy.
 *
 * IT WORD-WRAPS, AND DIVIDING CHARACTERS BY COLUMNS DOES NOT MODEL THAT. The
 * old figure was `ceil(join(', ').length / DEP_CHARS_PER_LINE)`, which assumes
 * the text repacks across the line break. It does not: the browser breaks at
 * the spaces after the commas and abandons whatever is left at the end of each
 * line. Measured on the fixture, `cold-start, fencing, allornothing,
 * checkpoints, absentzero` is 56 characters -- 1.8 lines of 31, so two by
 * division -- and renders as THREE:
 *
 *   ↑ cold-start, fencing,          22 of 31 used, 9 abandoned
 *   allornothing, checkpoints,      26 of 31 used, 5 abandoned
 *   absentzero
 *
 * That is 18px the card had not reserved, and it was spent in the strip the
 * stop control sits in: `synthesis` overflowed its own content box by 16px,
 * measured in the browser. The defect predates the transpose -- the same list
 * against the old 29-column node divides to two lines as well -- and it was
 * invisible while every node in a workflow took the tallest node's height,
 * because some other node's slack absorbed it. Per-level heights took the
 * slack away, which is how it surfaced.
 *
 * So the wrap is simulated rather than approximated: units are packed
 * greedily, a space between them, the `↑ ` prefix costing two columns of the
 * first line.
 *
 * THE UNITS ARE `depUnits`, PACKED AS GIVEN (WF-19). Each is an inline-block in
 * the rendered line, so the browser now breaks ONLY BETWEEN units -- never at
 * a hyphen inside a step id, and never at a space inside a filename. This
 * function used to split `join(', ')` on spaces, which was right while the
 * browser broke at every space and is wrong now: a filename with a space in it
 * is one box on screen, and counting it as two tokens let its first half fit a
 * line the box does not. A unit longer than a whole line still wraps inside its
 * own box (`.node-dep` keeps `overflow-wrap: anywhere`), so its own lines are
 * counted by division, as before.
 *
 * AND A UNIT LONGER THAN A LINE IS A BOX AS WIDE AS THE LINE, which the first
 * version of this packing missed (#160 review). An inline-block is
 * shrink-to-fit, and `overflow-wrap: anywhere` puts its minimum at one
 * character, so a unit whose text is wider than the line takes exactly the
 * line's width: it cannot sit after `↑ ` or after any unit before it, and no
 * unit after it can sit on its last line. This counted the text as flowing on
 * -- the next unit sharing the remainder of the long one's last line, and a
 * long FIRST unit fitting after the arrow -- and came out a line short in
 * both cases, 18px of dependency text in the stop strip.
 */
function depLines(units: readonly string[], perLine: number): number {
  // Empty units are dropped rather than counted as a column; `depUnits` makes
  // none, and a unit with no text draws no box.
  const tokens = units.filter((t) => t !== '')
  let lines = 1
  let used = 2 // the "↑ " prefix, so no line this list draws is ever empty
  let first = true
  for (const tok of tokens) {
    if (tok.length > perLine) {
      // A box the width of the line: it starts a line of its own -- the one
      // it is on always holds something, if only the arrow -- wraps inside
      // itself, and fills its last line, so whatever follows starts another.
      lines += Math.ceil(tok.length / perLine)
      used = perLine
    } else {
      // The space before a unit that moves to a new line collapses at the end
      // of the old one, so a unit that wraps costs only its own length there.
      const need = (first ? 0 : 1) + tok.length
      if (used + need <= perLine) {
        used += need
      } else {
        lines += 1
        used = tok.length
      }
    }
    first = false
  }
  return lines
}

/**
 * How tall THIS node will actually render.
 *
 * The layout positions nodes absolutely, so a node that renders taller than
 * the figure the layout used overlaps the node beneath it and every edge
 * endpoint on that band points at the wrong place. `minHeight` on the
 * element hid that: the node grew and nothing else moved. So the variable part
 * -- the dependency list, which wraps and is never truncated -- is measured
 * here, and the element is given this exact height rather than a floor.
 */
export function heightOf(
  step: WorkflowStep,
  tier: ZoomTier = 'figures',
  width: number = NODE_W,
): number {
  if (step.depends_on.length === 0) return nodeHeightAt(tier)
  // `+ GAP`: the dependency list is ONE MORE flex child, so it costs one more
  // `--ctl-s1` between itself and the row above as well as its own lines. The
  // gap was missing, which is 4px of overlap on every node that has parents --
  // and every node in a graph except the roots has parents.
  //
  // THE DEPENDENCY LIST IS NOT A ZOOM FIELD, and that is deliberate. Every
  // other field on this card is dropped at some tier; this one is the only
  // complete statement of the graph in text and the screen-reader route to it,
  // and it names parents this workflow does not contain -- the case that draws
  // no edge at all. It also costs only HEIGHT, and height is not the axis this
  // canvas has run out of. So it survives every tier, and it wraps at whatever
  // width the tier gave the card.
  //
  // `depUnits`, NOT `depends_on`. The line names the file a data edge stages
  // (`plan (plan.md)`), and a height counted from the bare ids would be short by
  // every line the filenames wrap onto -- the overlap defect this function
  // exists to prevent, arriving through the one field that just got longer.
  // And the UNITS rather than the joined text, because they are what the
  // browser keeps whole (WF-19): `StepNode` draws exactly this list.
  return (
    nodeHeightAt(tier) +
    GAP +
    depLines(
      depUnits(step).map((u) => u.text),
      depCharsPerLine(width),
    ) *
      DEP_LINE_H
  )
}

// ---------------------------------------------------------------------------
// Data edges -- which dependencies carry a file, and whether it arrived
// ---------------------------------------------------------------------------
//
// TWO KINDS OF EDGE, AND THE GRAPH DREW THEM ALIKE. `depends_on` is ORDERING:
// the child may not start until the parent succeeded. `input_from` is DATA: the
// child also gets a named artifact out of the parent's prefix, staged into its
// workspace before the agent runs. The API refuses an `input_from` whose source
// is not also a `depends_on` (validation.py, check 5), so every data edge is an
// ordering edge that additionally carries a file -- a KIND of edge, not a
// second edge. viz #1 asks for the style to distinguish them; one mark per pair
// is unchanged.
//
// AND A THIRD FACT, WHICH IS NOT THE SECOND ONE. `input_from` is what the
// submission DECLARED. `result_summary.staged_inputs` is what the worker says
// actually LANDED (viz #9: "edges drawn back into viz #1, one per staged
// file"). The two come apart exactly while the child is running -- staging
// happens at start, the report is written at finish -- and a graph that drew a
// declared file as a delivered one would be claiming a transfer nobody has
// reported.

/**
 * The artifact `child` declared it stages from `parentId`, or null.
 *
 * DEFENSIVE ABOUT THE SHAPE, because this field has been wrong in this client
 * before: it was typed and fixtured as a bare string against a server that has
 * only ever sent a map. `Object.hasOwn` so a step id that happens to spell a
 * prototype member (`constructor`) reads as "declared nothing" rather than as a
 * function.
 */
export function inputFileOf(child: WorkflowStep, parentId: string): string | null {
  const map: unknown = child.input_from
  if (map === null || typeof map !== 'object') return null
  if (!Object.hasOwn(map, parentId)) return null
  const file = (map as Record<string, unknown>)[parentId]
  return typeof file === 'string' && file !== '' ? file : null
}

/** One entry of a node's `↑` line: the parent, and the file it hands over. */
export interface DepItem {
  readonly parent: string
  readonly file: string | null
  /** Exactly what the node prints for this entry, and what `heightOf` counts. */
  readonly text: string
}

/**
 * The dependency line, entry by entry.
 *
 * `plan (plan.md)` for a data edge and `plan` for an ordering one. THE
 * PARENTHETICAL IS THE HOUSE FORM FOR A QUALIFIER (help-density.md §3, "a
 * parenthetical ... cannot be separated from the figures under it"), and it
 * keeps the line the complete statement of the graph in text that it has always
 * been: a reader who cannot see the edge styles -- a screen reader, a greyscale
 * screenshot -- still learns which dependencies carry a file and which file.
 *
 * ONE SOURCE FOR THE RENDERED TEXT AND THE MEASURED HEIGHT. `StepNode` prints
 * `text` and `heightOf` wraps `text`; two spellings would be two numbers.
 */
export function depItems(step: WorkflowStep): DepItem[] {
  return step.depends_on.map((parent) => {
    const file = inputFileOf(step, parent)
    return { parent, file, text: file === null ? parent : `${parent} (${file})` }
  })
}

/**
 * One unbreakable piece of the dependency line: a parent id, or the `(file)`
 * that parent hands over.
 *
 * WHY THE LINE IS DRAWN IN UNITS (WF-19, epic #83). The line wrapped wherever
 * the browser liked, and `overflow-wrap: anywhere` plus the UA's own break
 * opportunities put the breaks at HYPHENS: `check-` / `3`, `(cc-` /
 * `checkpoint.md`. A step id split across two lines is two things a reader has
 * to rejoin, and a copied half is not an id. Each unit is now an inline-block
 * (`.node-dep-item`), which a line may move to the next line but not split --
 * and one longer than a whole line still wraps inside its own box, so nothing
 * is ever truncated (the pinned `.node-dep` rule is untouched).
 *
 * THE SEPARATING COMMA LIVES INSIDE THE UNIT IT FOLLOWS, so `plan (plan.md),
 * fencing` is `plan`, `(plan.md),`, `fencing`: a comma never starts a line.
 *
 * ONE LIST FOR THE MARKUP AND THE HEIGHT. `StepNode` draws these units and
 * `heightOf` packs these units, so the two cannot disagree about where a line
 * breaks. That is why the height no longer splits the joined text on spaces: a
 * filename is not pattern-checked by the API and may contain a space, and a
 * model that counted two tokens where the browser now keeps one box would put
 * the height out of step with the render again.
 */
export interface DepUnit {
  readonly parent: string
  readonly kind: 'parent' | 'file'
  /** The file, for a `file` unit; null for a parent id. */
  readonly file: string | null
  /** Exactly what the unit prints, trailing comma included. */
  readonly text: string
}

export function depUnits(step: WorkflowStep): DepUnit[] {
  const items = depItems(step)
  const units: DepUnit[] = []
  items.forEach((d, i) => {
    const comma = i < items.length - 1 ? ',' : ''
    if (d.file === null) {
      units.push({ parent: d.parent, kind: 'parent', file: null, text: `${d.parent}${comma}` })
      return
    }
    units.push({ parent: d.parent, kind: 'parent', file: null, text: d.parent })
    units.push({ parent: d.parent, kind: 'file', file: d.file, text: `(${d.file})${comma}` })
  })
  return units
}

/**
 * Whether a declared file has arrived, as far as anything has said.
 *
 * `staged` is the only arm that is a report. The four `declared` reasons are
 * four different kinds of not-yet and each sends a reader somewhere different:
 *
 *   not-started   the child has no task: nothing could have been staged.
 *   unread        the child's task was not in the task read: nobody looked.
 *   in-flight     the child is live or waiting: it staged at start (or will),
 *                 and `result_summary` is written at FINISH. Not "not staged".
 *   not-reported  the child is terminal and its result lists no such file --
 *                 an older worker, or an attempt that failed before staging.
 *                 This reader cannot tell which, and says so.
 *   unreadable    the child's result lists staged entries this reader could
 *                 not read, and this file is not among the ones it could. It
 *                 may BE one of those entries. "Lists no such file" would be
 *                 a claim the result does not make -- the table counted the
 *                 entry while the edge denied it, which is the defect this
 *                 arm exists to end.
 */
export type DeclaredWhy = 'not-started' | 'unread' | 'in-flight' | 'not-reported' | 'unreadable'

/**
 * Which kind of not-yet a declared, unreported file is, for a step in `state`
 * whose result reported `staged`. ONE RULE for the graph, the table and the
 * agent run, so the three cannot word one file three ways.
 *
 * `unreadable` outranks the state: a live step has no result to be unreadable,
 * and a finished one whose result could not all be read cannot be said to list
 * "no such file".
 */
export function declaredWhy(state: StepState, staged: StagedInputs | null): DeclaredWhy {
  if (state.kind === 'unstarted') return 'not-started'
  if (state.kind === 'unknown') return 'unread'
  if (staged?.kind === 'reported' && staged.malformed > 0) return 'unreadable'
  return LIVE_OR_WAITING(state.state) ? 'in-flight' : 'not-reported'
}

export type InputProvenance =
  | {
      readonly kind: 'staged'
      readonly file: string
      readonly bytes: number | null
      readonly fromCheckpoint: boolean
    }
  | { readonly kind: 'declared'; readonly file: string; readonly why: DeclaredWhy }

/** An edge's kind, for the renderer: ordering only, or a file and its state. */
export type EdgeProvenance = { readonly kind: 'order' } | InputProvenance

/**
 * A staged file no edge in this graph can carry.
 *
 * `validation.py` makes every `input_from` source a `depends_on` of the same
 * step and `service.py` derives the task's declaration from it, so for a
 * workflow step these should not exist. They are drawn anyway -- as a mark on
 * the card and a row in the table -- because a file that landed in a workspace
 * and appears nowhere on screen is the silent-drop this console refuses, and
 * because viz #9 names the case that DOES occur: an entry with no `task_id`
 * came from the submission.
 */
export interface StrayInput {
  readonly file: string
  readonly bytes: number | null
  readonly source:
    | { readonly kind: 'submission' }
    /** An upstream task that is not any step of this workflow. */
    | { readonly kind: 'outside'; readonly taskId: string }
    /** A step of this workflow that the child did not declare a file from. */
    | { readonly kind: 'undeclared'; readonly stepId: string }
}

export interface StepInputs {
  /** One per `input_from` entry, keyed by the upstream STEP id. */
  readonly declared: ReadonlyMap<string, InputProvenance>
  readonly stray: readonly StrayInput[]
  /** Entries in the result that could not be read as a file. Counted, not dropped. */
  readonly malformed: number
}

const LIVE_OR_WAITING = (state: TaskState) => !TERMINAL_STATES.has(state)

/**
 * Every step's inputs: what it declared, what arrived, and what arrived that
 * nothing declared.
 *
 * Takes the WHOLE workflow because the join runs through it: a staged entry
 * names an upstream TASK, and only the workflow's steps say which step that
 * task belongs to.
 */
export function inputsByStep(
  steps: readonly WorkflowStep[],
  taskById: ReadonlyMap<string, Task> | null,
): Map<string, StepInputs> {
  const stepOfTask = new Map<string, string>()
  for (const s of steps) if (s.task_id) stepOfTask.set(s.task_id, s.step_id)

  const out = new Map<string, StepInputs>()
  for (const step of steps) {
    const state = stepState(step, taskById)
    const map: unknown = step.input_from
    // IN `depends_on` ORDER, NOT THE MAP'S. `input_from` arrives as a JSON
    // object serialised from a Firestore map (`swarm_api/codec.py`), and a
    // Firestore map has no order the client can rely on: the same step's two
    // inputs came back in a different order between two reads, so the table's
    // inputs cell showed a different file first on each refresh -- and the one
    // it pushed out of view was the second input merge-1..4 failed on.
    // `depends_on` is a LIST, it is the order the step was declared in, and
    // every `input_from` key is one of its members (`validate_dag`). A key that
    // somehow is not goes last, by name, so the order is still total.
    const rank = (parent: string) => {
      const i = step.depends_on.indexOf(parent)
      return i === -1 ? Number.MAX_SAFE_INTEGER : i
    }
    const declaredFiles: [string, string][] = (
      map !== null && typeof map === 'object'
        ? Object.entries(map as Record<string, unknown>).flatMap(([parent, file]) =>
            typeof file === 'string' && file !== '' ? [[parent, file] as [string, string]] : [],
          )
        : []
    ).sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b))

    const staged = state.kind === 'state' ? stagedInputsOf(state.task) : null
    // Staged entries, keyed by the upstream STEP they joined to. Anything that
    // does not join, or joins to a step nothing was declared from, is stray.
    const landed = new Map<string, { file: string; bytes: number | null; fromCheckpoint: boolean }>()
    const stray: StrayInput[] = []
    if (staged?.kind === 'reported') {
      const declaredParents = new Set(declaredFiles.map(([p]) => p))
      for (const s of staged.inputs) {
        if (s.upstreamTaskId === null) {
          stray.push({ file: s.filename, bytes: s.bytes, source: { kind: 'submission' } })
          continue
        }
        const parent = stepOfTask.get(s.upstreamTaskId)
        if (parent === undefined) {
          stray.push({
            file: s.filename,
            bytes: s.bytes,
            source: { kind: 'outside', taskId: s.upstreamTaskId },
          })
          continue
        }
        if (!declaredParents.has(parent)) {
          stray.push({ file: s.filename, bytes: s.bytes, source: { kind: 'undeclared', stepId: parent } })
          continue
        }
        landed.set(parent, { file: s.filename, bytes: s.bytes, fromCheckpoint: s.fromCheckpoint })
      }
    }

    const declared = new Map<string, InputProvenance>()
    for (const [parent, file] of declaredFiles) {
      const hit = landed.get(parent)
      if (hit !== undefined) {
        // THE REPORTED NAME, not the declared one. They are the same by
        // construction; if they ever differ, what landed is the fact.
        declared.set(parent, { kind: 'staged', file: hit.file, bytes: hit.bytes, fromCheckpoint: hit.fromCheckpoint })
        continue
      }
      declared.set(parent, { kind: 'declared', file, why: declaredWhy(state, staged) })
    }

    out.set(step.step_id, {
      declared,
      stray,
      malformed: staged?.kind === 'reported' ? staged.malformed : 0,
    })
  }
  return out
}

/** The provenance of one drawn edge. An edge with no declared file is ordering. */
export function edgeProvenance(
  edge: Pick<DagEdge, 'from' | 'to'>,
  inputs: ReadonlyMap<string, StepInputs>,
): EdgeProvenance {
  return inputs.get(edge.to)?.declared.get(edge.from) ?? { kind: 'order' }
}

/** How an edge is DRAWN: ordering only, a file reported staged, or a file
 *  declared and not (yet) reported. The renderer's three styles. */
export type EdgeKind = 'order' | 'staged' | 'declared'

/**
 * The kind every drawn edge is painted as, keyed `${from}->${to}`.
 *
 * NOT ALWAYS THE PAIR'S OWN KIND, and a collapsed stage is why. `layoutOf`
 * attaches every step of a collapsed stage to its band's centre, so the
 * thirteen edges `plan -> impl-1..13` are thirteen marks on ONE path. When each
 * edge was one group of halo-then-stroke, each later group wiped out every
 * earlier one, and the band edge showed the LAST child's kind as the whole
 * stage's: solid when one of thirteen had reported its file, dashed when twelve
 * had. Whichever child happened to be last in the level decided whether the
 * band edge read as data at all. (The canvas now paints every halo before any
 * stroke, which stops a halo erasing an ARROWHEAD; it does not stop thirteen
 * strokes on one path from being read as the topmost one, so this rule stays.)
 *
 * So the edges that share a path are painted as ONE edge, with the WEAKEST
 * claim any member can support:
 *
 *   * `order` only when no member carries a file;
 *   * `staged` only when EVERY member carries a file and every one of them was
 *     reported arriving;
 *   * `declared` otherwise -- some member carries a file and not every member
 *     reported one. Dashed is this canvas's "not a measurement", and that is
 *     what a stage-wide arrival claim is until every child has made it.
 *
 * Every member gets that kind, so the painting order no longer matters and the
 * one-group-per-dependency guarantee is unchanged. Opening the stage draws the
 * nodes, and each edge then has its own path and its own kind again. Edges
 * between two drawn nodes are groups of one, so this is `edgeProvenance`.
 */
export function edgeKinds(
  layout: Pick<DagLayout, 'edges' | 'bands'>,
  inputs: ReadonlyMap<string, StepInputs>,
): Map<string, EdgeKind> {
  // Which collapsed band each step is drawn as, if any.
  const bandOf = new Map<string, number>()
  for (const b of layout.bands) {
    if (b.expanded) continue
    for (const s of b.steps) bandOf.set(s.step_id, b.level)
  }
  const end = (id: string) => {
    const band = bandOf.get(id)
    return band === undefined ? `step:${id}` : `band:${band}`
  }
  const groups = new Map<string, EdgeKind[]>()
  const groupOf = new Map<string, string>()
  for (const e of layout.edges) {
    const g = `${end(e.from)}->${end(e.to)}`
    groupOf.set(`${e.from}->${e.to}`, g)
    const kinds = groups.get(g) ?? []
    kinds.push(edgeProvenance(e, inputs).kind)
    groups.set(g, kinds)
  }
  const drawn = new Map<string, EdgeKind>()
  for (const [g, kinds] of groups) {
    const kind: EdgeKind = kinds.every((k) => k === 'order')
      ? 'order'
      : kinds.every((k) => k === 'staged')
        ? 'staged'
        : 'declared'
    drawn.set(g, kind)
  }
  const out = new Map<string, EdgeKind>()
  for (const [pair, g] of groupOf) out.set(pair, drawn.get(g) ?? 'order')
  return out
}

// ---------------------------------------------------------------------------
// One task's inputs, for the agent run (redesign-v2 Panel 3)
// ---------------------------------------------------------------------------

/** One file a task declared, was given, or both. */
export interface TaskInputRow {
  readonly file: string
  /** Where it came from: an upstream task, or the submission itself. */
  readonly from: { readonly kind: 'task'; readonly taskId: string } | { readonly kind: 'submission' }
  /** True when the task's own declaration names this file. A staged file that
   *  nothing declared is listed too, and says so. */
  readonly declared: boolean
  readonly arrival: InputProvenance
}

export interface TaskInputs {
  readonly rows: readonly TaskInputRow[]
  /** Staged entries that could not be read as a file. Counted, not dropped. */
  readonly malformed: number
}

/**
 * What one task declared it stages and what its result says arrived, joined.
 *
 * THE SAME JOIN `inputsByStep` MAKES FOR A WORKFLOW, from the task's side: the
 * declaration here is `metadata.input_from`, which `service.py` writes keyed by
 * the upstream TASK id, so nothing needs the workflow to resolve it. The
 * not-yet words come from `declaredWhy`, so a file reads the same on the run as
 * on the board it was opened from.
 *
 * READ DEFENSIVELY, because `metadata` is untyped on a frozen type: an entry
 * that is not a non-empty string is not a declaration, and a declaration that is
 * not a plain object declares nothing.
 */
export function taskInputsOf(task: Task): TaskInputs {
  const meta: unknown = task.metadata
  const raw: unknown =
    meta !== null && typeof meta === 'object' && !Array.isArray(meta)
      ? (meta as Record<string, unknown>)['input_from']
      : undefined
  const declared: [string, string][] =
    raw !== null && typeof raw === 'object' && !Array.isArray(raw)
      ? Object.entries(raw as Record<string, unknown>).flatMap(([taskId, file]) =>
          typeof file === 'string' && file !== '' && taskId !== '' ? [[taskId, file] as [string, string]] : [],
        )
      : []

  const staged = stagedInputsOf(task)
  const reported = staged.kind === 'reported' ? staged.inputs : []
  const used = new Set<number>()
  const rows: TaskInputRow[] = []
  const why = declaredWhy({ kind: 'state', state: task.state, task }, staged)

  for (const [taskId, file] of declared) {
    const idx = reported.findIndex((s, i) => !used.has(i) && s.upstreamTaskId === taskId)
    const hit = idx < 0 ? undefined : reported[idx]
    if (hit !== undefined) {
      used.add(idx)
      rows.push({
        // THE REPORTED NAME, as `inputsByStep` does: what landed is the fact.
        file: hit.filename,
        from: { kind: 'task', taskId },
        declared: true,
        arrival: { kind: 'staged', file: hit.filename, bytes: hit.bytes, fromCheckpoint: hit.fromCheckpoint },
      })
      continue
    }
    rows.push({ file, from: { kind: 'task', taskId }, declared: true, arrival: { kind: 'declared', file, why } })
  }
  reported.forEach((s, i) => {
    if (used.has(i)) return
    rows.push({
      file: s.filename,
      from: s.upstreamTaskId === null ? { kind: 'submission' } : { kind: 'task', taskId: s.upstreamTaskId },
      declared: false,
      arrival: { kind: 'staged', file: s.filename, bytes: s.bytes, fromCheckpoint: s.fromCheckpoint },
    })
  })
  return { rows, malformed: staged.kind === 'reported' ? staged.malformed : 0 }
}

/** The words for a declared-but-not-reported file, and why. One place, so the
 *  edge, the dependency line and the table cannot say it three ways. */
export const DECLARED_WORDS: Readonly<Record<DeclaredWhy, { text: string; note: string }>> = {
  'not-started': {
    text: 'not staged yet',
    note: 'The step has no task yet, so nothing has been staged into it.',
  },
  unread: {
    text: 'staging unread',
    note: 'The step’s task was not in the task read, so whether the file arrived is unknown.',
  },
  'in-flight': {
    text: 'reported at finish',
    note: 'Inputs are staged when the step starts and reported when the step finishes. This one has not finished, so nothing has reported the file yet. That is not the same as the file being missing.',
  },
  'not-reported': {
    text: 'not reported',
    note: 'The step has finished and its result lists no such file. Either the worker predates the report or the attempt ended before staging; this read cannot tell which.',
  },
  unreadable: {
    text: 'report unreadable',
    note: 'The step’s result lists staged-input entries this reader could not read, and this file is not among the ones it could. It may be one of them; this read cannot tell.',
  },
}

/**
 * The gap BETWEEN TWO LEVELS -- down the flow, because the flow runs down.
 *
 * This is where the graph's air is, and it is the only distance on the canvas
 * a reader parses as "and then". 56 is two `--ctl-s5`, the scale's one large
 * break, taken twice because a level boundary is the biggest change of subject
 * this canvas has. It also has to hold the edge: `edgePath` bends with
 * `k = max(24, (y2 - y1) / 2)`, so 56 gives a 28px control offset and a curve
 * that reads as a curve rather than a kink, plus room for the 7px arrowhead.
 *
 * IT WAS `COL_GAP = 68`, AND IT WAS HORIZONTAL. 68 was tuned for a left-to-
 * right run, where the gap also had to separate two 236px columns of text.
 */
export const LEVEL_GAP = 56

/**
 * The gap BETWEEN TWO SIBLINGS -- across a level, because siblings sit across.
 *
 * `--ctl-s5`. It was `ROW_GAP = 20` when siblings stacked vertically, and 20
 * does not survive the transpose: two NODE_W-wide cards 20px apart read as one
 * striped band, which is design-system.md §13.3's rhythm inversion in its most
 * literal form. 28 is what this sheet puts between two objects.
 */
export const SIB_GAP = 28
export const PAD = 10

// ---------------------------------------------------------------------------
// Stage collapsing -- the transpose's unpaid bill
// ---------------------------------------------------------------------------
//
// THE TRANSPOSE IS CORRECT AND IT DID NOT FIX THIS. Levels run down and the
// steps of one level run across, which is what the owner asked for and what the
// comments above argue for at length. The cost that argument names -- "a wide
// fan-out now pushes the canvas sideways ... width is the axis that gives" --
// was accepted on the assumption that a fan is a handful of steps. Measured on
// a live 30-step run (`wf_7e2ee6c3075d43228e5a`, widest stage 13 steps):
//
//   canvas                            1776 x 4134 px
//   visible wrapper                   1138 px
//   nodes clipped                     17 of 30
//   nodes fully off-screen            4
//   vertical scroll for one workflow  4.6 screen-heights
//
// Thirteen nodes at NODE_W is 3,651px of band (it read 3,560 when NODE_W was
// the literal 248; the chrome correction in `NODE_CHROME_W` moved it to 255).
// There is no monitor that fits it and no amount of scrolling that makes a
// graph you cannot see at once into a graph. Scaling is still refused for the reason the header gives -- a node
// scaled to fit is texture, not a node -- so the thing that has to give is
// HOW MANY NODES ARE DRAWN AT ALL.
//
// So a stage wider than the column is drawn as ONE BAND that says what is in
// it by state, and expands to the full row on click. The graph then keeps the
// SHAPE of the workflow (1 -> 13 -> 1 reads as a fan and a join at a glance)
// instead of the shape of its widest moment.

/**
 * How much horizontal room the canvas element actually gets, in CSS pixels.
 *
 * MEASURED, NOT CHOSEN. 1138px is the width of `.wf-canvas-wrap`'s client box
 * at the 1440x900 viewport the overflow inventory was taken at
 * (docs/audits/2026-09-23/overflow-inventory.md). Out of that the graph pays:
 *
 *   .wf-canvas-wrap client width ............. 1138
 *   - .wf-levels, the sticky level rail ......   72
 *   - .wf-graph's `gap: var(--ctl-s3)` .......   12
 *                                             ----
 *   .wf-canvas ................................ 1054
 *
 * IT IS A REFERENCE, NOT A MAXIMUM. The wrapper is fluid -- `main.work` has no
 * max-width, which is the whole point of B19 -- so a 1920 monitor gives the
 * canvas more and a phone gives it far less. A threshold has to be one number,
 * and the honest one is the width of the laptop the product is actually used
 * on and the audit was actually measured at. Above it nothing is lost: the
 * bands are still there and still expand. Below it the canvas scrolls, exactly
 * as it does today.
 *
 * THE 72 AND THE 12 ARE IN styles.css AND MUST STAY IN STEP. `.wf-levels`
 * declares `width: 72px` and `.wf-graph` declares `gap: var(--ctl-s3)`; widen
 * either and this figure is wrong in the direction that clips.
 */
export const CANVAS_COLUMN = 1054

/**
 * The widest stage that is drawn as real nodes, in steps.
 *
 * DERIVED FROM THE NODE AND THE COLUMN, so it cannot go stale the way
 * `DEP_CHARS_PER_LINE` did when NODE_W moved: a stage of `n` occupies
 * `n * nodeW + (n - 1) * SIB_GAP`, the canvas spends `PAD` on each side, and
 * this is the largest `n` that still fits `CANVAS_COLUMN`. At the full tier's
 * 255px and SIB_GAP 28 that is
 *
 *   floor((1054 - 20 + 28) / (255 + 28)) = floor(1062 / 283) = 3
 *
 * -- and the fourth node of a stage ends 70px past the right edge of the
 * column, which is why the audit found 17 of 30 nodes clipped rather than
 * only the 13-wide stage's. FOUR IS ALREADY TOO WIDE AT THE FULL TIER; that is
 * a measurement, not a preference, and it is the reason this number is
 * computed here instead of being written down as a taste.
 *
 * IT TAKES A WIDTH NOW RATHER THAN READING NODE_W, because semantic zoom draws
 * nodes at three widths on one canvas. At the `details` tier's 144px the same
 * column holds floor(1062 / 172) = 6, which is what lets a six-step fan be
 * drawn in full instead of being collapsed into a band.
 *
 * Floored at 1: a threshold of 0 would collapse a chain, whose whole property
 * is that it fits anything.
 */
export function stageFitsAt(nodeW: number): number {
  return Math.max(1, Math.floor((CANVAS_COLUMN - PAD * 2 + SIB_GAP) / (nodeW + SIB_GAP)))
}

/**
 * The threshold at the FULL tier, which is the one the collapsing pass shipped
 * and the one every fixture and every comment above is written against.
 *
 * IT IS NO LONGER THE ONLY THRESHOLD, because there is no longer only one node
 * width. `stageFitsAt` is the general form and `layoutOf` calls it with the
 * width the resolved tier actually gave the cards -- a `details` node at 144px
 * fits six across the same column that holds three at 255. A stage that still
 * does not fit the SMALLEST tier keeps its band; semantic zoom is what happens
 * instead of collapsing, not instead of the band.
 */
export const STAGE_FITS = stageFitsAt(NODE_W)

/**
 * Whether this stage is wide enough to be drawn as a band, at a given node
 * width.
 *
 * The default is the full tier, so every existing caller and every existing
 * assertion means exactly what it meant before semantic zoom.
 */
export function stageIsWide(stepCount: number, nodeW: number = NODE_W): boolean {
  return stepCount > stageFitsAt(nodeW)
}

/**
 * The band's height, summed from the rows it declares rather than guessed --
 * the same discipline NODE_H had to be rewritten to, after 244 turned out to
 * be three pixels short of what `.node` rendered.
 *
 * Against the `.wf-band` rule in styles.css:
 *
 *   padding var(--ctl-s3) top + bottom ......... 24
 *   border 1px top + 1px bottom ................  2
 *   one line at --t-meta / --lh-meta (13 x 1.45) 18.85
 *                                              -----
 *                                               44.85
 *
 * 45 is that, rounded up by the pixel the fractional line box needs. It also
 * clears the 44px touch target design-system.md §7.2 asks of a control, which
 * is not a coincidence -- `--ctl-s3` was chosen over `--ctl-s2` for exactly
 * that, and a smaller step here would put a tappable summary under the floor.
 *
 * THE COUNTS DO NOT WRAP, so this height does not depend on how many states a
 * stage happens to be in. `.wf-band-counts` is `nowrap` with `overflow:
 * hidden`, and `stageCensus` orders failures first so the count that may not
 * be lost is never the one clipped. The complete census is the band's
 * accessible name either way.
 */
export const BAND_H = 45

/**
 * The gap between a band and the nodes it heads, when the stage is expanded.
 *
 * `--ctl-s3`, NOT `LEVEL_GAP`. A band and the stage under it are ONE subject --
 * the band is that stage's heading -- and LEVEL_GAP is reserved for the one
 * distance on this canvas a reader parses as "and then". Spending the level
 * break here would make an expanded stage read as two levels.
 */
export const BAND_GAP = 12

/**
 * The narrowest a band is drawn, and why the canvas width stops depending on
 * which stages happen to be collapsed.
 *
 * It is `levelWidth(stageFitsAt(nodeW))` -- the widest a stage can be at this
 * tier while still being drawn as nodes. Take it and two properties fall out
 * for free:
 *
 *  * a collapsed stage never makes the canvas wider than an uncollapsed one
 *    already could, so the canvas with every wide stage collapsed is exactly
 *    `PAD * 2 + bandMinWidth(nodeW)` -- 841px at the full tier, inside
 *    CANVAS_COLUMN with 213px to spare;
 *  * the band spans the full width of the canvas rather than floating at some
 *    fraction of it, so it reads as a band across the flow instead of as an
 *    unusually wide node.
 *
 * IT IS A FUNCTION OF THE TIER'S WIDTH AND IT HAS TO BE. Held at the full
 * tier's 821px while the nodes around it are 138px wide, a band would be five
 * times the width of the stage it stands in for -- and the collapsed canvas
 * would be WIDER than the drawn one, which is the single thing collapsing
 * exists not to do.
 */
export function bandMinWidth(nodeW: number): number {
  const fits = stageFitsAt(nodeW)
  return nodeW * fits + SIB_GAP * (fits - 1)
}

/** The same figure at the full tier: 3 x 255 + 2 x 28 = 821. */
export const BAND_MIN_W = bandMinWidth(NODE_W)

/**
 * Which tier to draw this workflow at, given the column the canvas has.
 *
 * THE RULE IS "ZOOM RATHER THAN COLLAPSE, AND COLLAPSE RATHER THAN LIE."
 * Semantic zoom exists to stop a five-wide fan being replaced by a band it has
 * to be clicked out of; it does not exist to make a 13-wide fan fit, because
 * nothing makes a 13-wide fan fit. Thirteen nodes need 13 x (w + 28) of column
 * and the smallest tier a legible node can be drawn at is about 138px, which is
 * 2,150px against 1,054. So:
 *
 *   * take the WIDEST tier whose nodes let the widest stage be drawn in full;
 *   * if no tier does, stay at `figures` and let the band do its job. Zooming
 *     out would then cost every node its figures AND still collapse the stage,
 *     which is paying twice for nothing.
 *
 * `column` is `CANVAS_COLUMN` by default -- the measured 1,054px the canvas
 * gets at the 1440x900 viewport the audit was taken at. It is a REFERENCE and
 * not a maximum, exactly as `STAGE_FITS` already treats it: a 1920 monitor has
 * more and a phone has far less. That is what the reader-facing override in
 * `Workflows.tsx` is for -- somebody on a wide monitor can ask for `figures`
 * back and get it, and somebody on a phone can ask for `names`.
 */
export function autoTier(
  steps: readonly WorkflowStep[],
  column: number = CANVAS_COLUMN,
): ZoomTier {
  const levels = levelsOf(steps)
  if (levels.length === 0) return 'figures'
  const widest = Math.max(...levels.map((l) => l.length))
  for (const tier of ZOOM_TIERS) {
    const w = nodeWidthAt(tier, steps)
    if (PAD * 2 + widest * w + (widest - 1) * SIB_GAP <= column) return tier
  }
  // Nothing fits. `figures` keeps every field on the nodes that ARE drawn and
  // leaves the band to carry the stage that is not.
  return 'figures'
}

export interface DagNode {
  /** This node's rendered height, from `heightOf`. */
  readonly h: number
  readonly step: WorkflowStep
  /** Dependency level, counting from the roots. It drives Y. */
  readonly level: number
  /** Position within the level, left to right. It drives X. */
  readonly pos: number
  readonly x: number
  readonly y: number
}

export interface DagEdge {
  readonly from: string
  readonly to: string
  /**
   * The artifact the child DECLARED it stages from this parent (`input_from`),
   * or null for an edge that only orders the two. A property of the steps, so
   * it is computed here with the geometry; whether the file actually ARRIVED
   * needs the task read and is `edgeProvenance`'s question, not this one's.
   */
  readonly file: string | null
  readonly x1: number
  readonly y1: number
  readonly x2: number
  readonly y2: number
  /**
   * The offset lane an edge that SKIPS A LEVEL descends on, or null for an
   * edge between two adjacent levels. See `laneRouter`.
   */
  readonly lane: EdgeLane | null
}

/**
 * Where a skip-level edge runs past the levels between its two ends.
 *
 * `x` is a column free of every card and band on those levels, and of every
 * other lane over them; `top` is the top of the first level it passes and
 * `bottom` the bottom of the last. `edgePath` curves into the lane through the
 * gap under the parent's level, runs straight down it, and curves out through
 * the gap over the child's -- so the only part of the edge that crosses a
 * level is a line nothing is drawn on.
 */
export interface EdgeLane {
  readonly x: number
  readonly top: number
  readonly bottom: number
}

/**
 * A stage drawn as one band instead of as its steps.
 *
 * It carries its STEPS, not a count, because the band's census is computed from
 * the same step objects the nodes would have been drawn from -- one source, so
 * "13 steps · 8 running" and the thirteen nodes behind it cannot come apart.
 */
export interface DagBand {
  readonly level: number
  readonly steps: readonly WorkflowStep[]
  /** True when the reader has opened this stage, so its nodes are drawn too. */
  readonly expanded: boolean
  readonly x: number
  readonly y: number
  readonly w: number
  readonly h: number
}

export interface DagLayout {
  readonly nodes: readonly DagNode[]
  readonly edges: readonly DagEdge[]
  /**
   * One per WIDE stage, collapsed or not. A wide stage keeps its band when it
   * is expanded: the band is the only control that collapses it again, and on
   * a 13-wide stage the census is the only thing on screen that says what the
   * part you have scrolled away from is doing.
   */
  readonly bands: readonly DagBand[]
  /**
   * The top of each level's band, in canvas coordinates.
   *
   * IT IS PUBLISHED RATHER THAN RE-DERIVED. `Workflows.tsx` used to recover it
   * with `layout.nodes.find((n) => n.level === i)?.y`, which was already a
   * workaround for the recomputation that drifted before it -- and it returns
   * the wrong answer twice over now: a collapsed level has no nodes at all, and
   * an expanded wide level's nodes sit BAND_H + BAND_GAP below the level's own
   * top. A caption one band off the level it names is worse than no caption.
   */
  readonly levelTop: readonly number[]
  readonly width: number
  readonly height: number
  readonly levels: readonly (readonly WorkflowStep[])[]
  /** The tier these coordinates were computed at. */
  readonly tier: ZoomTier
  /**
   * The width every node in this layout was given.
   *
   * PUBLISHED RATHER THAN RE-DERIVED, for the reason `levelTop` is: the
   * renderer needs it for `.node-slot`'s inline width and the minimap needs it
   * for its rectangles, and a second call to `nodeWidthAt` in either of those
   * places is a second thing to keep in step with the tier the layout actually
   * used. `levelTop` was recovered by hand once and was wrong twice over.
   */
  readonly nodeW: number
  /**
   * Which levels were too wide to draw at this tier, so are banded.
   *
   * `Workflows.tsx` asked `stageIsWide(level.length)` for this, which is right
   * only at the full tier -- at `details` a six-step stage is drawn in full and
   * that call would still have said it was a band.
   */
  readonly wide: readonly boolean[]
}

// ---------------------------------------------------------------------------
// Edges that skip a level -- routed around the cards between (WF-4)
// ---------------------------------------------------------------------------
//
// AN EDGE THAT SKIPS A LEVEL USED TO RUN STRAIGHT THROUGH IT. Every edge was
// one cubic from its parent's foot to its child's head, so a dependency from
// level 1 to level 3 crossed level 2 wherever the straight line happened to
// fall -- and cards are opaque HTML over the edge layer. Measured on the live
// 30-step run: `synthesis` depends directly on six of a 13-step stage, the
// stage is a band, and all six edges ran down the canvas's middle, collinear
// with the band's edges into `rollup-b` and hidden behind that card. Six real
// dependencies were not on the screen at all. design-system.md §5.3 said an
// edge may pass UNDER a node; the owner's decision (epic #83) is that it may
// not: every dependency is visible.
//
// SO A SKIP-LEVEL EDGE RUNS ON A LANE OF ITS OWN. Between its two ends it
// descends on a column that is clear of every card and band on the levels it
// passes, and clear of every other lane over those levels; it reaches the lane
// through the gap under its parent's level and leaves it through the gap over
// its child's, where nothing is drawn. An edge between adjacent levels is
// unchanged -- it only ever crossed a gap.
//
// WHERE A LANE GOES, in order of preference: in a gutter between two cards on
// every level it passes (the `SIB_GAP` gutters, 28px, hold three lanes), then
// beside the widest of those levels, then past the right edge of everything
// drawn, which widens the canvas. The nearest free lane to the middle of the
// edge's two ends wins, so a lane stays as close as it can to where the
// straight line was. Edges that SHARE BOTH ENDS -- every member of a collapsed
// band is drawn from the band -- share one lane, because they are drawn as one
// path and `edgeKinds` paints them as one edge.

/** Clear space between a lane and the side of any card or band it passes.
 *  The edge's halo is 5px wide (styles.css `.wf-edge-halo`), so 8px leaves a
 *  visible 5.5px of canvas between the line and the card. */
export const LANE_CLEAR = 8

/** How far apart two lanes over the same levels are drawn: the halo's 5px and
 *  one more, so two parallel lanes read as two lines, not one thick one. */
export const LANE_SEP = 6

/** A horizontal stretch a lane may not run through. Mutable only while merged. */
interface Span1D {
  from: number
  to: number
}

/**
 * The lane picker for one layout. `blockedAt(level)` is what is drawn across
 * that level; `levelTop` and `levelBottom` are its vertical extent.
 *
 * Deterministic: the same edges in the same order get the same lanes, because
 * `layoutOf` walks levels, children and parents in the steps' own order.
 */
function laneRouter(opts: {
  blockedAt: (level: number) => readonly Span1D[]
  levelTop: readonly number[]
  levelBottom: readonly number[]
}): (key: string, fromLevel: number, toLevel: number, x1: number, x2: number) => EdgeLane | null {
  const memo = new Map<string, EdgeLane>()
  const taken: { x: number; first: number; last: number }[] = []
  return (key, fromLevel, toLevel, x1, x2) => {
    if (toLevel - fromLevel < 2) return null
    const seen = memo.get(key)
    if (seen !== undefined) return seen
    const first = fromLevel + 1
    const last = toLevel - 1

    // Everything drawn across the levels the lane passes, merged.
    const spans: Span1D[] = []
    for (let l = first; l <= last; l++) for (const s of opts.blockedAt(l)) spans.push({ from: s.from, to: s.to })
    spans.sort((a, b) => a.from - b.from)
    const merged: Span1D[] = []
    for (const s of spans) {
      const top = merged[merged.length - 1]
      if (top !== undefined && s.from <= top.to) top.to = Math.max(top.to, s.to)
      else merged.push(s)
    }

    // Every lane that runs over any of the same levels.
    const others = taken.filter((t) => t.first <= last && t.last >= first)
    const free = (x: number) => others.every((t) => Math.abs(t.x - x) >= LANE_SEP - 0.01)

    // The candidate columns: centred in each gutter and stepping out from the
    // centre, then out from each side of the whole.
    const candidates: number[] = []
    for (let i = 0; i + 1 < merged.length; i++) {
      const lo = merged[i]!.to + LANE_CLEAR
      const hi = merged[i + 1]!.from - LANE_CLEAR
      if (hi < lo) continue
      const mid = (lo + hi) / 2
      candidates.push(mid)
      for (let k = 1; mid - k * LANE_SEP >= lo || mid + k * LANE_SEP <= hi; k++) {
        if (mid - k * LANE_SEP >= lo) candidates.push(mid - k * LANE_SEP)
        if (mid + k * LANE_SEP <= hi) candidates.push(mid + k * LANE_SEP)
      }
    }
    const leftmost = merged[0]?.from ?? (x1 + x2) / 2
    const rightmost = merged[merged.length - 1]?.to ?? (x1 + x2) / 2
    for (let k = 0, x = leftmost - LANE_CLEAR; x >= PAD / 2; k++, x = leftmost - LANE_CLEAR - k * LANE_SEP) {
      candidates.push(x)
    }
    // Past the right edge there is always room: one more column than there are
    // lanes already over these levels is enough to find a free one.
    for (let k = 0; k <= others.length; k++) candidates.push(rightmost + LANE_CLEAR + k * LANE_SEP)

    const target = (x1 + x2) / 2
    let best: number | null = null
    for (const x of candidates) {
      if (!free(x)) continue
      if (best === null || Math.abs(x - target) < Math.abs(best - target) || (Math.abs(x - target) === Math.abs(best - target) && x < best)) {
        best = x
      }
    }
    // Unreachable -- the right-hand columns outnumber the lanes they could
    // collide with -- but a lane is never invented at the target, which is the
    // one place known to be blocked.
    const x = best ?? rightmost + LANE_CLEAR + (others.length + 1) * LANE_SEP
    const lane: EdgeLane = { x, top: opts.levelTop[first]!, bottom: opts.levelBottom[last]! }
    taken.push({ x, first, last })
    memo.set(key, lane)
    return lane
  }
}

/** No stage opened. Hoisted so the default argument is not a fresh allocation
 *  on every render of every graph. */
const NO_STAGES_OPEN: ReadonlySet<number> = new Set<number>()

/**
 * Where every node, band and edge goes.
 *
 * `expandedStages` holds the LEVEL INDEXES the reader has opened. It is a
 * parameter rather than state in here for the same reason `now` is: this file
 * is pure, and the expansion has to survive a re-render anyway, so it is owned
 * by `WorkflowsScreen` above the `key` that the stop-and-reload bumps. A stage
 * that re-collapsed under the reader every poll would be worse than no
 * collapsing at all.
 *
 * Passing nothing draws what this function drew before stage collapsing
 * existed for every workflow whose stages all fit, which is most of them.
 *
 * `tier` is the semantic-zoom level, and it is a PARAMETER for the same reason
 * `expandedStages` is: this file stays pure, and the reader's override of the
 * automatic choice lives above the `key` a stop-and-reload bumps. The default
 * is `figures`, the full node, so a caller that knows nothing about zoom gets
 * exactly the layout this function produced before it existed.
 */
export function layoutOf(
  steps: readonly WorkflowStep[],
  expandedStages: ReadonlySet<number> = NO_STAGES_OPEN,
  tier: ZoomTier = 'figures',
): DagLayout {
  const levels = levelsOf(steps)
  // THE WIDTH EVERY NODE IN THIS LAYOUT GETS, computed once. Every coordinate
  // below is in terms of it rather than of NODE_W, which is now only the full
  // tier's value.
  const nodeW = nodeWidthAt(tier, steps)
  if (levels.length === 0) {
    return {
      nodes: [],
      edges: [],
      bands: [],
      levelTop: [],
      width: PAD * 2,
      height: PAD * 2,
      levels,
      tier,
      nodeW,
      wide: [],
    }
  }

  // ONE HEIGHT PER LEVEL, AND THIS IS WHAT THE TRANSPOSE CHANGED.
  //
  // It was one height for the whole workflow: `max(NODE_H, ...every step)`.
  // That was right on the old axis and is over-constrained on this one. The
  // reason given for it was "a chain must read as one row" -- and left to
  // right, a chain WAS one row, so its steps had to share a top, so they had
  // to share a height. Top to bottom a chain is a column, and two nodes at
  // different levels sharing a height buys nothing: what has to line up is the
  // nodes WITHIN a band, which is what a level is.
  //
  // Measured on the fixture: one step joining five parents wraps its
  // dependency list onto three lines and made all fourteen nodes on the screen
  // that tall, so a root with no dependency line at all carried ~40px of empty
  // card, once per level, all the way down. Per level, only the level that has
  // a long join pays for it.
  //
  // Sizing each node INDIVIDUALLY is still wrong and still for the original
  // reason: the layout positions nodes absolutely, so a node taller than the
  // figure the layout used overlaps its neighbour, and two cards side by side
  // on one band with different heights read as two different kinds of thing.
  //
  // A WIDE STAGE PAYS FOR ITS BAND ON TOP OF ITS NODES, not instead of them.
  // Collapsed it is BAND_H and nothing else. Expanded it is the band, the
  // BAND_GAP under it and then the nodes, because the band is the stage's
  // heading and the only control that closes it again.
  //
  // AND WHAT SEMANTIC ZOOM CHANGED: a stage is wide relative to THE WIDTH THIS
  // TIER GAVE ITS NODES, not to NODE_W. Six `details` nodes at 144px occupy
  // 1,004px of the 1,054px column, so a six-step fan that was a band at the
  // full tier is drawn in full at this one -- which is the entire point of the
  // feature. A stage that does not fit even the smallest tier still gets a
  // band, because there is no width at which thirteen nodes fit a laptop.
  const wide = levels.map((l) => stageIsWide(l.length, nodeW))
  const open = (lvl: number) => wide[lvl] === true && expandedStages.has(lvl)
  const nodesH = (l: readonly WorkflowStep[]) =>
    Math.max(nodeHeightAt(tier), ...l.map((s) => heightOf(s, tier, nodeW)))
  const levelH = levels.map((l, i) => {
    if (!wide[i]) return nodesH(l)
    return open(i) ? BAND_H + BAND_GAP + nodesH(l) : BAND_H
  })
  // The top of each band: every band above it, plus a gap per boundary.
  const levelTop: number[] = []
  for (let i = 0, y = PAD; i < levels.length; i++) {
    levelTop.push(y)
    y += levelH[i]! + LEVEL_GAP
  }
  /** Where a level's NODES start, which is below its band when it has one. */
  const nodesTop = (lvl: number) => levelTop[lvl]! + (wide[lvl] ? BAND_H + BAND_GAP : 0)

  // The width a level occupies: its steps side by side. Every node on this
  // canvas is `nodeW`, so this is arithmetic rather than a measurement.
  const levelWidth = (n: number) => n * nodeW + Math.max(0, n - 1) * SIB_GAP
  // The band's floor, at this tier's width for the same reason everything else
  // here is: `BAND_MIN_W` is `bandMinWidth(NODE_W)`, and a band that wide on a
  // canvas of 138px nodes would make the collapsed form WIDER than the drawn
  // one, which is the one thing collapsing may not do.
  const bandMin = bandMinWidth(nodeW)
  // A COLLAPSED STAGE CONTRIBUTES BAND_MIN_W, NOT ITS STEP COUNT. That is the
  // whole mechanism: the widest level of a 1 -> 13 -> 1 workflow stops being
  // 3,560px and becomes 800px, which fits CANVAS_COLUMN with room to spare.
  // An OPEN stage contributes its real width again and the canvas scrolls,
  // which is the behaviour that shipped and which opening a band asks for.
  const widest = Math.max(
    ...levels.map((l, i) => (wide[i] && !open(i) ? bandMin : levelWidth(l.length))),
  )

  const nodes: DagNode[] = []
  levels.forEach((level, lvl) => {
    // A collapsed stage draws no nodes at all. This is the one line that makes
    // the canvas smaller; everything else above is arithmetic about it.
    if (wide[lvl] && !open(lvl)) return
    // Each LEVEL is centred against the widest one, so a single joining step
    // sits under the middle of the fan it joins rather than at the left edge of
    // an empty band -- which reads as "this belongs to the first branch". It is
    // the same guarantee the old layout made vertically, on the other axis.
    const left = PAD + (widest - levelWidth(level.length)) / 2
    level.forEach((step, pos) => {
      nodes.push({
        step,
        level: lvl,
        pos,
        // The NODES' height, not the level's: an open wide level's height also
        // contains its band and the gap under it, and a card given that figure
        // would paint BAND_H + BAND_GAP past its own bottom edge.
        h: nodesH(level),
        // POSITION drives X: siblings side by side, one band per level.
        x: left + pos * (nodeW + SIB_GAP),
        // LEVEL drives Y: the flow descends.
        y: nodesTop(lvl),
      })
    })
  })

  const bands: DagBand[] = []
  levels.forEach((level, lvl) => {
    if (!wide[lvl]) return
    bands.push({
      level: lvl,
      steps: level,
      expanded: open(lvl),
      // FULL WIDTH, LEFT-ALIGNED WITH THE CANVAS. A band is a statement about
      // a whole stage, so it spans the stage; centring it like a node would
      // make a collapsed 13-wide stage look like one very wide step.
      x: PAD,
      y: levelTop[lvl]!,
      w: widest,
      h: BAND_H,
    })
  })

  // WHERE AN EDGE ATTACHES, for a step that may not have a node.
  //
  // It was a map of nodes, and iterating nodes was how edges were found. That
  // silently drops every dependency into or out of a collapsed stage -- which
  // is 10 of the 10 edges on a 1 -> 5 -> 1 workflow. So the map is keyed by
  // STEP and a collapsed step resolves to its band's box: the band is where
  // that step is on the canvas, so it is where the line belongs.
  const attach = new Map<string, { cx: number; top: number; bottom: number }>()
  for (const n of nodes) {
    attach.set(n.step.step_id, { cx: n.x + nodeW / 2, top: n.y, bottom: n.y + n.h })
  }
  for (const b of bands) {
    if (b.expanded) continue
    for (const step of b.steps) {
      attach.set(step.step_id, { cx: b.x + b.w / 2, top: b.y, bottom: b.y + b.h })
    }
  }

  // WHERE A SKIP-LEVEL EDGE RUNS (WF-4; `laneRouter` says why). What each level
  // draws across the canvas: a wide stage's band spans all of it, open or not;
  // any other level is its cards.
  const levelOfStep = new Map<string, number>()
  levels.forEach((level, lvl) => level.forEach((s) => levelOfStep.set(s.step_id, lvl)))
  const drawnAt: Span1D[][] = levels.map((_, lvl) =>
    wide[lvl]
      ? [{ from: PAD, to: PAD + widest }]
      : nodes.filter((n) => n.level === lvl).map((n) => ({ from: n.x, to: n.x + nodeW })),
  )
  const route = laneRouter({
    blockedAt: (lvl) => drawnAt[lvl] ?? [],
    levelTop,
    levelBottom: levelTop.map((t, lvl) => t + levelH[lvl]!),
  })
  // Edges that share both ends -- every member of a collapsed band is drawn
  // from the band -- are one path, so they are routed as one.
  const endOf = (id: string): string => {
    const lvl = levelOfStep.get(id)
    return lvl !== undefined && wide[lvl] && !open(lvl) ? `band:${lvl}` : `step:${id}`
  }

  const edges: DagEdge[] = []
  for (const level of levels) {
    for (const child of level) {
      const to = attach.get(child.step_id)
      if (!to) continue
      for (const parentId of child.depends_on) {
        const from = attach.get(parentId)
        // A dependency naming a step that is not in this workflow is not drawn.
        // Inventing an edge to nowhere would be a claim about a graph we cannot
        // see; the node keeps the name in its own `↑ depends on` line.
        if (!from) continue
        edges.push({
          from: parentId,
          to: child.step_id,
          file: inputFileOf(child, parentId),
          // AN EDGE LEAVES A PARENT'S BOTTOM EDGE AND ENTERS A CHILD'S TOP EDGE,
          // both at the box's horizontal centre. It was right edge to left edge
          // at the vertical centre; on this axis that would run every line
          // through the cards rather than through the gap between two levels.
          x1: from.cx,
          y1: from.bottom,
          x2: to.cx,
          y2: to.top,
          lane: route(
            `${endOf(parentId)}->${endOf(child.step_id)}`,
            levelOfStep.get(parentId) ?? 0,
            levelOfStep.get(child.step_id) ?? 0,
            from.cx,
            to.cx,
          ),
        })
      }
    }
  }
  // A lane routed past the right edge of everything drawn widens the canvas by
  // exactly what it needs; every other lane is inside the width already.
  const laneRight = Math.max(0, ...edges.map((e) => (e.lane === null ? 0 : e.lane.x + PAD)))

  return {
    nodes,
    edges,
    bands,
    levelTop,
    // WIDTH IS THE WIDEST LEVEL, not the level count: a fan-out is what makes
    // this canvas wide now, and a chain is exactly one node wide at any depth.
    // Or the rightmost skip-level lane, when one had to run past everything.
    width: Math.max(PAD * 2 + widest, laneRight),
    // HEIGHT IS THE SUM OF THE BANDS, not the count times a shared height --
    // the bands are no longer all the same height, so multiplying would either
    // clip the last one or leave a void under it.
    height:
      PAD * 2 + levelH.reduce((t, n) => t + n, 0) + (levels.length - 1) * LEVEL_GAP,
    levels,
    tier,
    nodeW,
    wide,
  }
}

// ---------------------------------------------------------------------------
// What is in a collapsed stage
// ---------------------------------------------------------------------------

/** One bucket of a stage's census: how many steps are in this state, and the
 *  tone that state is drawn in on a node, so the band and the node agree. */
export interface StageCount {
  /**
   * `running`, `succeeded`, `not started`, `not read`.
   *
   * THE NODE'S WORDS WHEREVER THEY SURVIVE A COUNT IN FRONT OF THEM, because a
   * reader should not have to learn a second vocabulary for the collapsed form
   * of the same thing. One does not: `present()` says `state unread` for one
   * step and "13 state unread" is not English. `not read` keeps the distinction
   * from `not started` -- which is the distinction that matters, and the one
   * this console exists to hold -- and reads correctly after a number.
   */
  readonly word: string
  readonly n: number
  readonly tone: Tone | 'unknown'
}

export interface StageCensus {
  readonly steps: number
  /** Ordered for the band: failures first, then by size. */
  readonly counts: readonly StageCount[]
  /** FAILED plus DEAD_LETTERED. Separate from `counts` because the band has to
   *  be able to answer "did anything break here" without scanning a list. */
  readonly failed: number
  readonly cancelled: number
  /** Steps whose task was not in the read. THE CENSUS IS INCOMPLETE BY EXACTLY
   *  THIS MANY, and no claim about failures may be made over them. */
  readonly unread: number
  /** The whole census as one sentence, for the band's accessible name. Nothing
   *  is dropped from it -- it is the route to any bucket the band's own line
   *  had to clip. */
  readonly sentence: string
}

/**
 * WHAT A BAND IS ALLOWED TO HIDE, AND WHAT IT IS NOT.
 *
 * A collapsed stage hides thirteen cards. It may not hide that one of them
 * FAILED: somebody scanning a workflow for what broke must not have to open
 * four bands to find it. So:
 *
 *  * `counts` is ordered with FAILED, DEAD_LETTERED and CANCELLED first, which
 *    is what makes `.wf-band-counts`'s clip safe -- the bucket that may not be
 *    lost is never the one at the end of the line;
 *  * `failed` and `cancelled` are their own fields, so the band can carry a
 *    modifier and be distinguishable at a glance without being read;
 *  * `sentence` is the complete census and is the band's `aria-label`.
 *
 * AND THE HONESTY RULE THE OTHER TWO WOULD OTHERWISE BREAK. A stage whose task
 * states could not be read cannot claim "nothing failed here" -- it does not
 * know. `unread` is counted separately and the sentence says so in those words,
 * because "no step failed" and "we could not tell whether a step failed" are
 * the two things this console exists to keep apart.
 */
export function stageCensus(
  steps: readonly WorkflowStep[],
  taskById: ReadonlyMap<string, Task> | null,
): StageCensus {
  // Insertion order is not the display order -- `rank` below is -- but a Map
  // keeps the iteration deterministic, which matters because the sort is stable
  // and two buckets of equal size must not swap between polls.
  const buckets = new Map<string, { n: number; tone: Tone | 'unknown'; rank: number }>()
  let failed = 0
  let cancelled = 0
  let unread = 0

  for (const step of steps) {
    const state = stepState(step, taskById)
    let word: string
    let tone: Tone | 'unknown'
    let rank: number
    if (state.kind === 'unstarted') {
      // The node says `not started` for this, so the band says `not started`.
      word = 'not started'
      tone = 'wait'
      rank = 3
    } else if (state.kind === 'unknown') {
      word = 'not read'
      tone = 'unknown'
      rank = 2
      unread += 1
    } else {
      word = state.state.toLowerCase()
      tone = stateTone(state.state)
      if (state.state === 'FAILED' || state.state === 'DEAD_LETTERED') failed += 1
      if (state.state === 'CANCELLED') cancelled += 1
      // FOUR RANKS, AND THE ORDER OF THE FIRST TWO IS A JUDGEMENT. A failure
      // outranks a cancellation because a cancellation is something somebody
      // asked for and a failure is not: if only one of them fits on the band's
      // line, the one that needs a human is the one that stays.
      //
      // DEAD_LETTERED is ranked with the failures although `types.ts` records
      // that `finish()` never writes it: the scheduler still lists it in
      // _FAILED_PARENT_STATES, so if one ever reaches a task document it is a
      // failure and must not sort below `succeeded`.
      rank =
        state.state === 'FAILED' || state.state === 'DEAD_LETTERED'
          ? 0
          : state.state === 'CANCELLED'
            ? 1
            : 3
    }
    const seen = buckets.get(word)
    if (seen) seen.n += 1
    else buckets.set(word, { n: 1, tone, rank })
  }

  const counts: StageCount[] = [...buckets.entries()]
    .map(([word, b]) => ({ word, n: b.n, tone: b.tone, rank: b.rank }))
    // Failures, then cancellations, then the unread part of the census, then
    // everything else biggest first. The word breaks the tie so the order
    // cannot change under a poll that happens to visit the steps in a
    // different order.
    .sort((a, b) => a.rank - b.rank || b.n - a.n || a.word.localeCompare(b.word))
    .map(({ word, n, tone }) => ({ word, n, tone }))

  const census = counts.map((c) => `${c.n} ${c.word}`).join(', ')
  // THE CLAUSE THAT MAY NOT BE DROPPED. With nothing failed and nothing unread
  // the band is genuinely clean and says so. With anything unread it says the
  // opposite of clean -- that it cannot tell -- and it says it in the same
  // breath as the count, because a reader who stops after the first clause must
  // not come away with "nothing failed".
  const verdict =
    unread > 0
      ? ` ${unread} step state${unread === 1 ? '' : 's'} could not be read, so this stage cannot be said to be free of failures.`
      : failed + cancelled > 0
        ? ` ${failed} failed and ${cancelled} cancelled.`
        : ' No step in this stage has failed or been cancelled.'

  return {
    steps: steps.length,
    counts,
    failed,
    cancelled,
    unread,
    // NO "click to expand" IN HERE. What the control does is the control's
    // business and it differs by state; this sentence is the CENSUS, and
    // `StageBand` appends the action to it.
    sentence: `${steps.length} step${steps.length === 1 ? '' : 's'} in this stage: ${census}.${verdict}`,
  }
}

/**
 * The cubic the edge is drawn as.
 *
 * VERTICAL CONTROL POINTS, so every edge leaves its parent travelling DOWN and
 * enters its child travelling DOWN. They were horizontal, offset in x, which
 * on this axis would draw a line that sets off sideways out of the bottom of a
 * card -- and an arrowhead, which takes its angle from the path's own tangent,
 * would then point sideways at the moment it meets the child.
 *
 * `k` is half the vertical span, floored at 24 so a sibling-to-sibling edge
 * across one LEVEL_GAP still bows instead of running straight through whatever
 * is between the two cards.
 *
 * AN EDGE THAT SKIPS A LEVEL IS THREE PIECES (WF-4): the same S-curve through
 * the gap under its parent's level, landing on its lane at the top of the
 * first level it passes; a straight run down the lane, which `laneRouter` kept
 * clear of every card, band and other lane; and the S-curve again through the
 * gap over its child. Each curve stays inside its gap -- its control points
 * sit at its own vertical midpoint, so it never rises above its start or
 * drops below its end -- and so no part of the edge is drawn under a card.
 */
export function edgePath(e: DagEdge): string {
  if (e.lane === null) {
    const k = Math.max(24, (e.y2 - e.y1) / 2)
    return `M ${e.x1} ${e.y1} C ${e.x1} ${e.y1 + k}, ${e.x2} ${e.y2 - k}, ${e.x2} ${e.y2}`
  }
  const l = e.lane
  const into = (l.top - e.y1) / 2
  const out = (e.y2 - l.bottom) / 2
  return (
    `M ${e.x1} ${e.y1} C ${e.x1} ${e.y1 + into}, ${l.x} ${l.top - into}, ${l.x} ${l.top}` +
    ` L ${l.x} ${l.bottom}` +
    ` C ${l.x} ${l.bottom + out}, ${e.x2} ${e.y2 - out}, ${e.x2} ${e.y2}`
  )
}

// ---------------------------------------------------------------------------
// Duration -- four states, four sentences, and never a zero for an absence
// ---------------------------------------------------------------------------

/**
 * How long a step has taken, and WHAT KIND of time that is.
 *
 * FIVE ARMS BECAUSE THERE ARE FIVE ANSWERS, and collapsing them into one
 * number is the regression this whole screen is written against:
 *
 *  - `none`    -- there is no duration at all. A step with no task has not
 *                 started; a step whose task was not in the read has timings
 *                 nobody looked at. NEITHER IS `0s`. This arm has no `seconds`
 *                 field, so a renderer cannot print a number for it. A step
 *                 BETWEEN ATTEMPTS is here too, as its state word: its age
 *                 includes the earlier run, so it has no figure (`elapsed()`).
 *  - `queued`  -- it is waiting. Time spent waiting is not time spent working.
 *  - `parked`  -- it waited, stopped, and holds nothing. Also not work.
 *  - `running` -- elapsed so far. Real, and NOT a final duration.
 *  - `ran`     -- start to finish. The only arm that is a duration.
 *
 * A measured zero survives: a step that started and finished inside the same
 * second is `ran 0s`, a digit, because that was measured.
 */
/**
 * The word for a TERMINAL step with no `started_at`, in every view that draws
 * one: the node's duration line, the table's `ran` cell, the timeline's track
 * and the inspector's `took`. One constant, so the four cannot drift apart --
 * which is how the node came to say `ran 3m 0s` beside a table saying
 * `not started`.
 */
export const NEVER_STARTED_WORD = 'never started'

export type StepDuration =
  | { readonly kind: 'none'; readonly text: string; readonly note: string }
  | { readonly kind: 'queued'; readonly seconds: number; readonly text: string; readonly note: string }
  | { readonly kind: 'parked'; readonly seconds: number; readonly text: string; readonly note: string }
  | { readonly kind: 'running'; readonly seconds: number; readonly text: string; readonly note: string }
  | { readonly kind: 'ran'; readonly seconds: number; readonly text: string; readonly note: string }

const at = (v: string | null | undefined): number => (v ? new Date(v).getTime() : NaN)
const secs = (ms: number): number => Math.max(0, Math.round(ms / 1000))

export function stepDuration(state: StepState, now: number): StepDuration {
  if (state.kind === 'unstarted') {
    return {
      kind: 'none',
      text: 'not started',
      note: 'The workflow has not reached this step, so it has no duration. That is an absence, not 0s.',
    }
  }
  if (state.kind === 'unknown') {
    return {
      kind: 'none',
      text: 'duration unread',
      note: `Task ${state.taskId} was not in the task read, so its timings were never looked at. Unknown, not zero.`,
    }
  }

  const task: Task = state.task
  const created = at(task.created_at)
  const started = at(task.started_at)
  const completed = at(task.completed_at)

  // TERMINAL AND NEVER STARTED. `started_at` is written on DISPATCHED ->
  // STARTING, so a task that went terminal without one never got that far --
  // cancelled while it queued is the ordinary case. This line said `ran 3m 0s`
  // for it, measured from SUBMISSION, while the table said `not started` and
  // the timeline drew no run at all: three views, two stories. None of that
  // span was time run, so there is no duration here, and the word is the one
  // the table, the timeline and the inspector print.
  if (TERMINAL_STATES.has(task.state) && !Number.isFinite(started)) {
    return {
      kind: 'none',
      text: NEVER_STARTED_WORD,
      note:
        Number.isFinite(completed) && Number.isFinite(created)
          ? `This step is ${task.state.toLowerCase()} and never started: it ended ${formatDuration(completed - created)} after submission with no start recorded, so none of that time was time run.`
          : `This step is ${task.state.toLowerCase()} and never started: no start time was recorded.`,
    }
  }

  if (Number.isFinite(completed)) {
    const from = Number.isFinite(started) ? started : created
    if (!Number.isFinite(from)) {
      return {
        kind: 'none',
        text: 'duration unread',
        note: 'This step finished, but neither a start time nor a submission time was recorded, so no span can be computed.',
      }
    }
    const s = secs(completed - from)
    return {
      kind: 'ran',
      seconds: s,
      text: `ran ${formatDuration(completed - from)}`,
      note: Number.isFinite(started)
        ? 'Final duration, from the moment it started to the moment it finished.'
        : 'Measured from submission: no start time was recorded, so this span includes the wait before it ran.',
    }
  }

  // Terminal with no completion time. The row went terminal between two polls,
  // or it was cancelled before it ever ran. Either way nothing timed it, and a
  // ticking "running 4m" for a cancelled step would be a live-looking lie.
  if (TERMINAL_STATES.has(task.state)) {
    return {
      kind: 'none',
      text: 'duration unread',
      note: `This step is ${task.state.toLowerCase()} and carries no completion time, so how long it took was never recorded.`,
    }
  }

  if (task.state === 'PARKED') {
    const parked = at(task.updated_at)
    if (!Number.isFinite(parked)) {
      return {
        kind: 'none',
        text: 'parked',
        note: 'Parked: waiting, holding no capacity and doing no work. When it parked was not recorded, so there is no figure.',
      }
    }
    return {
      kind: 'parked',
      seconds: secs(now - parked),
      text: `parked ${formatDuration(now - parked)}`,
      note: 'Time spent parked. A parked step holds no capacity and is doing no work, so this is time waited, never time worked.',
    }
  }

  // RUNNING MEANS STARTING OR RUNNING, NOT "HAS A START TIME". `started_at` is
  // overwritten per attempt (control.py:410-413) and is not cleared when an
  // attempt is reclaimed, so a task that is queued, ready, leased or dispatched
  // for its SECOND attempt still carries the first one's start -- and this line
  // said `running 7m` about a step holding no capacity at all. The timeline and
  // the table do not draw that span as a run, and neither does this (below).
  if (Number.isFinite(started) && (task.state === 'STARTING' || task.state === 'RUNNING')) {
    return {
      kind: 'running',
      seconds: secs(now - started),
      text: `running ${formatDuration(now - started)}`,
      note: 'Elapsed since it started. It has not finished, so this is not a final duration.',
    }
  }

  // BETWEEN ATTEMPTS THERE IS NO FIGURE (#145's rule for `elapsed()`, applied
  // here in the 2026-09-25 consistency sweep). This printed `waiting <age>`
  // from submission, under the word for a wait -- but the age includes the
  // earlier attempt's run, and nothing on the task document records when the
  // state it is in now began (a park, a promote and a lease write no time of
  // their own). Ten minutes of "waiting" for a step that ran for seven of them
  // is the same misreading `running 7m` was, the other way round. So the text
  // is the state word, exactly as `elapsed()` prints it for the same task in
  // the Agents list, and the arm is `none`: it carries no `seconds`, so no
  // renderer can put a number on it and nothing ticks.
  if (Number.isFinite(started) && Number.isFinite(created)) {
    return {
      kind: 'none',
      text: task.state.toLowerCase(),
      note: `Between attempts: ${task.state.toLowerCase()} for another attempt (${task.attempt_count} of ${task.max_attempts} used). An earlier attempt ran and is over, and nothing records when this state began, so there is no figure: the step's age includes that run.`,
    }
  }

  if (Number.isFinite(created)) {
    return {
      kind: 'queued',
      seconds: secs(now - created),
      text: `queued ${formatDuration(now - created)}`,
      note: 'Time spent waiting. Nothing has started.',
    }
  }

  return {
    kind: 'none',
    text: 'duration unread',
    note: 'No submission time was recorded for this step, so nothing about it can be timed.',
  }
}

// ---------------------------------------------------------------------------
// Spend
// ---------------------------------------------------------------------------

/**
 * What this workflow has cost so far, over the steps whose task reported one.
 *
 * `usd === null` MEANS NOTHING REPORTED A COST. It does not mean zero, and the
 * renderer must say "not reported" rather than `$0.00`. A run reported as
 * `0` -- a mock profile does exactly that -- is MEASURED and comes back as the
 * number `0`, so it renders as a digit.
 *
 * `covered` and `joined` travel with the figure so no total can be shown
 * without its coverage. Three of six steps reporting $0.42 does not make the
 * workflow's spend $0.42; it makes it "at least $0.42, from three of six".
 */
export interface WorkflowSpend {
  /** Sum over the steps that reported. Null when none did. */
  readonly usd: number | null
  /** Steps whose task reported a finite cost. */
  readonly covered: number
  /** Of `covered`, the steps whose figure is their result's rather than their attempts'. */
  readonly fromResult: number
  /** Steps whose task was joined at all -- the most that could have reported. */
  readonly joined: number
  readonly steps: number
}

/**
 * What a step's RESULT says it cost, from `result_summary.runner.usage`.
 *
 * THE SECOND SOURCE, AND IT MEANS SOMETHING DIFFERENT FROM THE FIRST (WF-5,
 * epic #83). The attempt documents are the telemetry: one typed `cost_usd` and
 * four token counts per attempt, summed per step by `loadWorkflowUsage` -- for
 * the twelve tasks the board samples. The result summary is what the worker's
 * `finish()` wrote for the attempt that finished: an untyped dict describing
 * THAT attempt only. The board already held it for every joined task, printed
 * "not sampled" for every step outside the sample, and summed it into the
 * row's total anyway -- so a row's total disagreed with its own nodes.
 *
 * The owner's decision: where telemetry is not sampled, show the result's
 * figure, and SAY WHERE IT CAME FROM ("from result"), because the two sources
 * are not the same measurement. Every key is read through the same finite-
 * number guard as everything else here: a missing key is not a zero.
 */
export interface ResultUsage {
  readonly usd: number | null
  readonly inputTokens: number | null
  readonly outputTokens: number | null
}

export function resultUsageOf(task: Task): ResultUsage | null {
  const usage = usageOf(task)
  if (usage === null) return null
  const n = (key: string): number | null => {
    const v = usage[key]
    return typeof v === 'number' && Number.isFinite(v) ? v : null
  }
  const out = { usd: n('total_cost_usd'), inputTokens: n('input_tokens'), outputTokens: n('output_tokens') }
  return out.usd === null && out.inputTokens === null && out.outputTokens === null ? null : out
}

/**
 * The result's figures for a step whose task has FINISHED, and null for any
 * other -- THE ONE BORROWING RULE, for the node, the Table, the row's total
 * and the inspector alike.
 *
 * A result belongs to the attempt that wrote it, and only a finished task's
 * result is its newest attempt's. `control.py`'s `fail_retryably` writes the
 * failed attempt's `result_summary` and sends the task back to READY, so a
 * step on its second attempt carries its FIRST attempt's result while it runs
 * again. The inspector already borrowed only for the newest attempt of a
 * finished task; the board borrowed for every state, and drew a running step's
 * old figure as its cost -- under a note saying it was what the worker wrote
 * when "this attempt" finished, and in a total no inspector could account for.
 */
export function finishedResultOf(task: Task): ResultUsage | null {
  return TERMINAL_STATES.has(task.state) ? resultUsageOf(task) : null
}

/** Where a step's cost figure came from. */
export type FigureSource = 'telemetry' | 'result'

/**
 * ONE STEP'S COST, BY THE ONE RULE THE NODE, THE TABLE AND THE ROW'S TOTAL
 * SHARE: the attempt telemetry where it carries a cost, otherwise the result's
 * -- and the result's only once the task has finished (`finishedResultOf`),
 * which is when the inspector offers it too.
 *
 * `telemetry` is the step's rolled-up attempts when this board read them, and
 * undefined when it did not (outside the sample, or the read failed). Null
 * when neither source has a figure -- which is an absence, never a zero.
 */
export function stepCostOf(
  task: Task,
  telemetry: StepUsage | undefined,
): { usd: number; from: FigureSource } | null {
  if (
    telemetry !== undefined &&
    telemetry.attemptsWithCost > 0 &&
    typeof telemetry.costUsd === 'number' &&
    Number.isFinite(telemetry.costUsd)
  ) {
    return { usd: telemetry.costUsd, from: 'telemetry' }
  }
  const result = finishedResultOf(task)?.usd ?? null
  return result === null ? null : { usd: result, from: 'result' }
}

/**
 * The row's total, summed from the SAME per-step figures its nodes draw.
 *
 * `telemetry` is the board's attempt read once it has landed, null before it
 * has (or when it failed). Before it lands every step's figure is its result's,
 * which is what the nodes would show too; once it lands, a sampled step's
 * figure is its attempts' sum -- every attempt, where the result describes only
 * the one that finished -- and the total moves with its nodes rather than
 * staying on the other source (WF-5).
 */
export function workflowSpend(
  steps: readonly WorkflowStep[],
  taskById: ReadonlyMap<string, Task> | null,
  telemetry: ReadonlyMap<string, StepUsage> | null = null,
): WorkflowSpend {
  let usd: number | null = null
  let covered = 0
  let fromResult = 0
  let joined = 0

  for (const step of steps) {
    const task = step.task_id ? (taskById?.get(step.task_id) ?? null) : null
    if (!task) continue
    joined += 1
    const cost = stepCostOf(task, telemetry?.get(task.id))
    if (cost === null) continue
    covered += 1
    if (cost.from === 'result') fromResult += 1
    usd = (usd ?? 0) + cost.usd
  }

  return { usd, covered, fromResult, joined, steps: steps.length }
}

// ---------------------------------------------------------------------------
// Runner profiles
// ---------------------------------------------------------------------------

/**
 * Which runner profiles this workflow uses, commonest first.
 *
 * On the collapsed row this answers "what is this workflow spending my
 * subscription on" without opening it -- a board can carry a twenty-step run
 * across five profiles, and which ones they are decides whether a quota park
 * is about to matter. The profile stays on every expanded node too; this is a
 * summary of those, never a replacement for them.
 */
export interface ProfileCount {
  readonly profile: string
  readonly count: number
}

export function profileMix(steps: readonly WorkflowStep[]): ProfileCount[] {
  const counts = new Map<string, number>()
  for (const s of steps) counts.set(s.runner_profile, (counts.get(s.runner_profile) ?? 0) + 1)
  return Array.from(counts, ([profile, count]) => ({ profile, count })).sort(
    (a, b) => b.count - a.count || a.profile.localeCompare(b.profile),
  )
}

/**
 * How many profiles the collapsed row names before it counts the rest. The
 * row's promise is "two named and the rest counted"; `foldMix` may name FEWER
 * when two do not fit, and never more.
 */
export const MIX_NAMED_MAX = 2

// THE CHIP'S GEOMETRY, from the sheet, in the same terms `NODE_CHROME_W` is
// derived in above. `.wf-chip` is `padding: 0 5px` inside a 1px border at
// --t-micro mono; `.wf-chip-n` (the `×N`) sits `margin-left: 3px` inside it;
// `.wf-mix` spaces the chips `--ctl-s1` apart. If those rules change, these
// move with them -- the cost of drifting is a chip folded one step early or a
// few pixels clipped, never a wrong count, because the `+N` is computed from
// what was folded rather than measured.
const CHIP_CHROME_W = 2 * 5 + 2
const CHIP_N_GAP = 3
const MIX_GAP = 4

/** One named chip, `claude-code ×20`, as drawn. */
export function mixChipW(m: ProfileCount): number {
  return (
    CHIP_CHROME_W +
    monoW(m.profile.length, T_MICRO) +
    CHIP_N_GAP +
    monoW(1 + String(m.count).length, T_MICRO)
  )
}

/** The `+N` chip that counts what was folded. */
export function moreChipW(rest: number): number {
  return CHIP_CHROME_W + monoW(1 + String(rest).length, T_MICRO)
}

/**
 * Which chips the row names, and how many it folds into `+N`, in `room` pixels.
 *
 * WHOLE CHIPS OR NONE. `.wf-mix` clips, so a row that simply drew the first two
 * chips and a `+1` cut them mid-word at 1440 -- `moc`, half a `+` -- and a
 * clipped profile name is a different, shorter name. So the row folds a chip
 * into the count instead of letting the column cut it: the named chips plus
 * the `+N` must fit, and the `+N` always counts exactly what is not named.
 *
 * `room === null` is a column nobody has measured -- jsdom, or the instant
 * before layout -- and gets the row's plain promise, two named and the rest
 * counted. Guessing a width there would be inventing a measurement.
 */
export function foldMix(
  mix: readonly ProfileCount[],
  room: number | null,
): { shown: ProfileCount[]; rest: number } {
  const cap = Math.min(MIX_NAMED_MAX, mix.length)
  if (room === null) return { shown: mix.slice(0, cap), rest: mix.length - cap }
  for (let k = cap; k >= 0; k--) {
    const shown = mix.slice(0, k)
    const rest = mix.length - k
    const widths = shown.map(mixChipW)
    if (rest > 0) widths.push(moreChipW(rest))
    const w = widths.reduce((t, x) => t + x, 0) + Math.max(0, widths.length - 1) * MIX_GAP
    if (w <= room) return { shown, rest }
  }
  // Not even the count fits. It is still the only true thing to draw: the
  // column clips it, and every profile is in the row's `title` regardless.
  return { shown: [], rest: mix.length }
}
