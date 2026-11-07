/**
 * THE WORKFLOW PAGE'S NEEDS YOU CARD AND ITS QUESTIONS SECTION (BGD, 6
 * November 2026: "We need to have a way to act on questions in the workflow
 * page", and "lets have the needs you card start off as a one line collapsed
 * section ... it expands when the user clicks on it, one more click on the
 * header and it collapses back again into a row"; part of #2688).
 *
 * `WorkflowNeedsCard` is ONE ROW on every mount (BGD's pick: nothing is
 * remembered): "Needs you" and one mark per kind waiting, each its glyph and
 * count, its name the tooltip. The row is the header's button
 * (`aria-expanded`); pressed, the items open under it, each with its action:
 * Answer jumps to the step's block in the questions section, a failed step's
 * Retry is the Needs-you row's own (`StepRetryButton`), and the rest open
 * where they act today. Nothing waits: no card.
 *
 * `WorkflowQuestions` draws every step that asked, grouped by step (its name,
 * state and agent page), each answered in place by THE AGENT PAGE'S OWN CARDS
 * (`QuestionsCard`, `HeldQuestionsCard`), mounted unchanged, so the rules, the
 * answer route and the read notification stay one implementation. A step
 * whose questions are all answered reads as one collapsed row, who and when.
 * No step asked: no section.
 */
import { useState, type MouseEvent } from 'react'

import { buttonClass } from './components'
import { type ApiError } from './fetch'
import { ownerQuestionsHold } from './Handoff'
import { addressToPath } from './paths'
import { HeldQuestionsCard, QuestionsCard, questionCount } from './QuestionsCard'
import { timeAgo, type Task, type Workflow, type WorkflowStep } from './types'
import { StepRetryButton, mergeHeldBecause, type NeedItem, type NeedItems } from './work/NeedsYou'
import { openQuestionsByTask } from './work/StageGrid'
import './work/needs-you.css'
import './styles/workflow-questions.css'

/** The id of a step's block in the questions section. */
export function questionsBlockId(stepId: string): string {
  return `questions-${stepId}`
}

/**
 * Scroll to a step's questions and focus its first unanswered one (its first
 * enabled answer control), else the block itself. False when the step has no
 * block on the page.
 */
export function jumpToQuestions(stepId: string): boolean {
  const block = document.getElementById(questionsBlockId(stepId))
  if (block === null) return false
  if (typeof block.scrollIntoView === 'function') block.scrollIntoView({ block: 'start', behavior: 'smooth' })
  const first = block.querySelector<HTMLElement>('.qc-pickset [role="radio"]:not(:disabled), .qc-pickset textarea:not(:disabled)')
  ;(first ?? block).focus({ preventScroll: true })
  return true
}

const taskPath = (taskId: string, pane = ''): string => addressToPath(`work/task/${encodeURIComponent(taskId)}${pane === '' ? '' : `/${pane}`}`)

// ---------------------------------------------------------------------------
// Needs you
// ---------------------------------------------------------------------------

/** The card's kinds: a question parked on (blocking) is its own mark. */
export type CardKind = 'blocking' | 'question' | 'merge' | 'failed' | 'handoff'

export const CARD_KINDS: readonly CardKind[] = ['blocking', 'question', 'merge', 'failed', 'handoff']

export const CARD_WORD: Readonly<Record<CardKind, string>> = {
  blocking: 'Blocking question holding a step',
  question: 'Questions to answer',
  merge: 'Merge waiting',
  failed: 'Failed',
  handoff: 'Your turn in a browser',
}

const CARD_GLYPH: Readonly<Record<CardKind, string>> = { blocking: '⏸', question: '?', merge: '⇢', failed: '✗', handoff: '◉' }

/** A question whose agent is parked on it holds that step: blocking. */
export function cardKindOf(item: NeedItem, taskById: ReadonlyMap<string, Task> | null): CardKind {
  if (item.kind !== 'question') return item.kind
  return taskById?.get(item.taskId)?.state === 'PARKED' ? 'blocking' : 'question'
}

function stepOfTask(workflow: Workflow, taskId: string): string | null {
  return workflow.steps.find((s) => s.task_id === taskId)?.step_id ?? null
}

function itemTaskId(item: NeedItem): string {
  return item.kind === 'question' ? item.taskId : item.task.id
}

