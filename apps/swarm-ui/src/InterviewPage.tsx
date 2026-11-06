// A plugin step's interview as the whole page, and a planner run's proposal as
// a page of its own: direction B's plan screens, the layout the owner picked on
// 4 November 2026 (docs/design/agent-plugins.md, agent-plugins-question-bridge.md).
//
// The interview is one task's questions as `GET /v1/tasks/{id}/questions`
// serves them, with its `waiting` block (the clock, the skill that asked).
// The pending question is the page: its options side by side, each option's
// design drawn inside its card when the question served one, the recommended
// one marked and picked. On the left a rail lists the questions answered so
// far, who answered and where. A bar stays at the foot with the time left and
// the answer. Answers go through `POST /v1/tasks/{id}/answers`, as `console`.
//
// A rendered design is HTML the agent wrote. It is drawn in an iframe with an
// EMPTY sandbox -- no script, no same-origin access, no forms, no top
// navigation -- from `srcDoc`, never as live markup in the console.
//
// The proposal page is the issue run's plan, decided on its digest with the
// run page's own calls and forms (Runs.tsx's PlanEditor and RejectForm,
// approvePlan / editPlan / rejectPlan): a plan that changed first is refused
// and read again, never decided on blind.

import { useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import {
  answerQuestion, approvePlan, editPlan, loadQuestions, loadRun, loadTask, loadWorkspacePlugins, rejectPlan,
  type AgentQuestion, type QuestionOption, type TaskQuestions,
} from './api'
import type { ApiError, Result } from './fetch'
import { Button, Chip } from './components'
import { FailedPanel, PageHead, Screen } from './Shell'
import { AUTO_APPROVER, PlanEditor, RejectForm, runStateWord, shortDigest } from './Runs'
import { leftOfWait } from './work/NeedsYou'
import type { CataloguePlugin, IssueRun, OwnerAnswer, RunPlan, Task } from './types'
import { useNow } from './useNow'
import { addressToPath } from './paths'
import { interviewAddress, type InterviewAt } from './interviewPaths'
import './styles/interview.css'

export { INTERVIEW_KEY, PROPOSAL_KEY, interviewAddress, interviewPageOf, proposalAddress, type InterviewAt } from './interviewPaths'


export function InterviewRoute({ at, go }: { at: InterviewAt; go: (to: string) => void }) {
  return at.kind === 'interview'
    ? <InterviewScreen key={at.taskId} taskId={at.taskId} go={go} />
    : <ProposalScreen key={at.runId} runId={at.runId} go={go} />
}

// ---------------------------------------------------------------------------
// Words
// ---------------------------------------------------------------------------

/** The CLI marks the recommended option by ending its label in "(Recommended)"; the chip says it once. */
export function plainLabel(label: string | null): string {
  return (label ?? 'no label').replace(/\s*\(Recommended\)$/, '')
}

/** A, B, C … for the option at `i`. */
export const optionKey = (i: number): string => String.fromCharCode(65 + i)

/**
 * Where an answer was given, as `OwnerAnswerVersion.via` serves it. The
 * console's own answers are sent as `console`; an answer stored before the
 * API recorded where (or by a client that left it out) says so instead of
 * being counted as the console's.
 */
export type ServedVia = 'console' | 'voice-it' | 'claude-code' | 'github-comment'
const VIA_WORD: Readonly<Record<ServedVia, string>> = {
  console: 'Console', 'voice-it': 'Voice it', 'claude-code': 'Claude Code', 'github-comment': 'GitHub',
}
export function viaWord(via: string | null | undefined): string {
  return via !== null && via !== undefined && via in VIA_WORD ? VIA_WORD[via as ServedVia] : 'where: not recorded'
}

/** The modes' words, as the catalogue's picker says them. */
const MODE_WORD: Readonly<Record<string, string>> = { unattended: 'Unattended', planner: 'Planner', designer: 'Designer' }
const modeWord = (mode: string): string => MODE_WORD[mode] ?? mode

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
/** "19 Oct, 11:22 UTC"; null for a time not served. */
export function atUtc(iso: string | null | undefined): string | null {
  const ms = typeof iso === 'string' ? Date.parse(iso) : NaN
  if (!Number.isFinite(ms)) return null
  const d = new Date(ms)
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}, ${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')} UTC`
}

/** Two letters for an answerer's mark, from the address's name part. */
export function initials(email: string | null): string {
  if (email === null || email === '') return '?'
  const parts = (email.split('@')[0] ?? email).split(/[._-]+/).filter(Boolean)
  return (parts.length > 1 ? `${parts[0]![0]}${parts[1]![0]}` : (parts[0] ?? email).slice(0, 2)).toUpperCase()
}

/** "example-org/contacts-app" for a forge URL; the value as served otherwise. */
function repoName(url: string): string {
  const m = /^https?:\/\/[^/]+\/(.+?)(?:\.git)?\/?$/.exec(url)
  return m?.[1] ?? url
}

// ---------------------------------------------------------------------------
// What the questions route serves beyond the shared `TaskQuestions` type
// ---------------------------------------------------------------------------

/** `waiting` on a question wait (`agent_questions._serve_wait`). */
export interface QuestionWait {
  open: boolean
  blocking: number[]
  unanswered: number[]
  asked_at: string | null
  expires_at: string | null
  issue_comment_url: string | null
  skill: string | null
}

/** An option as the bridge carries it: `preview`, the design's HTML, only when the route serves it. */
type ServedOption = QuestionOption & { preview?: unknown }
type ServedQuestion = AgentQuestion & { blocking?: boolean; options: ServedOption[] }
export type InterviewQuestions = TaskQuestions & { waiting?: QuestionWait | null; questions: ServedQuestion[] }

/** The design an option served, as HTML, or null. */
export function previewOf(option: QuestionOption): string | null {
  const p = (option as ServedOption).preview
  return typeof p === 'string' && p.trim() !== '' ? p : null
}

/** The question the page asks: the wait's first unanswered blocking one, else the first unanswered. */
export function pendingIndex(read: InterviewQuestions): number | null {
  const unanswered = read.waiting?.unanswered ?? []
  for (const i of unanswered) if (read.questions[i] !== undefined && (read.questions[i]!.answer ?? null) === null) return i
  const i = read.questions.findIndex((q) => (q.answer ?? null) === null)
  return i < 0 ? null : i
}

/** The step's plugins as its work contract holds them (`capabilities.plugins`), served on the task's metadata. */
export function taskPlugins(task: Task | null): { name: string; mode: string; skills: string[] }[] {
  const contract = task?.metadata?.work_contract
  const caps = typeof contract === 'object' && contract !== null ? (contract as Record<string, unknown>).capabilities : null
  const stored = typeof caps === 'object' && caps !== null ? (caps as Record<string, unknown>).plugins : null
  if (typeof stored !== 'object' || stored === null || Array.isArray(stored)) return []
  return Object.entries(stored as Record<string, unknown>).flatMap(([name, p]) => {
    if (typeof p !== 'object' || p === null) return []
    const { mode, skills } = p as { mode?: unknown; skills?: unknown }
    if (typeof mode !== 'string' || mode === '') return []
    return [{ name, mode, skills: Array.isArray(skills) ? skills.filter((s): s is string => typeof s === 'string') : [] }]
  })
}

// ---------------------------------------------------------------------------
// The interview
// ---------------------------------------------------------------------------

interface InterviewRead {
  questions: InterviewQuestions
  task: Task | null
  catalogue: CataloguePlugin[] | null
}

async function loadInterview(taskId: string): Promise<Result<InterviewRead>> {
  const [q, t, c] = await Promise.all([loadQuestions(taskId), loadTask(taskId), loadWorkspacePlugins()])
  if (q.status !== 'ok' && q.status !== 'stale') return q as Result<InterviewRead>
  return {
    ...q,
    data: {
      questions: q.data as InterviewQuestions,
      task: t.status === 'ok' || t.status === 'stale' ? t.data : null,
      catalogue: c.status === 'ok' || c.status === 'stale' ? c.data.catalogue : null,
    },
  }
}

function InterviewScreen({ taskId, go }: { taskId: string; go: (to: string) => void }) {
  return (
    <Screen title="Interview" load={() => loadInterview(taskId)} pollMs={15_000}>
      {(data) => <InterviewView taskId={taskId} read={data} go={go} />}
    </Screen>
  )
}

/** One answer the console sent and the API accepted, drawn at once over the next read. */
interface Sent { index: number; served: InterviewQuestions }

export function InterviewView({ taskId, read, go }: { taskId: string; read: InterviewRead; go: (to: string) => void }) {
  const now = useNow(30_000)
  const [sent, setSent] = useState<Sent | null>(null)
  const served = sent?.served ?? read.questions
  const wait = served.waiting ?? null
  const pending = sent === null ? pendingIndex(served) : null
  const task = read.task
  const plugins = taskPlugins(task)
  const title = task?.step_id ?? taskId

  const answered = served.questions
    .map((q, i) => ({ q, i }))
    .filter(({ q, i }) => (q.answer ?? null) !== null && i !== sent?.index)
  const viaCounts = new Map<string, number>()
  for (const { q } of answered) {
    const w = viaWord(q.answer!.via)
    viaCounts.set(w, (viaCounts.get(w) ?? 0) + 1)
  }
  if (sent !== null) viaCounts.set('Console', (viaCounts.get('Console') ?? 0) + 1)

  return (
    <div className="iv-page">
      <PageHead title={title} />
      <div className="iv-facts" aria-label="The step">
        <b>{task?.step_id ?? 'step not known'}</b>
        {task?.workflow_id && <span className="iv-dim">in {task.workflow_id}</span>}
        {task?.repository_url
          ? <><span className="iv-dim">on</span> <code>{repoName(task.repository_url)}</code></>
          : <span className="iv-dim">repository not known</span>}
        {plugins.map((p) => {
          const version = read.catalogue?.find((c) => c.name === p.name)?.version ?? null
          return (
            <span key={p.name} className="iv-sumchip is-static" title={`${p.name} ${modeWord(p.mode)}${p.skills.length > 0 ? `: ${p.skills.join(', ')}` : ''}`}>
              <span className="iv-sumchip-k">{p.name} {version ?? '(version not known)'}</span>
              <span>{modeWord(p.mode)}</span>
              {p.skills.length > 0 && <span className="iv-sumchip-s">{p.skills.join(' · ')}</span>}
            </span>
          )
        })}
        {sent !== null
          ? <Chip tone="live" title="The answer is stored; the step starts again with it">Answered: queued to resume</Chip>
          : wait?.open
            ? <Chip tone="park" title="While it waits the step holds no lease, no slot and no container">Parked: holds no machine</Chip>
            : null}
      </div>

      <div className="iv-grid">
        <Rail served={served} answered={answered} viaCounts={viaCounts} pending={pending} sent={sent} />
        <main className="iv-main">
          {sent !== null ? (
            <Done served={sent.served} index={sent.index} />
          ) : pending === null ? (
            <NoPending served={served} />
          ) : (
            <Pending key={pending} taskId={taskId} served={served} index={pending} now={now}
              onSent={(s) => setSent({ index: pending, served: s })} />
          )}
          {sent !== null && (
            <div className="iv-dbar">
              <div className="iv-dbar-left">
                <b>Answer saved</b>
                {wait?.skill
                  ? <span className="iv-dim iv-small">A second answer to question {sent.index + 1} is refused: the first one stands.</span>
                  : <span className="iv-dim iv-small">Answered in the console as {sent.served.questions[sent.index]?.answer?.by ?? 'you'}.</span>}
              </div>
              <div className="iv-dbar-acts">
                <Button kind="ghost" onClick={() => go('work/running')}>Back to Needs you</Button>
              </div>
            </div>
          )}
        </main>
      </div>
    </div>
  )
}

function Rail({ served, answered, viaCounts, pending, sent }: {
  served: InterviewQuestions
  answered: { q: ServedQuestion; i: number }[]
  viaCounts: ReadonlyMap<string, number>
  pending: number | null
  sent: Sent | null
}) {
  const list = useRef<HTMLOListElement>(null)
  // The current question sits at the rail's end: open the rail scrolled to it.
  useLayoutEffect(() => {
    const el = list.current
    if (el !== null) el.scrollTop = el.scrollHeight
  }, [])
  const current = sent?.index ?? pending
  const q = current === null ? null : served.questions[current]
  const just = sent === null ? null : sent.served.questions[sent.index]?.answer ?? null
  return (
    <nav className="iv-rail" aria-label="Questions answered so far">
      <div className="iv-rail-h">
        <b>{answered.length + (sent === null ? 0 : 1)} answered</b>
        <span className="iv-dim iv-small">how many more: not known</span>
        {viaCounts.size > 0 && (
          <span className="iv-row iv-small" aria-label="Where they were answered">
            {[...viaCounts].map(([w, n]) => <span key={w} className="iv-via">{w} <b>{n}</b></span>)}
          </span>
        )}
      </div>
      <ol className="iv-rail-list" role="list" ref={list}>
        {answered.map(({ q: r, i }) => (
          <li key={i} className="iv-rail-item">
            <span className="iv-rail-n">{i + 1}</span>
            <div className="iv-rail-body">
              <span className="iv-rail-q" title={r.question ?? undefined}>{r.question ?? 'question not served'}</span>
              <span className="iv-rail-a">{r.answer!.choice !== null ? plainLabel(r.answer!.choice) : r.answer!.text ?? 'no answer text'}</span>
              <Who answer={r.answer!} />
            </div>
          </li>
        ))}
        {q !== null && q !== undefined && current !== null && (
          <li className={`iv-rail-item ${sent !== null ? 'is-just' : 'is-now'}`} aria-current={sent !== null ? undefined : 'step'}>
            <span className="iv-rail-n">{current + 1}</span>
            <div className="iv-rail-body">
              <span className="iv-rail-q" title={q.question ?? undefined}>{q.question ?? 'question not served'}</span>
              {just !== null
                ? <><span className="iv-rail-a">{just.choice !== null ? plainLabel(just.choice) : just.text}</span><Who answer={just} late="just now" /></>
                : <span className="iv-rail-a is-now">Waiting for an answer</span>}
            </div>
          </li>
        )}
      </ol>
      <p className="iv-rail-end iv-small">
        <b>Then:</b> when the interview ends, its proposal is on its run&rsquo;s page, to Approve, Edit or Reject, unless
        the run was started to approve automatically.
      </p>
    </nav>
  )
}

function Who({ answer, late }: { answer: OwnerAnswer; late?: string }) {
  return (
    <span className="iv-rail-who">
      <span className="iv-av" aria-hidden="true">{initials(answer.by)}</span>
      {answer.by ?? 'who: not recorded'} <span className={`iv-via is-${answer.via ?? 'none'}`}>{viaWord(answer.via)}</span>
      {late !== undefined && <span className="iv-dim">{late}</span>}
    </span>
  )
}

function NoPending({ served }: { served: InterviewQuestions }) {
  if (served.status === 'unavailable') {
    return <p className="iv-next"><b>The questions could not be read.</b> {served.unavailable_because ?? ''}</p>
  }
  if (served.questions.length === 0) return <p className="iv-next">This step has asked no question.</p>
  return (
    <p className="iv-next">
      <b>Every question is answered.</b> The step starts again with the answers, in the same conversation; it holds no
      machine until then. The next question or the proposal appears here when it comes.
    </p>
  )
}

function Pending({ taskId, served, index, now, onSent }: {
  taskId: string
  served: InterviewQuestions
  index: number
  now: number
  onSent: (served: InterviewQuestions) => void
}) {
  const q = served.questions[index]!
  const wait = served.waiting ?? null
  const rec = q.recommended !== null && q.options.some((o) => o.label === q.recommended) ? q.recommended : null
  const [picked, setPicked] = useState<string | null>(rec ?? q.options[0]?.label ?? null)
  const [ownWords, setOwnWords] = useState(q.options.length === 0)
  const [text, setText] = useState('')
  const [big, setBig] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<ApiError | null>(null)
  const mayAnswer = served.may_answer !== false
  const n = index + 1
  const expiresMs = wait?.expires_at ? Date.parse(wait.expires_at) : NaN
  const askedMs = wait?.asked_at ? Date.parse(wait.asked_at) : NaN
  const left = Number.isFinite(expiresMs) ? leftOfWait(expiresMs, now) : null
  const span = Number.isFinite(expiresMs) && Number.isFinite(askedMs) && expiresMs > askedMs
    ? `${Math.round((expiresMs - askedMs) / 3_600_000)} h` : null
  const pickedAt = q.options.findIndex((o) => o.label === picked)

  async function send(choice: string | null, words: string | null) {
    setBusy(true)
    setFailed(null)
    const r = await answerQuestion(taskId, { question_index: index, choice, text: words, via: 'console' })
    setBusy(false)
    if (r.status === 'ok' || r.status === 'stale') onSent(r.data as InterviewQuestions)
    else if (r.status === 'error') setFailed(r.error)
  }

  return (
    <>
      <header className="iv-qhead">
        <span className="iv-qnum">Question {n}</span>
        <span className="iv-dim iv-small">
          {wait?.skill ? <>asked by <code>{wait.skill}</code></> : 'skill that asked: not known'}
          {atUtc(wait?.asked_at) !== null && <> at {atUtc(wait?.asked_at)}</>}
        </span>
      </header>
      <h2 className="iv-qtext">{q.question ?? 'The question was not served.'}</h2>
      {q.context !== null && <p className="iv-dim iv-qctx">{q.context}</p>}
      {q.options.length > 0 && (
        <ul className="iv-opts" role="radiogroup" aria-label={`Answers to question ${n}`}>
          {q.options.map((o, i) => {
            const isRec = o.label === rec
            const html = previewOf(o)
            return (
              <li key={`${i}-${o.label}`} className={`iv-opt${o.label === picked ? ' is-picked' : ''}${isRec ? ' is-rec' : ''}`}>
                <label className="iv-opt-h">
                  <input type="radio" name={`iv-q${index}`} checked={o.label === picked} disabled={o.label === null}
                    onChange={() => { setPicked(o.label); setOwnWords(false) }} />
                  <span className="iv-opt-k">{optionKey(i)}</span>
                  <span className="iv-opt-l"><b>{plainLabel(o.label)}</b>{isRec && <Chip tone="live">Recommended</Chip>}</span>
                </label>
                {o.description !== null && <p className="iv-opt-d">{o.description}</p>}
                {html !== null
                  ? <Shot html={html} title={`Design: ${plainLabel(o.label)}`} onBig={() => setBig(i)} />
                  : <p className="iv-shot-none">No design came with this option.</p>}
              </li>
            )
          })}
        </ul>
      )}
      {!ownWords && (
        <p className="iv-none-fit iv-small">
          None of these fits? <button type="button" className="c-link" onClick={() => setOwnWords(true)}>Answer in your own words</button>
        </p>
      )}
      {big !== null && q.options[big] !== undefined && previewOf(q.options[big]!) !== null && (
        <div className="iv-big" role="dialog" aria-label={`Design: ${plainLabel(q.options[big]!.label)}`}>
          <div className="iv-big-h">
            <b>{optionKey(big)} · {plainLabel(q.options[big]!.label)}</b>
            <Button size="sm" kind="ghost" onClick={() => setBig(null)}>Close</Button>
          </div>
          <iframe title={`Design, full size: ${plainLabel(q.options[big]!.label)}`} sandbox="" srcDoc={previewOf(q.options[big]!)!} />
        </div>
      )}
      <div className="iv-dbar">
        <div className="iv-dbar-left">
          <span className="iv-expiry" title={atUtc(wait?.expires_at) === null ? undefined : `Unanswered by ${atUtc(wait?.expires_at)}, the step ends with the question left open`}>
            {Number.isNaN(expiresMs)
              ? <span className="iv-dim">time left not known</span>
              : left === null ? <b>time is up</b> : <><b>{left}</b> left{span !== null && ` of ${span}`}</>}
          </span>
          <span className="iv-dim iv-small">
            Never answered for anyone.{wait?.skill ? ' The first answer stands.' : ''}
          </span>
          {!mayAnswer && <span className="iv-err">{served.may_answer_because ?? 'You may not answer this question.'}</span>}
          {failed !== null && <span className="iv-err" role="alert">Not sent: {failed.message}</span>}
        </div>
        {ownWords && (
          <label className="iv-own">
            <span className="iv-small">In your own words</span>
            <textarea rows={2} maxLength={2000} value={text} onChange={(e) => setText(e.target.value)} />
          </label>
        )}
        <div className="iv-dbar-acts">
          {ownWords ? (
            <>
              {q.options.length > 0 && <Button kind="ghost" onClick={() => setOwnWords(false)}>Pick an option instead</Button>}
              <Button kind="primary" disabled={!mayAnswer || busy || text.trim() === ''}
                onClick={() => void send(null, text.trim())}>{busy ? 'Sending…' : 'Send my answer'}</Button>
            </>
          ) : (
            <>
              {rec !== null && (
                <Button disabled={!mayAnswer || busy} onClick={() => void send(rec, null)}>Use the recommended answer</Button>
              )}
              <Button kind="primary" disabled={!mayAnswer || busy || pickedAt < 0}
                onClick={() => void send(picked, null)}>{busy ? 'Sending…' : `Answer ${pickedAt < 0 ? '' : optionKey(pickedAt)}`.trim()}</Button>
            </>
          )}
        </div>
      </div>
    </>
  )
}

function Done({ served, index }: { served: InterviewQuestions; index: number }) {
  const q = served.questions[index]
  const a = q?.answer ?? null
  if (q === undefined || a === null) {
    return <p className="iv-next"><b>Answer saved.</b> The step starts again with it, in the same conversation.</p>
  }
  const at = q.options.findIndex((o) => o.label === a.choice)
  const option = at < 0 ? null : q.options[at]!
  const others = q.options.map((_, i) => optionKey(i)).filter((_, i) => i !== at)
  const html = option === null ? null : previewOf(option)
  return (
    <div className="iv-done">
      <header className="iv-qhead">
        <span className="iv-qnum">Question {index + 1} answered</span>
        <span className="iv-dim iv-small">
          by {a.by ?? 'you'} in the console{atUtc(a.at) !== null && ` at ${atUtc(a.at)}`}
          {a.choice !== null && a.choice === q.recommended && ' · the recommended answer'}
        </span>
      </header>
      <h2 className="iv-qtext">{q.question}</h2>
      <div className="iv-done-pick">
        {option !== null
          ? <><span className="iv-opt-k">{optionKey(at)}</span><b>{plainLabel(option.label)}</b>{option.label === q.recommended && <Chip tone="live">Recommended</Chip>}</>
          : <b>{a.text}</b>}
        {others.length > 0 && <span className="iv-dim iv-small">{joinKeys(others)} {others.length === 1 ? 'was' : 'were'} not chosen</span>}
      </div>
      {html !== null && (
        <div className="iv-done-shot iv-shot">
          <iframe title={`The chosen design: ${plainLabel(option!.label)}`} sandbox="" srcDoc={html} />
        </div>
      )}
      <div className="iv-next">
        <b>Next:</b> the step starts again with this answer, in the same conversation. It holds no machine until then.
        Question {index + 2} or the proposal appears here when it comes; you can leave this page.
      </div>
    </div>
  )
}

/** "B, C and D". */
function joinKeys(keys: string[]): string {
  return keys.length <= 1 ? keys.join('') : `${keys.slice(0, -1).join(', ')} and ${keys[keys.length - 1]}`
}

/** A design drawn at desktop width (820 px) and scaled into its card; Enlarge shows it full size. */
function Shot({ html, title, onBig }: { html: string; title: string; onBig: () => void }) {
  const box = useRef<HTMLDivElement>(null)
  const [scale, setScale] = useState(0.42)
  useLayoutEffect(() => {
    const el = box.current
    if (el === null) return
    const fit = () => { if (el.clientWidth > 0) setScale(el.clientWidth / 820) }
    fit()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(fit)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return (
    <div className="iv-shot">
      <div className="iv-shot-frame" ref={box}>
        <iframe title={title} sandbox="" srcDoc={html} tabIndex={-1} scrolling="no" style={{ transform: `scale(${scale})` }} />
      </div>
      <button type="button" className="iv-shot-big" onClick={onBig}>Enlarge</button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// The proposal
// ---------------------------------------------------------------------------

interface ProposalRead { run: IssueRun; questions: InterviewQuestions | null }

async function loadProposal(runId: string): Promise<Result<ProposalRead>> {
  const r = await loadRun(runId)
  if (r.status !== 'ok' && r.status !== 'stale') return r as Result<ProposalRead>
  const run = r.data.run
  const q = run.planner_task_id === '' ? null : await loadQuestions(run.planner_task_id)
  return { ...r, data: { run, questions: q !== null && (q.status === 'ok' || q.status === 'stale') ? (q.data as InterviewQuestions) : null } }
}

function ProposalScreen({ runId, go }: { runId: string; go: (to: string) => void }) {
  const [nonce, setNonce] = useState(0)
  return (
    <Screen key={nonce} title="Proposal" load={() => loadProposal(runId)}>
      {(data) => <ProposalView read={data} go={go} reread={() => setNonce((n) => n + 1)} />}
    </Screen>
  )
}

/** Who answered the planner's questions and where, counted: one row per person and place. */
export function answerTally(questions: InterviewQuestions | null): { by: string | null; via: string; n: number }[] {
  const out: { by: string | null; via: string; n: number }[] = []
  for (const q of questions?.questions ?? []) {
    const a = q.answer ?? null
    if (a === null) continue
    const via = viaWord(a.via)
    const row = out.find((r) => r.by === a.by && r.via === via)
    if (row !== undefined) row.n += 1
    else out.push({ by: a.by, via, n: 1 })
  }
  return out
}

type Acting = { kind: 'idle' } | { kind: 'busy'; what: 'approve' | 'edit' | 'reject' } | { kind: 'failed'; error: ApiError }

export function ProposalView({ read, go, reread }: { read: ProposalRead; go: (to: string) => void; reread: () => void }) {
  const [run, setRun] = useState(read.run)
  const [open, setOpen] = useState<'none' | 'edit' | 'reject'>('none')
  const [acting, setActing] = useState<Acting>({ kind: 'idle' })
  const [note, setNote] = useState<ReactNode>(null)
  const plan = run.plan
  const digest = run.plan_digest
  const canAct = run.state === 'PLANNED' && digest !== null && plan !== null
  const busy = acting.kind === 'busy'
  const tally = answerTally(read.questions)
  const asked = read.questions?.questions.length ?? null
  const total = tally.reduce((n, r) => n + r.n, 0)
  const auto = run.approved_by === AUTO_APPROVER

  async function act(what: 'approve' | 'edit' | 'reject', call: () => Promise<Result<unknown>>) {
    setActing({ kind: 'busy', what })
    const r = await call()
    if (r.status === 'ok' || r.status === 'stale') {
      const next = (r.data as { run?: IssueRun } | null)?.run
      setActing({ kind: 'idle' })
      setOpen('none')
      if (next !== undefined && next !== null && typeof next.id === 'string') setRun(next)
      if (what === 'approve') setNote(<><b>Approved.</b> The run is {runStateWord((next ?? run).state)} now.</>)
      if (what === 'reject') setNote(<><b>Rejected.</b> Nothing ran.</>)
      return
    }
    if (r.status !== 'error') return
    if (r.error.code === 'plan_changed' || r.error.code === 'invalid_run_transition' || r.error.kind === 'conflict') {
      // The plan or the run moved first: nothing was done; the page reads the run again.
      setActing({ kind: 'idle' })
      reread()
      return
    }
    setActing({ kind: 'failed', error: r.error })
  }

  return (
    <div className="iv-page iv-prop">
      <PageHead title={run.issue_read?.title || run.issue.ref} />
      <div className="iv-facts" aria-label="The run">
        <code>{run.issue.ref}</code>
        <span className="iv-dim">run</span> <code>{run.id}</code>
        <Chip tone={run.state === 'PLANNED' ? 'park' : undefined}>
          {run.state === 'PLANNED' ? 'Plan waiting for approval' : runStateWord(run.state)}
        </Chip>
        <a className="c-link" href={addressToPath(`work/runs?run=${encodeURIComponent(run.id)}`)}
          onClick={(e) => { e.preventDefault(); go(`work/runs?run=${encodeURIComponent(run.id)}`) }}>Open the run&rsquo;s page</a>
      </div>

      <div className="iv-prop-grid">
        <aside className="iv-rail iv-rail-done" aria-label="The interview">
          <div className="iv-rail-h">
            <b>Interview</b>
            <span className="iv-dim iv-small">
              {asked === null ? 'its questions could not be read' : `${asked} ${asked === 1 ? 'question' : 'questions'} asked`}
            </span>
          </div>
          {tally.length > 0 && (
            <ul className="iv-rail-tally" role="list">
              {tally.map((r) => (
                <li key={`${r.by}-${r.via}`}>
                  <span className="iv-av" aria-hidden="true">{initials(r.by)}</span>
                  {r.by ?? 'who: not recorded'} <span className="iv-via">{r.via}</span><b>{r.n}</b>
                </li>
              ))}
            </ul>
          )}
          {asked !== null && <p className="iv-dim iv-small">{total} of {asked} answered.</p>}
          {run.planner_task_id !== '' && (
            <p className="iv-small">
              <a className="c-link" href={addressToPath(interviewAddress(run.planner_task_id))}
                onClick={(e) => { e.preventDefault(); go(interviewAddress(run.planner_task_id)) }}>The questions and answers</a>
            </p>
          )}
          <p className="iv-small">Started by {run.created_by}</p>
        </aside>

        <main className="iv-prop-main">
          {open === 'edit' && plan !== null && digest !== null ? (
            <PlanEditor plan={plan} busy={busy} onCancel={() => setOpen('none')}
              onSave={(next: RunPlan) => void act('edit', () => editPlan(run.id, digest, next))} />
          ) : (
            <section className="iv-prop-sec">
              <h2>The plan</h2>
              {plan === null ? <p className="iv-dim">No plan yet: the planner has not written one.</p> : (
                <>
                  <p className="iv-prop-lead">{plan.summary}</p>
                  <ol className="iv-plan-steps" role="list">
                    {plan.steps.map((s, i) => (
                      <li key={s.step_id}>
                        <span className="iv-n">{i + 1}</span>
                        <div><b>{s.title}</b> <code className="iv-dim">{s.step_id}</code>{s.prompt && <p className="iv-dim">{s.prompt}</p>}</div>
                      </li>
                    ))}
                  </ol>
                  <dl className="iv-dl">
                    <dt>Requirements</dt>
                    <dd className="iv-row">
                      {(plan.requirements ?? []).length === 0 ? <span className="iv-dim">none stated</span> : (plan.requirements ?? []).map((q) => <Chip key={q}>{q}</Chip>)}
                    </dd>
                    <dt>Estimate</dt><dd>{plan.estimate ?? <span className="iv-dim">not given</span>}</dd>
                  </dl>
                </>
              )}
            </section>
          )}
          <section className="iv-prop-sec">
            <h2>Design doc and diagrams</h2>
            <p className="iv-dim">Not known here: the run serves its plan, not the design doc or the diagrams the planner wrote. They are in the planner&rsquo;s artifacts.</p>
          </section>
          {open === 'reject' && canAct && (
            <RejectForm busy={busy} onCancel={() => setOpen('none')}
              onReject={(reason) => void act('reject', () => rejectPlan(run.id, digest, reason))} />
          )}
          {acting.kind === 'failed' && <FailedPanel error={acting.error} onRetry={() => setActing({ kind: 'idle' })} />}
          <div className="iv-dbar">
            <div className="iv-dbar-left">
              <b>Revision {run.plan_revision}</b>
              {digest !== null && (
                <span className="iv-dim iv-small">
                  digest <code title={digest}>{shortDigest(digest)}</code> · if the plan changes first, the decision is refused and the new one shown
                </span>
              )}
              {note !== null && <span role="status">{note}</span>}
            </div>
            {auto ? (
              <div className="iv-dbar-acts"><Chip tone="live">Approved automatically</Chip></div>
            ) : canAct && open !== 'edit' ? (
              <div className="iv-dbar-acts">
                <span className="iv-dim iv-small">This run asks a person to approve</span>
                <Button kind="danger" disabled={busy} onClick={() => setOpen('reject')}>Reject</Button>
                <Button disabled={busy} onClick={() => setOpen('edit')}>Edit</Button>
                <Button kind="primary" disabled={busy} onClick={() => void act('approve', () => approvePlan(run.id, digest!))}>
                  {acting.kind === 'busy' && acting.what === 'approve' ? 'Approving…' : 'Approve'}
                </Button>
              </div>
            ) : null}
          </div>
        </main>
      </div>
    </div>
  )
}
