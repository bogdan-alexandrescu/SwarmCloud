/**
 * THE WORKFLOWS LIST'S NEEDS-YOU ROWS, FROM THE LIST READ ALONE (the owner's
 * pick on PR #3147, "Widen view=summary").
 *
 * `GET /v1/workflows?view=summary` serves each step's `task_summary.needs`
 * (`codec.step_needs`) off the task document the rollup already read, and
 * `summaryTask` joins it into the step's task: the hand-off's wait and clock,
 * a merge's owner-questions hold, the pull request. What is not a task field
 * -- a question wait's open questions and the indices of those only the
 * agent's artifact holds -- is read off the block here. No row reads anything
 * of its own; approvals are not in the list read, so the page passes
 * `approvals={null}` and the chip says "not known".
 *
 *   Failed     every FAILED or DEAD_LETTERED step; Retry re-runs it inside
 *              its workflow.
 *   Question   one row per open question: the wait's, with its words and
 *              options (and, for a plugin step, its `plugin_ask`), then the artifact's, whose words are on the step's
 *              page (`onItsPage`): its Answer waits until they are read there.
 *   Merge      a merge held by the owner's questions (`ownerQuestionsHold`).
 *              An approval the merge asks for is not in the read, so a merge
 *              that waits only for one is under Approvals, "not known".
 *   Hand-off   `handoffOf` the step's task, unless it parks on a question
 *              wait, which is HUMAN_REQUIRED too and is answered, not taken.
 *
 * AN AWAITING_INPUT PARK (an await-input step, docs/design/human-steps.md
 * §3.4) is never a hand-off or a merge hold -- both read HUMAN_REQUIRED only.
 * It waits for the person's input, given on its page (the step's input card,
 * not `answerQuestion`), so it is one Question row `onItsPage`: counted,
 * named and opened, and its Answer disabled here.
 */
import { handoffOf, ownerQuestionsHold } from '../Handoff'
import { stepNeedsOf, summaryTask, type StepNeeds } from '../stepviews'
import { type Task, type Workflow } from '../types'
import { pluginAskOf, type NeedItem, type NeedItems, type NeedKind, type NeedPullRequest, type QuestionNeed } from './NeedsYou'

function prOf(task: Task): NeedPullRequest | null {
  const git = task.result_summary?.git
  const pr = typeof git === 'object' && git !== null ? (git as { pull_request?: unknown }).pull_request : null
  if (typeof pr !== 'object' || pr === null) return null
  const { number, url } = pr as { number?: unknown; url?: unknown }
  return typeof number === 'number' && typeof url === 'string' && /^https?:\/\//.test(url) ? { number, url } : null
}

/**
 * ONE STEP'S NEEDS: the one classifier the bar (`listNeedItems`) and the
 * row's Needs mark (`workflowlist.needsOf`) both read, so the two never
 * count the same step differently. `needs` is the step's block (null when the
 * list served none: the kinds only it can tell are then not known, see
 * `NEEDS_BLOCK_KINDS`, and a HUMAN_REQUIRED park is no hand-off, since it
 * may be a question wait); `title` is the row's.
 */
export function stepNeedItems(task: Task, needs: StepNeeds | null, title: string): NeedItem[] {
  const out: NeedItem[] = []
  const pr = prOf(task)
  if (task.state === 'FAILED' || task.state === 'DEAD_LETTERED') {
    const why = task.last_error?.split('\n')[0]?.trim() ?? ''
    out.push({ kind: 'failed', task, title, why: why === '' ? null : why, pr })
  }
  const plugin = pluginAskOf(task, needs)
  for (const q of needs?.questions ?? []) {
    out.push({
      kind: 'question',
      taskId: task.id,
      index: q.index,
      title,
      question: q.question,
      options: q.options,
      recommended: q.recommended,
      ...(plugin !== null ? { plugin } : {}),
    })
  }
  for (const index of needs?.questions_unread ?? []) {
    out.push({ kind: 'question', taskId: task.id, index, title, question: null, onItsPage: true })
  }
  if (task.state === 'PARKED' && task.park_reason === 'AWAITING_INPUT') {
    out.push({ kind: 'question', taskId: task.id, index: 0, title, question: null, onItsPage: true })
  }
  if (ownerQuestionsHold(task) !== null) out.push({ kind: 'merge', task, title, pr })
  else if (needs !== null && needs.question_wait !== true && handoffOf(task) !== null) out.push({ kind: 'handoff', task, title })
  return out
}

/**
 * The kinds only a step's needs block tells (PR #3147): a question wait's
 * and the artifact's questions, a merge's owner-questions hold and a browser
 * hand-off, which the block's state alone cannot tell apart. A step with no
 * block -- an API older than it, during a rolling deploy -- leaves their
 * counts not known, never 0 (the owner's rule). Failed is the step's state.
 */
export const NEEDS_BLOCK_KINDS: readonly NeedKind[] = ['question', 'merge', 'handoff']

/**
 * Every kind the list read can count. A step whose task is not in the read
 * at all leaves each of them not known, its failure too: nothing says what
 * state it is in. Approvals are never in the list read (`approvals={null}`).
 */
export const LIST_KINDS: readonly NeedKind[] = ['failed', ...NEEDS_BLOCK_KINDS]

