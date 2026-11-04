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
 * A chip filters the list to its kind and reports the pick with the task ids
 * it covers (`onSelect`), so a page can dim the grid cells that do not match.
 * Mounted by the Work pages; it reads nothing on its own but the masked
 * prompt a Retry carries over.
 */
import { useEffect, useId, useRef, useState, type MouseEvent } from 'react'
import { answerQuestion, approveApproval, loadTaskInputOnce, rerunWorkflowStep, type ApprovalItem, type QuestionOption } from '../api'
import { Button, buttonClass } from '../components'
import { errorHeading, type ApiError } from '../fetch'
import { handoffOf, humanReady, ownerQuestionsHold, timeLeft, type HeldQuestion } from '../Handoff'
import { addressToPath } from '../paths'
import { useRoleGate } from '../Profile'
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

export function NeedsYou({
  items,
  approvals,
  selected,
  onSelect,
  onChanged,
}: {
  items: readonly NeedItem[]
  /** The approvals waiting, or null when the page could not read them ("not known"). */
  approvals: readonly ApprovalItem[] | null
  /** The kind picked, when the page holds the pick; left out, the component holds it. */
  selected?: NeedKind | null
  /** A chip was pressed: the kind picked (null when cleared) and the task ids it covers. */
  onSelect?: (pick: NeedSelection | null) => void
  /** An action was accepted (an answer saved, an approval given, I'm ready): the page reads again. */
  onChanged?: () => void
}) {
  const [own, setOwn] = useState<NeedKind | null>(null)
  const [unfolded, setUnfolded] = useState(false)
  const kind = selected === undefined ? own : selected
  const listId = useId()

  const count = (k: NeedKind): number | null => (k === 'approval' ? (approvals === null ? null : approvals.length) : items.filter((i) => i.kind === k).length)
  const total = items.length + (approvals?.length ?? 0)

  const pick = (k: NeedKind | null) => {
    setOwn(k)
    setUnfolded(false)
    if (onSelect === undefined) return
    if (k === null) return onSelect(null)
    const ids = k === 'approval' ? [] : items.filter((i) => i.kind === k).flatMap(taskIdsOf)
    onSelect({ kind: k, taskIds: [...new Set(ids)] })
  }

  return (
    <section className={`ny${total > 0 ? ' has-some' : ''}`} aria-label="Needs you">
      <div className="ny-bar" role="group" aria-label="What needs you, by kind">
        <button type="button" className="ny-all" aria-pressed={kind === null} aria-controls={listId} onClick={() => pick(null)}>
          <b>Needs you</b> <span className="ny-n">{total}</span>
          {approvals === null && <span className="ny-plus" title="Approvals are not known here, so the total may be more">+</span>}
        </button>
        {NEED_KINDS.map((k) => {
          const n = count(k)
          if (n === null) {
            return (
              <span key={k} className="ny-chip is-approval is-faint" title="Approvals are not read on this screen, so their count is not known; Automate › Approvals lists them">
                <span className="ny-glyph" aria-hidden>
                  {NEED_GLYPH[k]}
                </span>
                <span className="ny-word">{NEED_WORD[k]}</span> <b>not known</b>
              </span>
            )
          }
          return (
            <button
              key={k}
              type="button"
              className={`ny-chip is-${k}${n === 0 ? ' is-zero' : ''}`}
              aria-pressed={kind === k}
              aria-controls={listId}
              aria-label={`${NEED_WORD[k]}: ${n}`}
              title={`${NEED_WORD[k]}: ${n}`}
              onClick={() => pick(kind === k ? null : k)}
            >
              <span className="ny-glyph" aria-hidden>
                {NEED_GLYPH[k]}
              </span>
              <span className="ny-word">{NEED_WORD[k]}</span> <b>{n}</b>
            </button>
          )
        })}
      </div>
      <NeedsList id={listId} items={items} approvals={approvals ?? []} kind={kind} unfolded={unfolded} onUnfold={(k) => (k === null ? setUnfolded(true) : pick(k))} onChanged={onChanged} />
    </section>
  )
}

function NeedsList({
  id,
  items,
  approvals,
  kind,
  unfolded,
  onUnfold,
  onChanged,
}: {
  id: string
  items: readonly NeedItem[]
  approvals: readonly ApprovalItem[]
  kind: NeedKind | null
  unfolded: boolean
  onUnfold: (k: NeedKind | null) => void
  onChanged?: () => void
}) {
  if (items.length === 0 && approvals.length === 0) {
    return (
      <p className="ny-quiet" id={id}>
        Nothing waits on you here
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
        : items.filter((i) => i.kind === k).map((i) => <NeedRow key={keyOf(i)} item={i} onChanged={onChanged} />)
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

function NeedRow({ item, onChanged }: { item: NeedItem; onChanged?: () => void }) {
  switch (item.kind) {
    case 'failed':
      return <FailedRow item={item} onChanged={onChanged} />
    case 'question':
      return <QuestionRow item={item} onChanged={onChanged} />
    case 'merge':
      return <MergeRow item={item} onChanged={onChanged} />
    case 'handoff':
      return <HandoffRow item={item} onChanged={onChanged} />
  }
}

function FailedRow({ item, onChanged }: { item: FailedNeed; onChanged?: () => void }) {
  const workflowId = item.task.workflow_id ?? null
  const stepId = item.task.step_id ?? null
  const [failed, setFailed] = useState<ApiError | null>(null)
  return (
    <li className="ny-row is-failed">
      <Kind k="failed" />
      <b className="ny-title">{item.title}</b>
      <span className="ny-text" title={item.why ?? undefined}>
        {item.why ?? 'No error was read'}
      </span>
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
function StepRetryButton({
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

function QuestionRow({ item, onChanged }: { item: QuestionNeed; onChanged?: () => void }) {
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
  const notAllowed = roleGate ?? (item.mayAnswer === false ? (item.mayAnswerBecause ?? 'The API did not say you may answer') : null)
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
    <li className={`ny-row is-question${open ? ' is-open' : ''}`}>
      <Kind k="question" />
      <b className="ny-title">{item.title}</b>
      <span className="ny-text" title={item.question ?? undefined}>
        {item.question ?? 'The question could not be read'}
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
