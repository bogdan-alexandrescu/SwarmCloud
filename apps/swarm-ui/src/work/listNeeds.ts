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
 *              options, then the artifact's, whose words are on the step's
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
import { type NeedItem, type NeedItems, type NeedKind, type NeedPullRequest } from './NeedsYou'

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
 * `NEEDS_BLOCK_KINDS`); `title` is the row's.
 */
export function stepNeedItems(task: Task, needs: StepNeeds | null, title: string): NeedItem[] {
  const out: NeedItem[] = []
  const pr = prOf(task)
  if (task.state === 'FAILED' || task.state === 'DEAD_LETTERED') {
    const why = task.last_error?.split('\n')[0]?.trim() ?? ''
    out.push({ kind: 'failed', task, title, why: why === '' ? null : why, pr })
  }
  for (const q of needs?.questions ?? []) {
    out.push({
      kind: 'question',
      taskId: task.id,
      index: q.index,
      title,
      question: q.question,
      options: q.options,
      recommended: q.recommended,
    })
  }
  for (const index of needs?.questions_unread ?? []) {
    out.push({ kind: 'question', taskId: task.id, index, title, question: null, onItsPage: true })
  }
  if (task.state === 'PARKED' && task.park_reason === 'AWAITING_INPUT') {
    out.push({ kind: 'question', taskId: task.id, index: 0, title, question: null, onItsPage: true })
  }
  if (ownerQuestionsHold(task) !== null) out.push({ kind: 'merge', task, title, pr })
  else if (needs?.question_wait !== true && handoffOf(task) !== null) out.push({ kind: 'handoff', task, title })
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
 * Every row the list's Needs-you bar draws for `workflows`, in their order and
 * each workflow's step order. `nameOf` is the list row's name for a workflow;
 * a row's title is that name and the step's id. `unknownKinds` names the
 * kinds whose count is not known: `NEEDS_BLOCK_KINDS` when any step with a
 * task was served no needs block.
 */
export function listNeedItems(
  workflows: readonly Workflow[],
  taskById: ReadonlyMap<string, Task> | null,
  nameOf: (w: Workflow) => string,
): NeedItems {
  const out: NeedItem[] = []
  let blind = false
  for (const w of workflows) {
    for (const step of w.steps) {
      if (!step.task_id) continue
      const needs = stepNeedsOf(step)
      if (needs === null) blind = true
      const task = taskById?.get(step.task_id) ?? summaryTask(w, step)
      if (task === null || task === undefined) continue
      out.push(...stepNeedItems(task, needs, `${nameOf(w)} › ${step.step_id}`))
    }
  }
  return Object.assign(out, { unknownKinds: blind ? NEEDS_BLOCK_KINDS : [] })
}
