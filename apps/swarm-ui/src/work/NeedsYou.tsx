/**
 * NEEDS YOU: THE WORK SCREENS' ROW OF WHAT WAITS ON THE PERSON, AND ITS LIST
 * (the picked Work redesign, "Graph with a docked inspector, plus the
 * Attention inbox's row of what needs you"; part of #2688).
 *
 * One line of counted chips -- Failed, Question, Merge waiting, Your turn in a
 * browser, Approvals -- on top of every Work screen, and under it one line per
 * item with its action in the row:
 *
 *   Question         Answer opens the answer box IN THE ROW and sends it
 *                    through `answerQuestion`, the call "Questions for you"
 *                    makes (`POST /v1/tasks/{id}/answers`).
 *   Failed           A lone agent's Retry is the agent page's Run again:
 *                    Submit opened with the same runner profile BY NAME
 *                    (invariant 10), the masked prompt and the repository
 *                    (`prefillTask`); nothing is sent from here. A workflow
 *                    step's Retry re-runs the step INSIDE its workflow
 *                    (`rerunWorkflowStep`, `POST /v1/workflows/{id}/steps/
 *                    {step}/rerun`; the owner's pick on PR #3123): its prompt
 *                    alone, submitted as a lone agent, is not the step it was.
 *                    It asks first, in the row, naming the step.
 *   Merge waiting    Approve merge is `approveApproval` with the digest of the
 *                    approval the page read. While owner questions hold the
 *                    merge (`ownerQuestionsHold`) it is disabled and names
 *                    each one; their answers resume it on their own.
 *   Your turn        A held browser: Take your turn opens the agent's Browser
 *                    tab, where Take control is. A parked one: I'm ready, the
 *                    banner's `humanReady`.
 *   Approval         Open, to its page in Automate › Approvals.
 *
 * NOTHING IS COUNTED BY HAND. Every count is the length of what the page
 * passed, read from data it already holds. A page that cannot read approvals
 * passes `approvals={null}`, and the chip reads "not known", never 0.
 *
 * THE HEADER ROW IS THE TOGGLE (the owner's ask, 11 November 2026, the
 * workflow page's WorkflowNeedsCard of 6 November made the shared strip's
 * behaviour): Needs you, its count and one plain count per kind, and a caret,
 * in one button. The strip ALWAYS STARTS COLLAPSED -- nothing is stored, not
 * in localStorage, not in the URL -- and a press opens the item rows; one more
 * folds them back into the row. With nothing waiting there is no toggle: the
 * counts and the quiet line, as before.
 *
 * A list's "N more" picks its kind and reports the pick with the task ids it
 * covers (`onSelect`), so a page can dim the grid cells that do not match;
 * folding the strip clears it.
 * Mounted by the Work pages; it reads nothing on its own but the masked
 * prompt a Retry carries over.
 *
 * A PLUGIN STEP'S PARKED QUESTION (the plugins console's picked interview,
 * direction B's Needs-you screen, 4 November 2026; docs/design/agent-plugins.md
 * and agent-plugins-question-bridge.md). A step that runs a plugin parks on a
 * question its agent could not answer itself, and nobody's answer is guessed:
 * unanswered for its wait (24 hours for a bridged question), the step ends
 * with the question open. Its row says which plugin and mode asked, the skill
 * that asked and the time left; beside the list, one card per parked plugin
 * step. What tells one is in the list read (the owner's pick on PR #3262,
 * "Serve them in step_needs"): a step's `task_summary.needs.plugin_ask`
 * (`codec.step_needs`), served for a plugin step PARKED on a question wait
 * only -- the plugins and modes of its work contract's `capabilities.plugins`,
 * its repository, the wait's `asked_at` and `expires_at` as
 * `GET /v1/tasks/{id}/questions` serves them, and the skill that asked. `stepNeedItems` puts it on the
 * step's question rows (`plugin`); no task or questions are read for it, so
 * every parked plugin step in the read is told, however many there are.
 * The skill that asked is the worker's record of the last `Skill` call before
 * the question (the owner's pick of 4 November 2026); a park with none
 * recorded reads "not known", and so does a clock the wait did not serve,
 * never 0. A
 * planner or designer step's card opens its interview, a full page
 * (InterviewPage.tsx), and links its run's page beside it.
 */
import { useEffect, useId, useRef, useState, type MouseEvent } from 'react'
import {
  answerQuestion,
  approveApproval,
  loadTaskInputOnce,
  rerunWorkflowStep,
  type ApprovalItem,
  type QuestionOption,
} from '../api'
import { Button, buttonClass } from '../components'
import { errorHeading, type ApiError } from '../fetch'
import { handoffOf, humanReady, ownerQuestionsHold, timeLeft, type HeldQuestion } from '../Handoff'
import { interviewAddress } from '../interviewPaths'
import { addressToPath } from '../paths'
import { useRoleGate } from '../Profile'
import { type StepNeeds } from '../stepviews'
import { prefillTask } from '../submitPrefill'
import { type Task } from '../types'
import { useNow } from '../useNow'
import './needs-you.css'

export type NeedKind = 'failed' | 'question' | 'merge' | 'handoff' | 'approval'

/** A pull request a row names, as the page read it. */
export interface NeedPullRequest {
  number: number
  url: string
}

