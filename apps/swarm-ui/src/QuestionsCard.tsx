/**
 * "QUESTIONS FOR YOU": what the task's agent asked its owner (owner report
 * 2026-10-13, part of #1206: "what does it mean 'your agent asked a
 * question' as a notification, there is nothing on the agent page about
 * questions").
 *
 * An agent that meets a decision that is the owner's writes it into
 * `questions.json` instead of guessing; the worker validates the file and
 * counts it in `result_summary.questions`, and the bell says "Your agent
 * asked N question(s)". This card is where that notification lands: the
 * top of the task's Details (`/agents/<list>/<id>#questions`), each question
 * with its context, its options with their descriptions, and the
 * recommended one marked -- read from `GET /v1/tasks/{id}/questions`, the
 * file the worker validated, redacted by the artifact read.
 *
 * THERE IS NO ANSWER BOX, and the card says so. The questions are data for a
 * person, never a channel: the answer goes back through whoever launched the
 * agent (the session or operator that ran the workflow), who reads them with
 * `swarm_result`. Whether to answer in the console is the owner's decision,
 * asked separately; nothing here answers.
 *
 * OPENING IT READS THE NOTIFICATION. When the card is drawn with questions,
 * the bell's `task:questions:<id>` item is marked read through the feed's own
 * `markRead`, once, and only while the feed lists it unread.
 *
 * NOTHING IS READ FOR A TASK THAT ASKED NONE: the count is on the task
 * document already, so a task without questions costs no request.
 */
import { useEffect, useRef, useSyncExternalStore } from 'react'

import { loadQuestions, type AgentQuestion, type TaskQuestions } from './api'
import { errorHeading } from './fetch'
import { feedData, markRead, notificationsSnapshot, subscribeNotifications } from './notificationFeed'
import { useRead } from './RunFiles'
import type { Task } from './types'

/** How many questions the worker validated in the task's `questions.json`, else 0. */
export function questionCount(task: Pick<Task, 'result_summary'> | null): number {
  const n = (task?.result_summary as Record<string, unknown> | null | undefined)?.questions
  return typeof n === 'number' && Number.isInteger(n) && n > 0 ? n : 0
}

/** The bell's item for this task's questions (`notifications.agent_questions`). */
export function questionsNotificationId(taskId: string): string {
  return `task:questions:${taskId}`
}

/** `1 question`, `2 questions`. */
function questionsWord(n: number): string {
  return `${n} question${n === 1 ? '' : 's'}`
}

/** Who started the agent, as the card names them: the workflow, then the person. */
function launcher(task: Task): string {
  const by = task.submitted_by ? ` (${task.submitted_by})` : ''
  return task.workflow_id ? `whoever launched workflow ${task.workflow_id}${by}` : `whoever launched this agent${by}`
}

/** Mark the bell's item read once the card is on screen, while the feed lists it unread. */
function useReadsNotification(taskId: string, shown: boolean): void {
  const feed = useSyncExternalStore(subscribeNotifications, notificationsSnapshot, notificationsSnapshot)
  const done = useRef<string | null>(null)
  const id = questionsNotificationId(taskId)
  const unread = feedData(feed)?.items.some((i) => i.id === id && !i.read) ?? false
  useEffect(() => {
    if (!shown || !unread || done.current === id) return
    done.current = id
    void markRead([id])
  }, [shown, unread, id])
}

function Question({ q, n }: { q: AgentQuestion; n: number }) {
  return (
    <li className="qc-q">
      <p className="qc-text">
        <span className="qc-n">{n}.</span> {q.question ?? '—'}
      </p>
      {q.context && <p className="dt-note qc-context">{q.context}</p>}
      <ul className="qc-options" aria-label={`Options for question ${n}`}>
        {q.options.map((o, i) => {
          const rec = q.recommended !== null && o.label === q.recommended
          return (
            <li key={`${i}:${o.label ?? ''}`} className={`qc-option${rec ? ' is-recommended' : ''}`}>
              <b className="qc-label">{o.label ?? '—'}</b>
              {rec && <span className="dt-chip qc-rec">recommended</span>}
              {o.description && <span className="qc-desc">{o.description}</span>}
            </li>
          )
        })}
      </ul>
    </li>
  )
}

/** The card, at the top of Details; nothing when the agent asked none. */
export function QuestionsCard({ task }: { task: Task }) {
  const count = questionCount(task)
  if (count === 0) return null
  return <AskedCard task={task} count={count} />
}

function AskedCard({ task, count }: { task: Task; count: number }) {
  // Keyed on the attempt count, so a re-run's file is read once it ends.
  const { state } = useRead<TaskQuestions>(() => loadQuestions(task.id), task.id, `${task.attempt_count}`, null)
  const data = state.status === 'ok' || state.status === 'stale' ? state.data : null
  const questions = data?.status === 'ok' ? data.questions : []
  useReadsNotification(task.id, questions.length > 0)
  const who = task.step_id ? `This agent (step ${task.step_id})` : 'This agent'

  return (
    <section id="questions" className="dt-card qc" aria-label="Questions for you">
      <div className="dt-card-head">
        <b>Questions for you</b>
        <span className="dt-card-r">
          <span className="dt-chip">{questionsWord(count)}</span>
        </span>
      </div>
      <p className="dt-note">
        {who} asked you {count === 1 ? 'this' : 'these'} instead of guessing. There is no answer box here yet:
        your answer goes back through {launcher(task)}, the session or operator that reads them with swarm_result.
      </p>
      {state.status === 'loading' ? (
        <p className="dt-note">Reading the questions…</p>
      ) : state.status === 'error' ? (
        <p className="dt-note">The questions could not be read: {errorHeading(state.error)}</p>
      ) : data === null || data.status === 'none' ? (
        <p className="dt-note">The questions could not be read: the API served none.</p>
      ) : data.status === 'unavailable' ? (
        <p className="dt-note">The questions could not be read: {data.unavailable_because ?? 'the API did not say why'}.</p>
      ) : (
        <ol className="qc-list">
          {questions.map((q, i) => (
            <Question key={i} q={q} n={i + 1} />
          ))}
        </ol>
      )}
      {data !== null && data.redacted && (
        <p className="dt-note">{data.redaction_count} value(s) that looked like a credential are masked.</p>
      )}
    </section>
  )
}
