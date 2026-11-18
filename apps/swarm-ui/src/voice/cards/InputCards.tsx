/**
 * VOICE IT'S INPUT CARDS: details input (docs/design/voice-it.md §4's table;
 * section 10, phase two). The credential input card is a later lane's
 * (`CredentialCard.tsx`), and it never carries a value.
 *
 * A DETAILS CARD ASKS ONE OF TWO THINGS.
 *
 *   * A lane's waiting question: the card names the step's task (`task_id`)
 *     and its workflow (`references.workflow`). It is drawn by the console's
 *     own await-input card (AwaitInputCard.tsx), the same one the workflow's
 *     step inspector draws, so the answer goes through the workflow's
 *     ordinary input route with its checks, its idempotency key and its
 *     first-answer-is-final rule. The step is found by its task in
 *     `GET /v1/workflows/{id}`; a task with no such step is said, with a
 *     link to the task.
 *   * The planner's own question: the question, its options and whether a
 *     free answer is welcome. It is answered by talking; the planner writes
 *     the answer back onto the card, which then shows it.
 *
 * Only the catalogue's fields are read (CardList.tsx hands over nothing
 * else).
 */

import { useEffect, useState } from 'react'

import { AwaitInputCard } from '../../AwaitInputCard'
import { loadWorkflow, type WorkflowRead } from '../../api'
import { Chip } from '../../components'
import { inspectorHref } from '../../decision'
import { errorHeading, type Result } from '../../fetch'
import type { CardProps } from '../CardList'
import { list, obj, str, VoiceCardFrame } from './WorkCards'

/** The workflow step whose task is `taskId`, or null. */
export function stepOfTask(read: WorkflowRead, taskId: string): string | null {
  const step = read.workflow.steps.find((s) => s.task_id === taskId)
  if (step !== undefined) return step.step_id
  return read.tasks.find((t) => t.id === taskId)?.step_id ?? null
}

/** A lane's waiting question, drawn by the console's await-input card. */
function LaneQuestion({ workflowId, taskId }: { workflowId: string; taskId: string }) {
  const [r, setR] = useState<Result<WorkflowRead>>({ status: 'loading', since: Date.now() })
  useEffect(() => {
    let live = true
    void loadWorkflow(workflowId).then((next) => {
      if (live) setR(next)
    })
    return () => {
      live = false
    }
  }, [workflowId])
  if (r.status === 'loading') return <p className="sb-note">Reading the waiting step…</p>
  if (r.status === 'error') {
    return (
      <p className="c-hint" role="alert">
        The waiting step was not read: {errorHeading(r.error)}. {r.error.message}
      </p>
    )
  }
  const step = r.status === 'empty' ? null : stepOfTask(r.data, taskId)
  if (step === null) {
    return (
      <p className="sb-note">
        No step of workflow <span className="mono">{workflowId}</span> runs task{' '}
        <a className="c-link" href={inspectorHref(taskId)}>
          {taskId}
        </a>
        .
      </p>
    )
  }
  return <AwaitInputCard workflowId={workflowId} stepId={step} />
}

export function DetailsInputCard({ card, spec, body }: CardProps) {
  const taskId = str(body.task_id)
  const workflowId = str(obj(card.references).workflow)
  const question = str(body.question)
  const options = list(body.options).map(str).filter((o): o is string => o !== null)
  const answer = str(body.answer)
  const freeText = body.free_text === true
  if (taskId !== null && workflowId !== null) {
    return (
      <VoiceCardFrame card={card} spec={spec}>
        <LaneQuestion workflowId={workflowId} taskId={taskId} />
      </VoiceCardFrame>
    )
  }
  return (
    <VoiceCardFrame card={card} spec={spec}>
      {question !== null && <p className="vt-card-text">{question}</p>}
      {options.length > 0 && (
        <p className="vt-card-refs" aria-label="Options">
          {options.map((o) => (
            <Chip key={o} faint={answer !== null && answer !== o}>
              {o}
            </Chip>
          ))}
        </p>
      )}
      {answer !== null ? (
        <p className="vt-card-answer">
          Your answer: <b>{answer}</b>
        </p>
      ) : (
        card.state === 'waiting' && (
          <p className="sb-note">
            {options.length > 0 ? 'Say which one' : 'Say your answer'}
            {options.length > 0 && freeText ? ', or answer in your own words' : ''}; Voice it writes it here.
          </p>
        )
      )}
      {taskId !== null && (
        <p className="sb-note">
          Asked for task{' '}
          <a className="c-link" href={inspectorHref(taskId)}>
            {taskId}
          </a>
          .
        </p>
      )}
    </VoiceCardFrame>
  )
}