/** A failed agent or workflow step. */
export interface FailedNeed {
  kind: 'failed'
  task: Task
  /** The step's or agent's name, as the page draws it. */
  title: string
  /** Why it failed, one line; null when the page has no error to show. */
  why: string | null
  pr?: NeedPullRequest | null
}

/** One open owner question. `index` is its 0-based place in its task's questions. */
export interface QuestionNeed {
  kind: 'question'
  taskId: string
  index: number
  /** The asking step's or agent's name. */
  title: string
  question: string | null
  /** Its options, when the page read them; with none, the answer is the person's own words. */
  options?: readonly QuestionOption[]
  recommended?: string | null
  /** The API's `may_answer` for this task when the page read it; false disables Answer and says why. */
  mayAnswer?: boolean
  mayAnswerBecause?: string | null
  /**
   * Its words are on its task's page and not in what the page read (the
   * Workflows list: the agent's `questions.json`, PR #3147). The row says so
   * and Answer is disabled: nobody answers a question they cannot read.
   */
  onItsPage?: boolean
  /** Its step runs a plugin and is parked on this question: served in the list read (`pluginAskOf`). */
  plugin?: PluginAsk
}

/** A merge step waiting. What holds it is read from the task's park (`ownerQuestionsHold`). */
export interface MergeNeed {
  kind: 'merge'
  task: Task
  title: string
  pr?: NeedPullRequest | null
  /** The merge's approval, when the page read one with its digest; null when none is asked. */
  approval?: { id: string; digest: unknown; head?: string | null } | null
}

/** A hand-off: an agent's browser waits for the person (`handoffOf` is not null). */
export interface HandoffNeed {
  kind: 'handoff'
  task: Task
  title: string
}

export type NeedItem = FailedNeed | QuestionNeed | MergeNeed | HandoffNeed

/**
 * The rows a page draws, and the kinds whose count it cannot know
 * (`unknownKinds`, e.g. the Workflows list's `listNeedItems` over a step the
 * list served no needs for): those chips read "not known", never 0.
 */
export type NeedItems = readonly NeedItem[] & { readonly unknownKinds?: readonly NeedKind[] }

/** The chips' order and words. */
export const NEED_KINDS: readonly NeedKind[] = ['failed', 'question', 'merge', 'handoff', 'approval']

export const NEED_WORD: Readonly<Record<NeedKind, string>> = {
  failed: 'Failed',
  question: 'Question',
  merge: 'Merge waiting',
  handoff: 'Your turn in a browser',
  approval: 'Approvals',
}

/** The mark each kind carries; on a narrow bar it stands for the word, which stays its label. */
const NEED_GLYPH: Readonly<Record<NeedKind, string>> = { failed: '✗', question: '?', merge: '⇢', handoff: '◉', approval: '✓' }

/** Rows per kind shown before the rest fold into "N more", while no kind is picked. */
export const FOLD_AT = 2

/** What a chip pick covers: the kind, and every task id its rows name. */
export interface NeedSelection {
  kind: NeedKind
  taskIds: string[]
}

/** The task ids a row names: its own, and for a held merge the asking tasks. */
export function taskIdsOf(item: NeedItem): string[] {
  if (item.kind === 'question') return [item.taskId]
  if (item.kind === 'merge') {
    const hold = ownerQuestionsHold(item.task)
    return [item.task.id, ...(hold?.questions.map((q) => q.task_id) ?? [])]
  }
  return [item.task.id]
}

function keyOf(item: NeedItem): string {
  return item.kind === 'question' ? `question:${item.taskId}:${item.index}` : `${item.kind}:${item.task.id}`
}

/** How a held question is named: its step (or task) and its number. */
export function heldName(q: HeldQuestion): string {
  return `${q.step_id ?? q.task_id} question ${q.question_index + 1}`
}

/** Why Approve merge is disabled, or null when it may be pressed. */
export function mergeHeldBecause(item: MergeNeed): string | null {
  const hold = ownerQuestionsHold(item.task)
  if (hold !== null && (hold.questions.length > 0 || hold.unreadable > 0)) {
    const n = hold.questions.length + hold.unreadable
    const named = hold.questions.map(heldName)
    if (hold.unreadable > 0) named.push(`${hold.unreadable} not readable`)
    return `Held by ${n} ${n === 1 ? 'question' : 'questions'}: ${named.join(', ')}`
  }
  if (item.approval === null || item.approval === undefined) return 'No approval is asked of you: it merges by itself'
  return null
}

/** One plugin a step runs, by catalogue name, and its mode, as its work contract holds it. */
export interface StepPlugin {
  name: string
  mode: string
}

/** A plugin step parked on a question, as its task and its wait were read. */
export interface PluginAsk {
  taskId: string
  workflowId: string | null
  stepId: string | null
  repository: string | null
  plugins: readonly StepPlugin[]
  /** When the wait began and when it ends, ms; null when the wait served neither. */
  askedAt: number | null
  expiresAt: number | null
  /** The skill that asked, as the worker recorded it; null when not known. */
  skill: string | null
}

/** The modes' words, as the catalogue's picker says them. */
const MODE_WORD: Readonly<Record<string, string>> = { unattended: 'Unattended', planner: 'Planner', designer: 'Designer' }
const modeWord = (mode: string): string => MODE_WORD[mode] ?? mode
/** The modes that run an interview with a person, whose questions are its pages. */
const INTERVIEW_MODES: readonly string[] = ['planner', 'designer']