/**
 * ONE WORKFLOW'S NEEDS, the rows the bar draws for it and the kinds it cannot
 * count: the one reading both the bar (`listNeedItems`, over every workflow)
 * and the row's Needs mark (`workflowlist.needsOf`) take, so the two never
 * disagree on a step (the third review of PR #3147: a step served without a
 * block made the bar read "not known" and the row "—"). A step with a task
 * and no block leaves `NEEDS_BLOCK_KINDS` not known and is never a hand-off
 * here (its park may be a question wait); a step whose task is not in the
 * read leaves every kind not known. `title` is a step's row title.
 */
export function workflowNeedItems(
  w: Workflow,
  taskById: ReadonlyMap<string, Task> | null,
  title: (stepId: string) => string,
): NeedItems {
  const { items, blind, unread } = classified(w, taskById, title)
  const ended = workflowFinished(w) && !items.some((i) => i.kind === 'merge')
  // A FINISHED WORKFLOW holds no work: nothing it can still wait on is not
  // known, only whether a step it could not read failed.
  const unknownKinds = unread ? (ended ? (['failed'] as const) : LIST_KINDS) : blind && !ended ? NEEDS_BLOCK_KINDS : []
  return Object.assign(items, { unknownKinds })
}

/**
 * ONLY WHAT STILL HOLDS WORK (BGD's pick, 6 November 2026): a workflow that
 * has finished -- SUCCEEDED, FAILED, CANCELLED or DEAD_LETTERED, its derived
 * state -- waits on nobody's answer, so its unanswered questions are neither
 * Needs you nor the list's "?". Its page shows them as one quiet tick beside
 * its state (`finishedQuestionsOf`). A merge held by owner questions is
 * still work, and a held merge keeps every question in its workflow counted.
 * The bell's task question source (`notifications._asked_item`) counts by the
 * same rule.
 */
export const FINISHED_WORKFLOW_STATES: ReadonlySet<string> = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED', 'DEAD_LETTERED'])

export function workflowFinished(w: Workflow): boolean {
  return FINISHED_WORKFLOW_STATES.has(w.state)
}

/**
 * A question's words as two steps compare them: case and spacing aside. An
 * implement step and its fix step often ask the same question with other
 * option labels, so the text alone is matched (the server's
 * `notifications._question_key` reads the same).
 */
export function questionKey(text: string): string {
  return text.split(/\s+/).filter((t) => t !== '').join(' ').toLowerCase()
}

/**
 * The one walk both reads take: every step's rows, a question asked again by
 * a later step in step order dropped (its words matched by `questionKey`;
 * a row whose words are only on its page cannot be matched and stays), and,
 * for a finished workflow with no held merge, its question rows moved to
 * `quiet`.
 */
function classified(
  w: Workflow,
  taskById: ReadonlyMap<string, Task> | null,
  title: (stepId: string) => string,
): { items: NeedItem[]; quiet: QuestionNeed[]; blind: boolean; unread: boolean } {
  const all: NeedItem[] = []
  const asked = new Set<string>()
  let blind = false
  let unread = false
  for (const step of w.steps) {
    if (!step.task_id) continue
    const needs = stepNeedsOf(step)
    if (needs === null) blind = true
    const task = taskById?.get(step.task_id) ?? summaryTask(w, step)
    if (task === null || task === undefined) {
      unread = true
      continue
    }
    for (const item of stepNeedItems(task, needs, title(step.step_id))) {
      if (item.kind === 'question' && item.question !== null) {
        const key = questionKey(item.question)
        if (asked.has(key)) continue
        asked.add(key)
      }
      all.push(item)
    }
  }
  if (!workflowFinished(w) || all.some((i) => i.kind === 'merge')) return { items: all, quiet: [], blind, unread }
  return {
    items: all.filter((i) => i.kind !== 'question'),
    quiet: all.filter((i): i is QuestionNeed => i.kind === 'question'),
    blind,
    unread,
  }
}

/**
 * A FINISHED workflow's unanswered questions, which no longer count as Needs
 * you: its page's quiet tick beside its state counts them, and opens each
 * asking step's questions, still answerable there. Empty for a workflow that
 * has not finished, or whose merge is held: its questions are Needs you.
 */
export function finishedQuestionsOf(w: Workflow, taskById: ReadonlyMap<string, Task> | null): QuestionNeed[] {
  return classified(w, taskById, (stepId) => stepId).quiet
}

/**
 * Every row the list's Needs-you bar draws for `workflows`, in their order and
 * each workflow's step order. `nameOf` is the list row's name for a workflow;
 * a row's title is that name and the step's id. `unknownKinds` names the
 * kinds whose count is not known: every kind any workflow cannot count
 * (`workflowNeedItems`), so the bar never totals what a row says it cannot.
 */
export function listNeedItems(
  workflows: readonly Workflow[],
  taskById: ReadonlyMap<string, Task> | null,
  nameOf: (w: Workflow) => string,
): NeedItems {
  const out: NeedItem[] = []
  const unknown = new Set<NeedKind>()
  for (const w of workflows) {
    const name = nameOf(w)
    const items = workflowNeedItems(w, taskById, (stepId) => `${name} › ${stepId}`)
    out.push(...items)
    for (const k of items.unknownKinds ?? []) unknown.add(k)
  }
  return Object.assign(out, { unknownKinds: LIST_KINDS.filter((k) => unknown.has(k)) })
}
