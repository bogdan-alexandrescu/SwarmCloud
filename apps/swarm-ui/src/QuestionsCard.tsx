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
 * THE OWNER ANSWERS HERE (owner decision 2026-10-13, "Yes, pick an option on
 * the task"; lane TASK-ANSWERS). Each question's options are radio buttons,
 * the recommended one marked, with "Other…" for the person's own words, and
 * Answer posts `POST /v1/tasks/{id}/answers`. The answer is stored ON THE
 * TASK, where whoever launched the agent reads it beside the questions
 * (`swarm_result`'s `owner_answers`), and once every question is answered the
 * bell stops asking. A saved answer is drawn with who gave it and when, and
 * "Change" reopens the options with it picked; the API keeps the replaced one.
 * Only the task's submitter or an admin may answer: the API says whether this
 * viewer may (`may_answer`) and why not, and the card draws the buttons
 * disabled with that reason rather than guessing the rule itself.
 *
 * OPENING IT READS THE NOTIFICATION. When the card is drawn with questions,
 * the bell's `task:questions:<id>` item is marked read through the feed's own
 * `markRead`, once, and only while the feed lists it unread.
 *
 * NOTHING IS READ FOR A TASK THAT ASKED NONE: the count is on the task
 * document already, so a task without questions costs no request.
 */
import { useEffect, useRef, useState, useSyncExternalStore } from 'react'

import { answerQuestion, loadQuestions, type AgentQuestion, type OwnerAnswer, type TaskQuestions } from './api'
import { Button } from './components'
import { errorHeading, type ApiError } from './fetch'
import { feedData, markRead, notificationsSnapshot, subscribeNotifications } from './notificationFeed'
import { useRead } from './RunFiles'
import { timeAgo, type Task } from './types'

/** The "Other…" pick: the person's own words instead of one of the options. */
const OTHER = '\u0000other'

/** The longest free-text answer the API takes (`agent_questions.MAX_ANSWER_CHARS`). */
const MAX_ANSWER_CHARS = 2000

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

/** The option row's content: its label, the recommended chip, its description. */
function OptionBody({ q, label, description }: { q: AgentQuestion; label: string | null; description: string | null }) {
  const rec = q.recommended !== null && label === q.recommended
  return (
    <>
      <b className="qc-label">{label ?? '—'}</b>
      {rec && <span className="dt-chip qc-rec">recommended</span>}
      {description && <span className="qc-desc">{description}</span>}
    </>
  )
}

/** The saved answer: what, who and when, and "Change" for a viewer who may answer. */
function SavedAnswer({ a, n, onChange }: { a: OwnerAnswer; n: number; onChange: (() => void) | null }) {
  return (
    <div className="qc-saved" role="group" aria-label={`Your answer to question ${n}`}>
      <span className="qc-saved-what">
        Answered: {a.choice && <b>{a.choice}</b>}
        {a.choice && a.text && ' — '}
        {a.text && <span className="qc-saved-text">{a.text}</span>}
      </span>
      <span className="dt-note qc-saved-who">
        {a.by ?? 'someone'}
        {a.at ? `, ${timeAgo(a.at)}` : ''}
        {a.history.length > 0 ? ` · changed ${a.history.length}×` : ''}
      </span>
      {onChange && (
        <Button size="sm" kind="ghost" onClick={onChange}>
          Change
        </Button>
      )}
    </div>
  )
}

function Question({
  q,
  n,
  taskId,
  mayAnswer,
  onSaved,
}: {
  q: AgentQuestion
  n: number
  taskId: string
  mayAnswer: boolean
  onSaved: (fresh: TaskQuestions) => void
}) {
  const saved = q.answer ?? null
  const [editing, setEditing] = useState(saved === null)
  const [pick, setPick] = useState<string | null>(saved?.choice ?? (saved?.text ? OTHER : null))
  const [text, setText] = useState(saved?.choice ? '' : (saved?.text ?? ''))
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<ApiError | null>(null)
  const ownWords = text.trim()
  const ready = mayAnswer && !busy && (pick === OTHER ? ownWords.length > 0 && ownWords.length <= MAX_ANSWER_CHARS : pick !== null)

  const submit = async (): Promise<void> => {
    if (!ready || pick === null) return
    setBusy(true)
    setFailed(null)
    const r = await answerQuestion(taskId, {
      question_index: n - 1,
      choice: pick === OTHER ? null : pick,
      text: pick === OTHER ? ownWords : null,
    })
    setBusy(false)
    if (r.status === 'ok' || r.status === 'stale') {
      setEditing(false)
      onSaved(r.data)
    } else if (r.status === 'error') {
      setFailed(r.error)
    }
  }

  return (
    <li className="qc-q">
      <p className="qc-text">
        <span className="qc-n">{n}.</span> {q.question ?? '—'}
      </p>
      {q.context && <p className="dt-note qc-context">{q.context}</p>}
      {!editing && saved !== null ? (
        <>
          <ul className="qc-options" aria-label={`Options for question ${n}`}>
            {q.options.map((o, i) => {
              const rec = q.recommended !== null && o.label === q.recommended
              const chosen = saved.choice !== null && o.label === saved.choice
              return (
                <li key={`${i}:${o.label ?? ''}`} className={`qc-option${rec ? ' is-recommended' : ''}${chosen ? ' is-chosen' : ''}`}>
                  <OptionBody q={q} label={o.label} description={o.description} />
                </li>
              )
            })}
          </ul>
          <SavedAnswer a={saved} n={n} onChange={mayAnswer ? () => setEditing(true) : null} />
        </>
      ) : (
        <div className="qc-pickset" role="radiogroup" aria-label={`Answer question ${n}`}>
          <ul className="qc-options" aria-label={`Options for question ${n}`}>
            {q.options.map((o, i) => {
              const rec = q.recommended !== null && o.label === q.recommended
              const on = o.label !== null && pick === o.label
              return (
                <li key={`${i}:${o.label ?? ''}`} className={`qc-option${rec ? ' is-recommended' : ''}${on ? ' is-chosen' : ''}`}>
                  <button
                    type="button"
                    role="radio"
                    aria-checked={on}
                    className="qc-pick"
                    disabled={!mayAnswer || busy || o.label === null}
                    onClick={() => setPick(o.label)}
                  >
                    <OptionBody q={q} label={o.label} description={o.description} />
                  </button>
                </li>
              )
            })}
          </ul>
          <div className={`qc-option qc-other${pick === OTHER ? ' is-chosen' : ''}`}>
            <button
              type="button"
              role="radio"
              aria-checked={pick === OTHER}
              className="qc-pick"
              disabled={!mayAnswer || busy}
              onClick={() => setPick(OTHER)}
            >
              <b className="qc-label">Other…</b>
              <span className="qc-desc">your own answer</span>
            </button>
            {pick === OTHER && (
              <textarea
                className="qc-own"
                aria-label={`Your own answer to question ${n}`}
                maxLength={MAX_ANSWER_CHARS}
                rows={3}
                value={text}
                disabled={!mayAnswer || busy}
                onChange={(e) => setText(e.target.value)}
              />
            )}
          </div>
          <div className="qc-actions">
            <Button size="sm" kind="primary" disabled={!ready} busy={busy ? 'Saving…' : false} onClick={() => void submit()}>
              Answer
            </Button>
            {saved !== null && mayAnswer && (
              <Button size="sm" kind="ghost" disabled={busy} onClick={() => setEditing(false)}>
                Keep the saved answer
              </Button>
            )}
          </div>
          {failed && <p className="dt-note qc-failed">The answer was not saved: {errorHeading(failed)}</p>}
        </div>
      )}
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
  // What the last saved answer returned, until the next read of the questions.
  const [fresh, setFresh] = useState<TaskQuestions | null>(null)
  const read = state.status === 'ok' || state.status === 'stale' ? state.data : null
  const data = fresh ?? read
  // Absent from an API older than answers: nobody may answer there.
  const mayAnswer = data?.may_answer === true
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
        {who} asked you {count === 1 ? 'this' : 'these'} instead of guessing. Pick an option or write your own: the
        answer is stored on this task, where {launcher(task)} reads it with swarm_result.
      </p>
      {data !== null && data.status === 'ok' && !mayAnswer && (
        <p className="dt-note qc-why-not">
          You can read these but not answer them: {data.may_answer_because ?? 'the API did not say you may answer'}.
        </p>
      )}
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
            <Question
              key={`${i}:${q.answer?.at ?? ''}`}
              q={q}
              n={i + 1}
              taskId={task.id}
              mayAnswer={mayAnswer}
              onSaved={setFresh}
            />
          ))}
        </ol>
      )}
      {data !== null && data.redacted && (
        <p className="dt-note">{data.redaction_count} value(s) that looked like a credential are masked.</p>
      )}
    </section>
  )
}