const isMap = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)

const msOf = (v: unknown): number | null => {
  const ms = typeof v === 'string' ? Date.parse(v) : NaN
  return Number.isFinite(ms) ? ms : null
}

/**
 * A parked plugin step's ask, from its task and its step's needs block:
 * `plugin_ask` (`codec.step_needs`), served for a plugin step PARKED on a
 * question wait only. Null for any other step, and for a block that served
 * none (an API older than it): the row is then plain, nothing guessed. A
 * plugin not `{name, mode}` is no plugin.
 */
export function pluginAskOf(task: Task, needs: StepNeeds | null): PluginAsk | null {
  const served: unknown = needs?.plugin_ask
  if (!isMap(served) || task.state !== 'PARKED') return null
  const plugins = (Array.isArray(served.plugins) ? served.plugins : []).flatMap((p: unknown) =>
    isMap(p) && typeof p.name === 'string' && p.name !== '' && typeof p.mode === 'string' && p.mode !== '' ? [{ name: p.name, mode: p.mode }] : [],
  )
  if (plugins.length === 0) return null
  return {
    taskId: task.id,
    workflowId: task.workflow_id ?? null,
    stepId: task.step_id ?? null,
    repository: typeof served.repository_url === 'string' && served.repository_url !== '' ? served.repository_url : (task.repository_url ?? null),
    plugins,
    askedAt: msOf(served.asked_at),
    expiresAt: msOf(served.expires_at),
    skill: typeof served.skill === 'string' && served.skill.trim() !== '' ? served.skill.trim() : null,
  }
}

/** The time a wait has left, "23 h 22 min"; null once it has passed or when its end is not known. */
export function leftOfWait(expiresAt: number | null, now: number): string | null {
  if (expiresAt === null) return null
  const ms = expiresAt - now
  if (ms <= 0) return null
  const min = Math.floor(ms / 60_000)
  if (min < 1) return 'under a minute'
  const h = Math.floor(min / 60)
  return h === 0 ? `${min} min` : `${h} h ${min % 60} min`
}