export function WorkflowNeedsCard({
  items,
  workflow,
  taskById,
  onChanged,
}: {
  items: NeedItems
  workflow: Workflow
  taskById: ReadonlyMap<string, Task> | null
  /** An action was accepted (a step re-run): the page reads again. */
  onChanged?: () => void
}) {
  // ALWAYS STARTS COLLAPSED (BGD's pick, 6 November 2026): never stored.
  const [open, setOpen] = useState(false)
  if (items.length === 0) return null
  const kinds = items.map((i) => cardKindOf(i, taskById))
  const counts = CARD_KINDS.map((k) => ({ k, n: kinds.filter((x) => x === k).length })).filter((c) => c.n > 0)
  const unknown = items.unknownKinds ?? []
  const bodyId = `wq-needs-${workflow.workflow_id}`
  const firstAsking = (k: CardKind): string | null => {
    const at = kinds.indexOf(k)
    return at < 0 ? null : stepOfTask(workflow, itemTaskId(items[at]!))
  }
  const onHeader = (e: MouseEvent<HTMLButtonElement>) => {
    // A QUESTION MARK JUMPS to its first step's questions; the rest of the row toggles.
    const mark = (e.target as Element).closest?.('[data-kind]')
    const k = mark?.getAttribute('data-kind') as CardKind | null | undefined
    if (k === 'question' || k === 'blocking') {
      const step = firstAsking(k)
      if (step !== null && jumpToQuestions(step)) return
    }
    setOpen((o) => !o)
  }
  return (
    <section className={`wq-needs${open ? ' is-open' : ''}`} aria-label="Needs you">
      <h2 className="wq-needs-h">
        <button type="button" className="wq-needs-btn" aria-expanded={open} aria-controls={bodyId} onClick={onHeader}>
          <b>Needs you</b>
          {counts.map(({ k, n }) => (
            <span key={k} className={`wq-mark is-${k}`} data-kind={k} title={`${CARD_WORD[k]}: ${n}`}>
              <span aria-hidden>{CARD_GLYPH[k]}</span>
              <span className="wq-vh">{CARD_WORD[k]} </span>
              {n}
            </span>
          ))}
          {unknown.length > 0 && (
            <span className="wq-mark is-unknown" title="Some steps' needs were not read: the total may be more">
              <span aria-hidden>+</span>
              <span className="wq-vh"> not known</span>
            </span>
          )}
          <span className="wq-caret" aria-hidden>
            {open ? '▴' : '▾'}
          </span>
        </button>
      </h2>
      <div id={bodyId} className="wq-needs-body" hidden={!open}>
        <ul className="ny-list" aria-label="Needs you">
          {items.map((item, i) => (
            <NeedsCardRow key={`${item.kind}:${itemTaskId(item)}:${item.kind === 'question' ? item.index : ''}`} item={item} kind={kinds[i]!} workflow={workflow} onChanged={onChanged} />
          ))}
        </ul>
      </div>
    </section>
  )
}

function KindMark({ k }: { k: CardKind }) {
  return (
    <span className={`ny-kind is-${k === 'blocking' ? 'question' : k}`} title={CARD_WORD[k]}>
      <span className="ny-glyph" aria-hidden>
        {CARD_GLYPH[k]}
      </span>
      <span className="wq-vh">{CARD_WORD[k]}</span>
    </span>
  )
}

function AnswerButton({ stepId, name }: { stepId: string | null; name: string }) {
  if (stepId === null) return null
  return (
    <button type="button" className={buttonClass({ size: 'sm', kind: 'primary' })} aria-label={`Answer ${name}`} onClick={() => jumpToQuestions(stepId)}>
      Answer
    </button>
  )
}

function OpenLink({ href, name, word = 'Open' }: { href: string; name: string; word?: string }) {
  return (
    <a className={buttonClass({ size: 'sm', kind: 'ghost' })} href={href} aria-label={`${word} ${name}`}>
      {word}
    </a>
  )
}

function NeedsCardRow({ item, kind, workflow, onChanged }: { item: NeedItem; kind: CardKind; workflow: Workflow; onChanged?: () => void }) {
  const [failed, setFailed] = useState<ApiError | null>(null)
  const stepId = stepOfTask(workflow, itemTaskId(item))
  const cls = `ny-row is-${item.kind}${kind === 'blocking' ? ' is-blocking' : ''}`
  if (item.kind === 'question') {
    const name = `${item.title} question ${item.index + 1}`
    return (
      <li className={cls}>
        <KindMark k={kind} />
        <b className="ny-title">{item.title}</b>
        <span className="ny-text" title={item.question ?? undefined}>
          {item.question ?? '—'}
        </span>
        <span className="ny-acts">
          <AnswerButton stepId={stepId} name={name} />
        </span>
      </li>
    )
  }
  if (item.kind === 'merge') {
    const hold = ownerQuestionsHold(item.task)
    const held = hold === null ? 0 : hold.questions.length + hold.unreadable
    return (
      <li className={cls}>
        <KindMark k={kind} />
        <b className="ny-title">{item.title}</b>
        <span className="ny-text" title={mergeHeldBecause(item) ?? undefined}>
          {held > 0 ? `held by ${held} ${held === 1 ? 'question' : 'questions'}` : item.approval ? 'waits for your approval' : 'waiting'}
        </span>
        <span className="ny-acts">
          {held > 0 && <AnswerButton stepId={stepId} name={item.title} />}
          <OpenLink
            href={item.approval ? addressToPath(`automate/approvals?approval=${encodeURIComponent(item.approval.id)}`) : taskPath(item.task.id)}
            name={item.title}
          />
        </span>
      </li>
    )
  }
  if (item.kind === 'failed') {
    return (
      <li className={cls}>
        <KindMark k={kind} />
        <b className="ny-title">{item.title}</b>
        <span className="ny-text" title={item.why ?? undefined}>
          {item.why ?? 'No error was read'}
        </span>
        <span className="ny-acts">
          {stepId !== null && <StepRetryButton workflowId={workflow.workflow_id} stepId={stepId} name={item.title} onFailed={setFailed} onChanged={onChanged} />}
          <OpenLink href={taskPath(item.task.id, 'logs')} name={item.title} />
        </span>
        {failed !== null && (
          <span className="ny-err" role="alert">
            Not re-run: {failed.message}
          </span>
        )}
      </li>
    )
  }
  return (
    <li className={cls}>
      <KindMark k={kind} />
      <b className="ny-title">{item.title}</b>
      <span className="ny-text">{CARD_WORD.handoff}</span>
      <span className="ny-acts">
        <OpenLink href={taskPath(item.task.id, 'browser')} name={item.title} word="Take your turn" />
        <OpenLink href={taskPath(item.task.id)} name={item.title} />
      </span>
    </li>
  )
}