/** The whole wait in hours, "24 h", when both ends were served. */
function spanOf(ask: PluginAsk): string | null {
  if (ask.askedAt === null || ask.expiresAt === null || ask.expiresAt <= ask.askedAt) return null
  return `${Math.round((ask.expiresAt - ask.askedAt) / 3_600_000)} h`
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
/** "19 Oct, 11:22 UTC". */
function atUtc(ms: number): string {
  const d = new Date(ms)
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}, ${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')} UTC`
}

/** "example-org/contacts-app" for a forge URL; the value as served otherwise. */
function repoName(url: string): string {
  const m = /^https?:\/\/[^/]+\/(.+?)(?:\.git)?\/?$/.exec(url)
  return m?.[1] ?? url
}

/** "gstack · Unattended", each plugin the step runs. */
const pluginWords = (ask: PluginAsk): string => ask.plugins.map((p) => `${p.name} · ${modeWord(p.mode)}`).join(', ')

/** The time left as a row and a card say it. */
function leftWords(ask: PluginAsk, now: number): string {
  if (ask.expiresAt === null) return 'time left not known'
  const left = leftOfWait(ask.expiresAt, now)
  if (left === null) return 'time is up'
  const span = spanOf(ask)
  return span === null ? `${left} left` : `${left} left of ${span}`
}

/** Each parked plugin step's ask, by task, off the question rows that carry one (`QuestionNeed.plugin`). */
function pluginAsksOf(items: NeedItems): ReadonlyMap<string, PluginAsk> {
  const out = new Map<string, PluginAsk>()
  for (const i of items) if (i.kind === 'question' && i.plugin !== undefined && !out.has(i.taskId)) out.set(i.taskId, i.plugin)
  return out
}

/** The line a plugin step's question row adds: plugin and mode, the skill that asked, the time left. */
function PluginLine({ ask }: { ask: PluginAsk }) {
  const now = useNow(30_000)
  return (
    <span className="ny-plugin" title={ask.expiresAt === null ? undefined : `It ends, the question open, at ${atUtc(ask.expiresAt)}`}>
      <b>{pluginWords(ask)}</b> · {ask.skill === null ? 'skill that asked: not known' : `asked by ${ask.skill}`} · <span className="ny-plugin-left">{leftWords(ask, now)}</span> · never answered for anyone
    </span>
  )
}

/** One card per parked plugin step, beside the list (direction B's Needs-you screen). */
function ParkedPluginSteps({ asks, rows }: { asks: readonly PluginAsk[]; rows: ReadonlyMap<string, QuestionNeed> }) {
  const now = useNow(30_000)
  const spans = new Set(asks.map(spanOf))
  const span = spans.size === 1 ? [...spans][0] : null
  return (
    <aside className="ny-parked" aria-label="Plugin steps parked on a question">
      <h3 className="ny-parked-h">
        Plugin steps parked <span className="ny-n">{asks.length}</span>
      </h3>
      <p className="ny-parked-lead">
        Each holds no machine while it waits. Unanswered {span === '24 h' ? 'in 24 hours' : 'in its time'}, it ends with the question open: nobody&rsquo;s
        answer is guessed.
      </p>
      {asks.map((a) => {
        const row = rows.get(a.taskId)
        const interview = a.plugins.some((p) => INTERVIEW_MODES.includes(p.mode))
        const left = leftOfWait(a.expiresAt, now)
        return (
          <article key={a.taskId} className="ny-park" aria-label={`${a.stepId ?? a.taskId}, parked`}>
            <span className="ny-park-h">
              <b>{a.stepId ?? a.taskId}</b>
              {a.workflowId !== null && <span className="ny-dim"> in {a.workflowId}</span>}
            </span>
            {a.repository !== null ? <code className="ny-park-repo">{repoName(a.repository)}</code> : <span className="ny-dim">repository not known</span>}
            <span className="ny-park-chip">
              {a.plugins.map((p) => (
                <span key={p.name} className="ny-park-seg">
                  <b>{p.name}</b>
                  <span>{modeWord(p.mode)}</span>
                </span>
              ))}
              <span className="ny-park-seg is-skill">{a.skill === null ? 'asked by: not known' : `asked by ${a.skill}`}</span>
            </span>
            <span className="ny-park-time">
              {a.expiresAt === null ? (
                <span className="ny-dim">time left not known</span>
              ) : left === null ? (
                <b>time is up</b>
              ) : (
                <>
                  <b>{left}</b> <span className="ny-dim">left{a.askedAt !== null && ` · asked ${atUtc(a.askedAt)}`}</span>
                </>
              )}
            </span>
            {interview ? (
              <span className="ny-park-act">
                <a className={buttonClass({ size: 'sm', kind: 'primary' })} href={addressToPath(interviewAddress(a.taskId))}
                  aria-label={`Open the interview: ${a.stepId ?? a.taskId}`}>
                  Open the interview
                </a>{' '}
                <a className={buttonClass({ size: 'sm', kind: 'secondary' })} href={openPath(a.taskId)} aria-label={`Open its run: ${a.stepId ?? a.taskId}`}>
                  Open its run
                </a>
              </span>
            ) : (
              <span className="ny-park-act ny-dim">
                It ran unattended
                {row !== undefined && (row.recommended === null || row.recommended === undefined) ? ' and this question has no recommended answer' : ''}, so it
                waits for you: answer it in its row (Question, {row?.title ?? a.stepId ?? a.taskId}).
              </span>
            )}
          </article>
        )
      })}
    </aside>
  )
}

export function NeedsYou({
  items,
  approvals,
  selected,
  onSelect,
  onChanged,
  cardStep,
}: {
  items: NeedItems
  /** The approvals waiting, or null when the page could not read them ("not known"). */
  approvals: readonly ApprovalItem[] | null
  /** The kind picked, when the page holds the pick; left out, the component holds it. */
  selected?: NeedKind | null
  /** A chip was pressed: the kind picked (null when cleared) and the task ids it covers. */
  onSelect?: (pick: NeedSelection | null) => void
  /** An action was accepted (an answer saved, an approval given, I'm ready): the page reads again. */
  onChanged?: () => void
  /** The step the workflow page's step card shows, which already says why it failed. */
  cardStep?: string | null
}) {
  const [own, setOwn] = useState<NeedKind | null>(null)
  const [unfolded, setUnfolded] = useState(false)
  const kind = selected === undefined ? own : selected
  const [open, setOpen] = useState(false)
  const listId = useId()
  const bodyId = useId()
  const asks = pluginAsksOf(items)

  const count = (k: NeedKind): number | null => {
    if (k === 'approval') return approvals === null ? null : approvals.length
    return items.unknownKinds?.includes(k) === true ? null : items.filter((i) => i.kind === k).length
  }
  const total = items.length + (approvals?.length ?? 0)

  const pick = (k: NeedKind | null) => {
    setOwn(k)
    setUnfolded(false)
    if (onSelect === undefined) return
    if (k === null) return onSelect(null)
    const ids = k === 'approval' ? [] : items.filter((i) => i.kind === k).flatMap(taskIdsOf)
    onSelect({ kind: k, taskIds: [...new Set(ids)] })
  }

  const list = (
    <NeedsList
      id={listId}
      items={items}
      approvals={approvals ?? []}
      kind={kind}
      unfolded={unfolded}
      asks={asks}
      onUnfold={(k) => (k === null ? setUnfolded(true) : pick(k))}
      onChanged={onChanged}
      cardStep={cardStep ?? null}
    />
  )
  // The cards stand beside the questions: hidden while a chip picks another kind.
  const parked = kind === null || kind === 'question' ? [...asks.values()] : []
  const firstRows = new Map<string, QuestionNeed>()
  for (const i of items) if (i.kind === 'question' && !firstRows.has(i.taskId)) firstRows.set(i.taskId, i)

  const marks = (
    <>
      <span className="ny-all">
        <b>Needs you</b> <span className="ny-n">{total}</span>
        {approvals === null && <span className="ny-plus" title="Approvals are not known here, so the total may be more">+</span>}
      </span>
      {NEED_KINDS.map((k) => {
        const n = count(k)
        // A CHIP IS A PLAIN COUNT in the header (the owner's ask, 11 November
        // 2026): the whole header toggles, so it is named, as an image, by its
        // kind and count. ON A PHONE THE WORD IS HIDDEN (needs-you.css), so the
        // label names the kind, a faint one's too: "Approvals: not known".
        const label = `${NEED_WORD[k]}: ${n === null ? 'not known' : n}`
        return (
          <span
            key={k}
            className={`ny-chip is-${k}${n === null ? ' is-faint' : n === 0 ? ' is-zero' : ''}`}
            role="img"
            aria-label={label}
            title={
              n !== null
                ? label
                : k === 'approval'
                  ? 'Approvals are not read on this screen, so their count is not known; Automate › Approvals lists them'
                  : `${NEED_WORD[k]}: not known. This screen's read does not say for every step; each step's page does`
            }
          >
            <span className="ny-glyph" aria-hidden>
              {NEED_GLYPH[k]}
            </span>
            <span className="ny-word">{NEED_WORD[k]}</span> <b>{n === null ? 'not known' : n}</b>
          </span>
        )
      })}
    </>
  )

  // WITH NOTHING WAITING the strip draws what it always drew: the counts and
  // the quiet line, no toggle (there is nothing to open).
  if (total === 0) {
    return (
      <section className="ny" aria-label="Needs you">
        <div className="ny-bar" role="group" aria-label="What needs you, by kind">
          {marks}
        </div>
        {list}
      </section>
    )
  }

  // THE HEADER IS THE TOGGLE, and the strip ALWAYS STARTS COLLAPSED (the
  // owner's ask, 11 November 2026, as WorkflowNeedsCard's pick of 6 November):
  // never stored. Folding it back clears a kind the list picked.
  const toggle = () => {
    if (open && kind !== null) pick(null)
    setUnfolded(false)
    setOpen(!open)
  }
  return (
    <section className={`ny has-some${open ? ' is-open' : ''}`} aria-label="Needs you">
      <button type="button" className="ny-bar" aria-expanded={open} aria-controls={bodyId} onClick={toggle}>
        {marks}
        <span className="ny-caret" aria-hidden>
          {open ? '▴' : '▾'}
        </span>
      </button>
      <div id={bodyId} className={`ny-body${parked.length > 0 ? ' has-parked' : ''}`} hidden={!open}>
        {list}
        {parked.length > 0 && <ParkedPluginSteps asks={parked} rows={firstRows} />}
      </div>
    </section>
  )
}

function NeedsList({
  id,
  items,
  approvals,
  kind,
  unfolded,
  asks,
  onUnfold,
  onChanged,
  cardStep,
}: {
  id: string
  items: NeedItems
  approvals: readonly ApprovalItem[]
  kind: NeedKind | null
  unfolded: boolean
  asks: ReadonlyMap<string, PluginAsk>
  onUnfold: (k: NeedKind | null) => void
  onChanged?: () => void
  cardStep: string | null
}) {
  if (items.length === 0 && approvals.length === 0) {
    // A KIND THE READ COULD NOT COUNT IS NOT A QUIET ONE: with questions or a
    // merge "not known", "Nothing waits on you here" would claim what the read
    // never said, so the line names what it does not know.
    const unknown = (items.unknownKinds ?? []).filter((k) => k !== 'approval')
    return (
      <p className="ny-quiet" id={id}>
        {/* 'NOTHING FOUND' (owner's pick, 2026-11-05): the chips already say
            which kinds are not known here, so the sentence is its tooltip. */}
        {unknown.length === 0 ? (
          'Nothing waits on you here'
        ) : (
          <span title={`Nothing this screen read waits on you; ${unknown.map((k) => NEED_WORD[k]).join(', ')} not known here`}>Nothing found</span>
        )}
      </p>
    )
  }
  const rows: JSX.Element[] = []
  const folded: { kind: NeedKind; n: number }[] = []
  for (const k of NEED_KINDS) {
    if (kind !== null && kind !== k) continue
    const all: JSX.Element[] =
      k === 'approval'
        ? approvals.map((a) => <ApprovalRow key={`approval:${a.approval_id}`} a={a} />)
        : items.filter((i) => i.kind === k).map((i) => <NeedRow key={keyOf(i)} item={i} ask={i.kind === 'question' ? asks.get(i.taskId) : undefined} onChanged={onChanged} cardStep={cardStep} />)
    const shown = kind === null && !unfolded ? all.slice(0, FOLD_AT) : all
    rows.push(...shown)
    if (shown.length < all.length) folded.push({ kind: k, n: all.length - shown.length })
  }
  return (
    <div className="ny-listwrap" id={id}>
      {rows.length === 0 ? (
        <p className="ny-quiet">Nothing of this kind waits on you here</p>
      ) : (
        <ul className="ny-list" aria-label={kind === null ? 'Needs you' : `Needs you: ${NEED_WORD[kind]}`}>
          {rows}
        </ul>
      )}
      {folded.length > 0 && (
        <p className="ny-more">
          {folded.map((f) => (
            <button key={f.kind} type="button" className="ny-morebtn" onClick={() => onUnfold(f.kind)}>
              {f.n} more {NEED_WORD[f.kind].toLowerCase()}
            </button>
          ))}
          <button type="button" className="ny-morebtn" onClick={() => onUnfold(null)}>
            Show all
          </button>
        </p>
      )}
    </div>
  )
}

function Kind({ k }: { k: NeedKind }) {
  return (
    <span className={`ny-kind is-${k}`} title={NEED_WORD[k]}>
      <span className="ny-glyph" aria-hidden>
        {NEED_GLYPH[k]}
      </span>
      <span className="ny-word">{NEED_WORD[k]}</span>
    </span>
  )
}

function Pr({ pr }: { pr: NeedPullRequest | null | undefined }) {
  if (pr === null || pr === undefined) return null
  return (
    <a className="ny-pr" href={pr.url} target="_blank" rel="noreferrer">
      #{pr.number}
    </a>
  )
}

const openPath = (taskId: string, pane = ''): string => addressToPath(`work/task/${encodeURIComponent(taskId)}${pane === '' ? '' : `/${pane}`}`)

function OpenLink({ href, name }: { href: string; name: string }) {
  return (
    <a className={buttonClass({ size: 'sm', kind: 'ghost' })} href={href} aria-label={`Open ${name}`}>
      Open
    </a>
  )
}

function NeedRow({ item, ask, onChanged, cardStep }: { item: NeedItem; ask?: PluginAsk; onChanged?: () => void; cardStep: string | null }) {
  switch (item.kind) {
    case 'failed':
      return <FailedRow item={item} onChanged={onChanged} cardStep={cardStep} />
    case 'question':
      return <QuestionRow item={item} ask={ask} onChanged={onChanged} />
    case 'merge':
      return <MergeRow item={item} onChanged={onChanged} />
    case 'handoff':
      return <HandoffRow item={item} onChanged={onChanged} />
  }
}

function FailedRow({ item, onChanged, cardStep }: { item: FailedNeed; onChanged?: () => void; cardStep: string | null }) {
  const workflowId = item.task.workflow_id ?? null
  const stepId = item.task.step_id ?? null
  const [failed, setFailed] = useState<ApiError | null>(null)
  // THE STEP CARD SAYS IT ALREADY (owner's pick, 2026-11-05): while the
  // workflow page's card is open on this step, the row drops its copy of the
  // error; on another step, with no card, or on another page it keeps it.
  const onCard = stepId !== null && stepId === cardStep
  return (
    <li className="ny-row is-failed">
      <Kind k="failed" />
      <b className="ny-title">{item.title}</b>
      {!onCard && (
        <span className="ny-text" title={item.why ?? undefined}>
          {item.why ?? 'No error was read'}
        </span>
      )}
      <Pr pr={item.pr} />
      <span className="ny-acts">
        {workflowId === null ? (
          <RetryLink task={item.task} name={item.title} />
        ) : stepId !== null ? (
          <StepRetryButton workflowId={workflowId} stepId={stepId} name={item.title} onFailed={setFailed} onChanged={onChanged} />
        ) : null}
        <OpenLink href={openPath(item.task.id, 'logs')} name={item.title} />
      </span>
      {failed !== null && (
        <span className="ny-err" role="alert">
          Not re-run: {failed.message || errorHeading(failed)}
        </span>
      )}
    </li>
  )
}

/**
 * RETRY ON A WORKFLOW STEP: the step re-run inside its workflow
 * (`rerunWorkflowStep`). Retry asks first, in the row, naming the step: a
 * re-run spends what the steps spend, and it re-runs the steps the failure
 * stopped too. Re-run sends it, by a member or above as a cancel is; Keep it
 * puts Retry back. The API refuses it while any step of the workflow still
 * runs, and a second Retry once one landed, and says why, which the row shows
 * with Retry back to press again. Accepted, the row names the steps re-run
 * (the answer's `replaced`) and the page reads again.
 */
export function StepRetryButton({
  workflowId,
  stepId,
  name,
  onFailed,
  onChanged,
}: {
  workflowId: string
  stepId: string
  name: string
  onFailed: (e: ApiError | null) => void
  onChanged?: () => void
}) {
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [rerun, setRerun] = useState<readonly string[] | null>(null)
  const roleGate = useRoleGate('member', 're-run steps')
  if (rerun !== null) {
    return (
      <span className="ny-done" role="status">
        Re-running {rerun.join(', ')}
      </span>
    )
  }
  if (roleGate !== null) {
    return (
      <Button size="sm" disabledReason={roleGate}>
        Retry
      </Button>
    )
  }
  const send = async () => {
    if (busy) return
    setBusy(true)
    onFailed(null)
    const r = await rerunWorkflowStep(workflowId, stepId)
    setBusy(false)
    setAsking(false)
    if (r.status === 'ok' || r.status === 'stale') {
      const steps = Object.keys(r.data.replaced ?? {})
      setRerun(steps.length > 0 ? steps : [stepId])
      onChanged?.()
    } else if (r.status === 'error') {
      onFailed(r.error)
    }
  }
  if (asking) {
    return (
      <span role="group" aria-label={`Re-run step ${stepId}?`}>
        <span>Re-run step {stepId} inside its workflow, with the steps its failure stopped?</span>
        <Button size="sm" kind="primary" busy={busy} aria-label={`Re-run ${stepId}`} onClick={() => void send()}>
          Re-run
        </Button>
        <Button size="sm" disabled={busy} onClick={() => setAsking(false)}>
          Keep it
        </Button>
      </span>
    )
  }
  return (
    <Button
      size="sm"
      kind="primary"
      aria-label={`Retry ${name}`}
      title="Re-run this step inside its workflow, with the steps its failure stopped."
      onClick={() => {
        onFailed(null)
        setAsking(true)
      }}
    >
      Retry
    </Button>
  )
}

/**
 * RETRY, AS RUN AGAIN DOES IT (AgentDetail's `RunAgain`): Submit with the same
 * profile, the masked prompt and the repository. The prompt is read on the
 * press, not per row; once it is in, the same link is pressed again and the
 * console's own link handling opens Submit.
 */
function RetryLink({ task, name }: { task: Task; name: string }) {
  const ref = useRef<HTMLAnchorElement>(null)
  const primed = useRef(false)
  const [busy, setBusy] = useState(false)
  const onClick = (e: MouseEvent<HTMLAnchorElement>) => {
    if (primed.current) {
      primed.current = false
      return
    }
    e.preventDefault()
    if (busy) return
    setBusy(true)
    void loadTaskInputOnce(task.id).then((copy) => {
      setBusy(false)
      const prompt = copy.status === 'ok' || copy.status === 'stale' ? (copy.data.prompt?.text ?? '') : ''
      prefillTask({ profile: task.runner_profile, prompt, repositoryUrl: task.repository_url ?? '' })
      primed.current = true
      ref.current?.click()
    })
  }
  return (
    <a
      ref={ref}
      className={buttonClass({ size: 'sm', kind: 'primary' }, busy)}
      href={addressToPath('work/new')}
      aria-busy={busy || undefined}
      aria-label={`Retry ${name}`}
      title={`Open Submit with ${task.runner_profile}, this agent's prompt and its repository filled in.`}
      onClick={onClick}
    >
      Retry
    </a>
  )
}

const OTHER = '__own_words__'
/** The longest free-text answer the API takes (`agent_questions.MAX_ANSWER_CHARS`), as QuestionsCard holds it. */
const MAX_ANSWER_CHARS = 2000

function QuestionRow({ item, ask, onChanged }: { item: QuestionNeed; ask?: PluginAsk; onChanged?: () => void }) {
  const [open, setOpen] = useState(false)
  const [pick, setPick] = useState<string | null>(null)
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<ApiError | null>(null)
  const [saved, setSaved] = useState(false)
  const roleGate = useRoleGate('member', 'answer')
  const boxRef = useRef<HTMLDivElement>(null)
  const name = `${item.title} question ${item.index + 1}`
  const options = (item.options ?? []).filter((o): o is QuestionOption & { label: string } => typeof o.label === 'string' && o.label !== '')
  const choice = options.length === 0 ? OTHER : pick
  const ownWords = text.trim()
  const notAllowed =
    roleGate ??
    (item.onItsPage === true ? 'Read the question on its page first: this list does not carry its words' : null) ??
    (item.mayAnswer === false ? (item.mayAnswerBecause ?? 'The API did not say you may answer') : null)
  const ready = !busy && choice !== null && (choice !== OTHER || (ownWords.length > 0 && ownWords.length <= MAX_ANSWER_CHARS))

  useEffect(() => {
    if (open) boxRef.current?.querySelector<HTMLElement>('input, textarea')?.focus()
  }, [open])

  const send = async () => {
    if (!ready || choice === null) return
    setBusy(true)
    setFailed(null)
    const r = await answerQuestion(item.taskId, {
      question_index: item.index,
      choice: choice === OTHER ? null : choice,
      text: choice === OTHER ? ownWords : null,
    })
    setBusy(false)
    if (r.status === 'ok' || r.status === 'stale') {
      setSaved(true)
      setOpen(false)
      onChanged?.()
    } else if (r.status === 'error') {
      setFailed(r.error)
    }
  }

  return (
    <li className={`ny-row is-question${open ? ' is-open' : ''}${ask ? ' is-plugin' : ''}`}>
      <Kind k="question" />
      <b className="ny-title">{item.title}</b>
      <span className="ny-text" title={item.question ?? undefined}>
        {item.question ?? (item.onItsPage === true ? 'Its words are on its page' : 'The question could not be read')}
      </span>
      <span className="ny-acts">
        {saved ? (
          <span className="ny-done" role="status">
            Answered
          </span>
        ) : notAllowed !== null ? (
          <Button size="sm" disabledReason={notAllowed}>
            Answer
          </Button>
        ) : (
          <Button size="sm" kind={open ? 'secondary' : 'primary'} aria-expanded={open} aria-label={`Answer ${name}`} onClick={() => setOpen(!open)}>
            {open ? 'Close' : 'Answer'}
          </Button>
        )}
        <OpenLink href={openPath(item.taskId)} name={item.title} />
      </span>
      {ask !== undefined && <PluginLine ask={ask} />}
      {open && !saved && (
        <div className="ny-answer" ref={boxRef} role="group" aria-label={`Your answer to ${name}`}>
          {item.question !== null && <p className="ny-q">{item.question}</p>}
          {options.length > 0 && (
            <div className="ny-opts" role="radiogroup" aria-label={`Options for ${name}`}>
              {options.map((o) => (
                <label key={o.label} className={`ny-opt${o.label === item.recommended ? ' is-recommended' : ''}`}>
                  <input type="radio" name={`ny:${item.taskId}:${item.index}`} checked={pick === o.label} onChange={() => setPick(o.label)} />
                  <span>
                    <b>{o.label}</b>
                    {o.label === item.recommended && <span className="ny-rec"> recommended</span>}
                    {o.description && <span className="ny-desc"> {o.description}</span>}
                  </span>
                </label>
              ))}
              <label className="ny-opt">
                <input type="radio" name={`ny:${item.taskId}:${item.index}`} checked={pick === OTHER} onChange={() => setPick(OTHER)} />
                <span>your own answer</span>
              </label>
            </div>
          )}
          {choice === OTHER && (
            <textarea
              className="ny-own"
              rows={2}
              maxLength={MAX_ANSWER_CHARS}
              aria-label={`Your own answer to ${name}`}
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
          )}
          <span className="ny-send">
            <Button size="sm" kind="primary" disabled={!ready} busy={busy ? 'Sending…' : false} onClick={() => void send()}>
              Send
            </Button>
            {failed !== null && (
              <span className="ny-err" role="alert">
                The answer was not saved: {errorHeading(failed)}
              </span>
            )}
          </span>
        </div>
      )}
    </li>
  )
}

function MergeRow({ item, onChanged }: { item: MergeNeed; onChanged?: () => void }) {
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<ApiError | null>(null)
  const [done, setDone] = useState(false)
  const held = mergeHeldBecause(item)
  const hold = ownerQuestionsHold(item.task)
  const head = item.approval?.head ?? null

  const approve = async () => {
    if (held !== null || item.approval === null || item.approval === undefined) return
    setBusy(true)
    setFailed(null)
    const r = await approveApproval(item.approval.id, { digest: item.approval.digest ?? null })
    setBusy(false)
    if (r.status === 'error') return setFailed(r.error)
    setDone(true)
    onChanged?.()
  }

  return (
    <li className="ny-row is-merge">
      <Kind k="merge" />
      <b className="ny-title">{item.title}</b>
      <span className="ny-text">
        {hold !== null && hold.questions.length + hold.unreadable > 0
          ? `held by ${hold.questions.length + hold.unreadable} ${hold.questions.length + hold.unreadable === 1 ? 'question' : 'questions'}`
          : item.approval
            ? 'waits for your approval'
            : 'waiting'}
      </span>
      <Pr pr={item.pr} />
      <span className="ny-acts">
        {done ? (
          <span className="ny-done" role="status">
            Approved
          </span>
        ) : (
          <Button size="sm" kind="primary" disabledReason={held ?? undefined} busy={busy ? 'Approving…' : false} onClick={() => void approve()}>
            {head === null ? 'Approve merge' : `Approve merge at ${head.slice(0, 7)}`}
          </Button>
        )}
        <OpenLink
          href={item.approval ? addressToPath(`automate/approvals?approval=${encodeURIComponent(item.approval.id)}`) : openPath(item.task.id)}
          name={item.title}
        />
        {failed !== null && (
          <span className="ny-err" role="alert">
            Not approved: {failed.message || errorHeading(failed)}
          </span>
        )}
      </span>
    </li>
  )
}

function HandoffRow({ item, onChanged }: { item: HandoffNeed; onChanged?: () => void }) {
  const now = useNow(1000)
  const h = handoffOf(item.task)
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<ApiError | null>(null)
  const [done, setDone] = useState(false)
  if (h === null) return null
  const left = timeLeft(h.deadline, now)
  const reason = h.phase === 'hold' ? h.reason : null

  const ready = async () => {
    setBusy(true)
    setFailed(null)
    const r = await humanReady(item.task.id)
    setBusy(false)
    if (r.status === 'error') return setFailed(r.error)
    setDone(true)
    onChanged?.()
  }

  return (
    <li className="ny-row is-handoff">
      <Kind k="handoff" />
      <b className="ny-title">{item.title}</b>
      <span className="ny-text" title={reason ?? undefined}>
        {h.phase === 'hold' ? (reason ?? 'asks for a person') : 'parked until you say you are ready'}
      </span>
      <span className="ny-left" title={h.phase === 'hold' ? 'Before the browser is parked' : 'Before it is given up (dead-lettered)'}>
        {left ?? (h.deadline === null ? 'time left not known' : 'time is up')}
      </span>
      <span className="ny-acts">
        {h.phase === 'hold' ? (
          <a className={buttonClass({ size: 'sm', kind: 'primary' })} href={openPath(item.task.id, 'browser')} aria-label={`Take your turn in ${item.title}`}>
            Take your turn
          </a>
        ) : done ? (
          <span className="ny-done" role="status">
            Starting its browser
          </span>
        ) : (
          <Button size="sm" kind="primary" busy={busy ? 'Asking…' : false} aria-label={`I'm ready for ${item.title}`} onClick={() => void ready()}>
            I'm ready
          </Button>
        )}
        <OpenLink href={openPath(item.task.id)} name={item.title} />
        {failed !== null && (
          <span className="ny-err" role="alert">
            Not resumed: {failed.message}
          </span>
        )}
      </span>
    </li>
  )
}

function ApprovalRow({ a }: { a: ApprovalItem }) {
  return (
    <li className="ny-row is-approval">
      <Kind k="approval" />
      <b className="ny-title">{a.kind}</b>
      <span className="ny-text" title={a.summary}>
        {a.summary}
      </span>
      <span className="ny-acts">
        <OpenLink href={addressToPath(`automate/approvals?approval=${encodeURIComponent(a.approval_id)}`)} name={`approval ${a.summary}`} />
      </span>
    </li>
  )
}