// ---------------------------------------------------------------------------
// Questions
// ---------------------------------------------------------------------------

/** A step that asked: its own questions, or a merge held by the owner's questions. */
export interface AskingStep {
  step: WorkflowStep
  task: Task
  /** Its questions not yet answered (its own, or the ones holding its merge). */
  open: number
}

export function askingSteps(workflow: Workflow, taskById: ReadonlyMap<string, Task> | null): AskingStep[] {
  if (taskById === null) return []
  const openByTask = openQuestionsByTask(workflow.workflow_id, taskById)
  const out: AskingStep[] = []
  for (const step of workflow.steps) {
    const task = step.task_id ? taskById.get(step.task_id) : undefined
    if (task === undefined) continue
    const hold = ownerQuestionsHold(task)
    if (questionCount(task) === 0 && hold === null) continue
    const held = hold === null ? 0 : hold.questions.length + hold.unreadable
    out.push({ step, task, open: Math.max(openByTask.get(task.id) ?? 0, held) })
  }
  return out
}

export function WorkflowQuestions({
  workflow,
  taskById,
  stateOf,
}: {
  workflow: Workflow
  taskById: ReadonlyMap<string, Task> | null
  /** The step's state word, as the page draws it. */
  stateOf: (step: WorkflowStep) => string
}) {
  const steps = askingSteps(workflow, taskById)
  if (steps.length === 0) return null
  const open = steps.reduce((n, s) => n + s.open, 0)
  return (
    <section className="wq" aria-label="Questions">
      <h2 className="wq-h">
        Questions <span className="wq-n" title={`${open} not answered`}>{open}</span>
      </h2>
      {steps.map((s) => (
        <StepQuestions key={s.step.step_id} asking={s} state={stateOf(s.step)} />
      ))}
    </section>
  )
}

function lastAnswer(task: Task): { by: string | null; at: string | null } | null {
  const all = task.owner_answers ?? []
  if (all.length === 0) return null
  return [...all].sort((a, b) => (Date.parse(b.at ?? '') || 0) - (Date.parse(a.at ?? '') || 0))[0] ?? null
}

function StepQuestions({ asking, state }: { asking: AskingStep; state: string }) {
  const { step, task, open } = asking
  const answered = open === 0
  // AN ANSWERED STEP READS COLLAPSED; its cards are read only once opened.
  const [shown, setShown] = useState(!answered)
  const last = answered ? lastAnswer(task) : null
  const bodyId = `${questionsBlockId(step.step_id)}-body`
  return (
    <section id={questionsBlockId(step.step_id)} className={`wq-step${answered ? ' is-answered' : ''}`} tabIndex={-1} aria-label={`Questions: ${step.step_id}`}>
      <div className="wq-step-h">
        <h3>{step.step_id}</h3>
        <span className="wq-state">{state}</span>
        {answered ? (
          <button type="button" className="wq-answered" aria-expanded={shown} aria-controls={bodyId} onClick={() => setShown((v) => !v)}>
            <span aria-hidden>✓</span> answered
            {last?.by ? ` · ${last.by}` : ''}
            {last?.at ? <span title={last.at}> · {timeAgo(last.at)}</span> : null}
          </button>
        ) : (
          <span className="wq-open" title={`${open} not answered`}>
            ? {open}
          </span>
        )}
        <a className="wq-agent" href={taskPath(task.id)}>
          Agent page
        </a>
      </div>
      {shown && (
        <div id={bodyId} className="wq-step-body">
          <QuestionsCard task={task} />
          <HeldQuestionsCard task={task} />
        </div>
      )}
    </section>
  )
}
