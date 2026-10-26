/**
 * THE STEP THAT WAITS FOR A PERSON (docs/design/human-steps.md §4.5, lane
 * HW-UI; part of #1436): the card an `await-input` step draws in the
 * workflow page's step inspector, and where the inbox's Answer lands.
 *
 * It reads `GET /v1/workflows/{wf}/steps/{step}/input` and draws, in order:
 *
 *   * the step's window -- "waiting for a member", the workspace, "expires in
 *     3 h 20 m" -- and what happens at expiry;
 *   * the prompt, and every artifact the step shows (its `input_from`),
 *     each read through the existing artifact viewer, which applies the
 *     tenant check and read-time redaction of `/artifacts/content`;
 *   * the form the step declared: `verdict` (Approve sends `MERGE`, Request
 *     changes sends `NOT_YET`, each with the findings typed), `answers` (the
 *     upstream agent's questions, as the server read them) or `text`;
 *   * when `max_files` is above 0, a file picker with THE WARNING above it
 *     and again beside Send. The picker refuses, before anything is sent, a
 *     file over the step's count, a name or type the server would refuse,
 *     and a whole answer over 16 MiB (`filesProblem`). The server repeats
 *     every check: these are for a fast refusal, not for safety.
 *
 * AN ANSWERED, EXPIRED OR CANCELLED STEP OFFERS NO FORM. It says who
 * answered, when and how (`via`), and lists the answer's files. A file is
 * OFFERED AS A DOWNLOAD, NEVER DRAWN: an image or a PDF a person uploaded is
 * never rendered in the console's origin (§4.3, "Reading back").
 *
 * The first answer is final (Q8). The card makes one `answer_id` when it
 * mounts, so a Send retried after a lost response is the same answer and the
 * server returns the original 200; a different person's earlier answer is
 * the server's 409, said in place, and the card is read again.
 */
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'

import { artifactRawUrl } from './api'
import { ArtifactViewer } from './ArtifactViewer'
import {
  FILE_WARNING,
  MAX_ANSWER_TEXT_CHARS,
  MAX_FINDINGS,
  MAX_FINDING_CHARS,
  MAX_TEXT_BYTES,
  TEXT_EXTENSIONS,
  BINARY_EXTENSIONS,
  WAITING_WORD,
  bytesWords,
  expiresWords,
  filesProblem,
  formBytes,
  isBinaryName,
  loadAwaitInput,
  newAnswerId,
  postAnswer,
  type AnswerRecord,
  type AwaitInputView,
  type AwaitPayload,
} from './awaitInput'
import { Button, ButtonLink } from './components'
import { errorHeading, type ApiError, type Result } from './fetch'
import { useNow } from './useNow'

/** The OTHER pick of an `answers` question: the person's own words. */
const OTHER = '\u0000other'

/** How an answer arrived, in words. */
const VIA_WORD: Readonly<Record<string, string>> = {
  console: 'in the console',
  mcp: 'through the plugin',
  github: 'by a GitHub comment',
}

/** A timestamp as the person's local time, with the ISO in the title. */
function At({ iso }: { iso: string | null }) {
  if (iso === null || iso === '') return <i className="ctl-em">at a time not recorded</i>
  const d = new Date(iso)
  return <time dateTime={iso} title={iso}>{Number.isFinite(d.getTime()) ? d.toLocaleString() : iso}</time>
}

/** The findings box's lines, trimmed, blanks dropped. */
function findingsOf(text: string): string[] {
  return text.split('\n').map((l) => l.trim()).filter((l) => l !== '')
}

/** Why the findings cannot be sent, or null. */
function findingsProblem(findings: readonly string[]): string | null {
  if (findings.length > MAX_FINDINGS) return `At most ${MAX_FINDINGS} findings, one per line; ${findings.length} are typed.`
  const long = findings.findIndex((f) => f.length > MAX_FINDING_CHARS)
  return long === -1 ? null : `Finding ${long + 1} is over ${MAX_FINDING_CHARS} characters.`
}

/** The refusal in the server's words, with its `detail.reason` when it gave one. */
function refusalWords(e: ApiError): string {
  const detail = typeof e.detail === 'object' && e.detail !== null ? (e.detail as Record<string, unknown>) : null
  const reason = detail !== null && typeof detail.reason === 'string' ? detail.reason : null
  return `${errorHeading(e)}${reason !== null ? ` (${reason})` : ''}: ${e.message}`
}

/**
 * THE ANSWER'S FILES, EACH A DOWNLOAD. Never an `<img>`, never an inline
 * frame: a person's upload is not drawn in the console (§4.3).
 */
function AnswerFiles({ taskId, files }: { taskId: string; files: AnswerRecord['files'] }) {
  if (files.length === 0) return <p className="sb-note">No files came with the answer.</p>
  return (
    <ul className="ai-files" aria-label="The answer's files">
      {files.map((f) => {
        const short = f.name.includes('/') ? f.name.slice(f.name.lastIndexOf('/') + 1) : f.name
        return (
          <li key={f.name} data-file={f.name}>
            <span className="mono">{f.name}</span>
            {f.bytes !== null && <span className="sb-note"> · {bytesWords(f.bytes)}</span>}{' '}
            <ButtonLink size="sm" className="copy" href={artifactRawUrl(taskId, f.name, 'attachment')} download={short}>
              download
            </ButtonLink>
            {isBinaryName(f.name) && <span className="sb-note"> · a PDF or an image is offered as a download only, never shown here</span>}
          </li>
        )
      })}
    </ul>
  )
}

/** What became of a step that takes no answer now: who, when and how, or why not. */
function Closed({ view, now }: { view: AwaitInputView; now: number }) {
  if (view.status === 'answered' && view.answer !== null) {
    const a = view.answer
    return (
      <div className="ai-closed" data-testid="ai-closed" data-status="answered">
        <p>
          Answered by <b>{a.answered_by ?? 'a person not recorded'}</b>, <At iso={a.answered_at} />,{' '}
          {a.via === null ? 'by a channel not recorded' : (VIA_WORD[a.via] ?? `via ${a.via}`)}. The first answer is final;
          a correction is a new round.
        </p>
        {view.artifact !== null && (
          <p className="sb-note">
            Written as <span className="mono">{view.artifact}</span>
            {a.bytes !== null ? `, ${bytesWords(a.bytes)} with its files` : ''}, which the next step stages.
          </p>
        )}
        <AnswerFiles taskId={view.task_id} files={a.files} />
      </div>
    )
  }
  if (view.status === 'expired') {
    return (
      <div className="ai-closed" data-testid="ai-closed" data-status="expired">
        <p>
          Nobody answered. It expired <At iso={view.expires_at} />{' '}
          {view.on_expire === 'default'
            ? 'and took the default its author declared and signed at submission, so the steps after it ran on that.'
            : view.on_expire === 'fail'
              ? 'and failed, as its author declared.'
              : 'and ended as its author declared.'}
        </p>
      </div>
    )
  }
  if (view.status === 'cancelled') {
    return (
      <p className="ai-closed" data-testid="ai-closed" data-status="cancelled">
        Cancelled before anyone answered: the workflow or the step was stopped. Nothing waits on it.
      </p>
    )
  }
  if (view.status === 'not_open') {
    return (
      <p className="ai-closed sb-note" data-testid="ai-closed" data-status="not_open">
        Not open yet: it opens when the steps it depends on have finished, and then any member of {view.tenant} may answer
        {view.expires_at === null ? '' : `; ${expiresWords(view.expires_at, now) ?? ''}`}.
      </p>
    )
  }
  return null
}

/** The file picker: the warning above it, what is chosen, and why it cannot go. */
function Picker({ files, setFiles, max, problem }: {
  files: File[]
  setFiles: (next: File[]) => void
  max: number
  problem: string | null
}) {
  return (
    <div className="ai-picker">
      <p className="c-hint ai-warning" data-testid="ai-warning-picker" role="note">{FILE_WARNING}</p>
      <label className="c-field">
        <span className="c-lbl">Files, at most {max}, 16 MiB with the answer</span>
        <input
          type="file"
          multiple
          data-testid="ai-picker"
          accept={[...TEXT_EXTENSIONS, ...BINARY_EXTENSIONS].join(',')}
          onChange={(e) => {
            const chosen = Array.from(e.target.files ?? [])
            // Added to what is already chosen, so a second pick does not drop the first.
            setFiles([...files, ...chosen])
            e.target.value = ''
          }}
        />
      </label>
      {files.length > 0 && (
        <ul className="ai-chosen" aria-label="Chosen files">
          {files.map((f, i) => (
            <li key={`${f.name}:${i}`}>
              <span className="mono">{f.name}</span> <span className="sb-note">{bytesWords(f.size)}</span>{' '}
              <button type="button" className="c-link is-sm" onClick={() => setFiles(files.filter((_, j) => j !== i))}>
                remove
              </button>
            </li>
          ))}
        </ul>
      )}
      {problem !== null && (
        <p className="c-hint" role="alert" data-testid="ai-files-problem">
          {problem}
        </p>
      )}
    </div>
  )
}

/** The form the step declared, the picker, and Send. */
function AnswerForm({ view, onSent }: { view: AwaitInputView; onSent: (said: string) => void }) {
  const answerId = useMemo(() => newAnswerId(), [])
  const [findings, setFindings] = useState('')
  const [text, setText] = useState('')
  const [picks, setPicks] = useState<Record<number, string>>({})
  const [others, setOthers] = useState<Record<number, string>>({})
  const [files, setFiles] = useState<File[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const max = Math.max(0, Math.min(10, view.max_files ?? 0))
  const questions = view.questions ?? []

  const payloadFor = (verdict: 'MERGE' | 'NOT_YET' | null): { payload: AwaitPayload } | { problem: string } => {
    if (view.form === 'verdict') {
      const list = findingsOf(findings)
      const p = findingsProblem(list)
      return p !== null ? { problem: p } : { payload: { verdict: verdict ?? 'MERGE', findings: list } }
    }
    if (view.form === 'text') {
      if (text.trim() === '') return { problem: 'Type the answer first.' }
      if (new TextEncoder().encode(text).length > MAX_TEXT_BYTES) return { problem: `The text is over ${bytesWords(MAX_TEXT_BYTES)}.` }
      return { payload: { text } }
    }
    const answers = []
    for (let i = 0; i < questions.length; i++) {
      const pick = picks[i]
      const own = (others[i] ?? '').trim()
      if (pick === undefined || (pick === OTHER && own === '')) return { problem: `Question ${i + 1} is not answered; every question must be.` }
      if (own.length > MAX_ANSWER_TEXT_CHARS) return { problem: `The answer to question ${i + 1} is over ${MAX_ANSWER_TEXT_CHARS} characters.` }
      answers.push(pick === OTHER ? { question_index: i, choice: null, text: own } : { question_index: i, choice: pick, text: null })
    }
    return { payload: { answers } }
  }

  // The form's size counts toward the 16 MiB; measured on what would be sent.
  const draft = payloadFor('MERGE')
  const fileProblem = filesProblem(files, max, 'payload' in draft ? formBytes(draft.payload) : 0)

  const send = async (verdict: 'MERGE' | 'NOT_YET' | null, word: string) => {
    const built = payloadFor(verdict)
    if ('problem' in built || fileProblem !== null) return
    setBusy(word)
    setRefused(null)
    const r: Result<unknown> = await postAnswer(view.workflow_id, view.step_id, { answer_id: answerId, payload: built.payload }, files)
    setBusy(null)
    if (r.status === 'ok' || r.status === 'empty') return onSent('Answered. The steps after it start on the answer.')
    if (r.status === 'error') {
      setRefused(r.error)
      // Someone else's answer, an expiry or a cancel won: read the step again.
      if (r.error.httpStatus === 409) onSent(refusalWords(r.error))
    }
  }

  const formProblem = 'problem' in draft ? draft.problem : null
  const blocked = fileProblem ?? (view.form === 'verdict' ? formProblem : null)
  const sendWarning = max > 0 ? <p className="c-hint ai-warning" data-testid="ai-warning-send" role="note">{FILE_WARNING}</p> : null
  let fields: ReactNode = null
  let acts: ReactNode = null
  if (view.form === 'verdict') {
    fields = (
      <label className="c-field">
        <span className="c-lbl">Findings, one per line (optional; at most {MAX_FINDINGS})</span>
        <textarea className="c-inp" rows={4} value={findings} onChange={(e) => setFindings(e.target.value)} />
      </label>
    )
    acts = (
      <>
        <Button busy={busy === 'changes' ? 'Sending…' : false} disabledReason={blocked ?? undefined} onClick={() => void send('NOT_YET', 'changes')}>
          Request changes
        </Button>
        <Button kind="primary" busy={busy === 'approve' ? 'Sending…' : false} disabledReason={blocked ?? undefined} onClick={() => void send('MERGE', 'approve')}>
          Approve
        </Button>
      </>
    )
  } else if (view.form === 'text') {
    fields = (
      <label className="c-field">
        <span className="c-lbl">The answer</span>
        <textarea className="c-inp" rows={6} value={text} onChange={(e) => setText(e.target.value)} />
      </label>
    )
    acts = (
      <Button kind="primary" busy={busy === 'send' ? 'Sending…' : false} disabledReason={fileProblem ?? formProblem ?? undefined} onClick={() => void send(null, 'send')}>
        Send
      </Button>
    )
  } else if (view.form === 'answers') {
    fields =
      questions.length === 0 ? (
        <p className="c-hint" role="alert">
          The upstream agent’s questions could not be read just now, so there is nothing to answer here yet. Read the step again in a moment.
        </p>
      ) : (
        <ol className="ai-questions">
          {questions.map((q, i) => (
            <li key={i}>
              <fieldset className="c-field">
                <legend className="c-lbl">{q.question ?? <i className="ctl-em">question text not recorded</i>}</legend>
                {q.context !== null && <p className="sb-note">{q.context}</p>}
                {q.options.map((o, j) =>
                  o.label === null ? null : (
                    <label key={j} className="ai-option">
                      <input type="radio" name={`ai-q${i}`} checked={picks[i] === o.label} onChange={() => setPicks({ ...picks, [i]: o.label as string })} />{' '}
                      {o.label}
                      {q.recommended === o.label && <span className="sb-note"> · recommended</span>}
                      {o.description !== null && <span className="sb-note"> — {o.description}</span>}
                    </label>
                  ),
                )}
                <label className="ai-option">
                  <input type="radio" name={`ai-q${i}`} checked={picks[i] === OTHER} onChange={() => setPicks({ ...picks, [i]: OTHER })} /> Other…
                </label>
                {picks[i] === OTHER && (
                  <textarea className="c-inp" rows={2} maxLength={MAX_ANSWER_TEXT_CHARS} aria-label={`Your answer to question ${i + 1}`} value={others[i] ?? ''} onChange={(e) => setOthers({ ...others, [i]: e.target.value })} />
                )}
              </fieldset>
            </li>
          ))}
        </ol>
      )
    acts = (
      <Button kind="primary" busy={busy === 'send' ? 'Sending…' : false} disabledReason={fileProblem ?? formProblem ?? undefined} onClick={() => void send(null, 'send')}>
        Send
      </Button>
    )
  }
  return (
    <div className="ai-form" data-form={view.form ?? 'none'}>
      {fields}
      {max > 0 && <Picker files={files} setFiles={setFiles} max={max} problem={fileProblem} />}
      {refused !== null && (
        <p className="c-hint" role="alert" data-code={refused.code ?? ''}>
          {refusalWords(refused)}
        </p>
      )}
      <div className="c-acts ai-send">
        {sendWarning}
        {acts}
      </div>
    </div>
  )
}

/** One artifact the step shows, read on demand through the artifact viewer. */
function Shown({ item }: { item: AwaitInputView['shows'][number] }) {
  const [open, setOpen] = useState(false)
  return (
    <li>
      <span className="mono">{item.name}</span>
      <span className="sb-note"> from {item.step_id ?? item.task_id}</span>{' '}
      {!open && (
        <Button size="sm" className="copy" onClick={() => setOpen(true)}>
          read
        </Button>
      )}
      {open && <ArtifactViewer taskId={item.task_id} artifact={{ name: item.name, bytes: 0, uri: '' }} onClose={() => setOpen(false)} backLabel="Close" />}
    </li>
  )
}

/** The card for one step, read from the step's own route. */
export function AwaitInputCard({ workflowId, stepId, onAnswered }: {
  workflowId: string
  stepId: string
  /** The workflow is read again once an answer lands, so the next step is drawn starting. */
  onAnswered?: () => void
}) {
  const now = useNow()
  const [r, setR] = useState<Result<AwaitInputView>>({ status: 'loading', since: Date.now() })
  const [said, setSaid] = useState<string | null>(null)
  const [round, setRound] = useState(0)
  const reread = useCallback(() => setRound((n) => n + 1), [])
  useEffect(() => {
    let live = true
    void loadAwaitInput(workflowId, stepId).then((next) => {
      if (live) setR(next)
    })
    return () => {
      live = false
    }
  }, [workflowId, stepId, round])

  let body: ReactNode
  if (r.status === 'loading') body = <p className="sb-note">Reading the step…</p>
  else if (r.status === 'empty') body = <p className="sb-note">The step answered nothing.</p>
  else if (r.status === 'error') body = <p className="c-hint" role="alert">Not read: {errorHeading(r.error)}. {r.error.message}</p>
  else {
    const v = r.data
    const expires = v.status === 'waiting' ? expiresWords(v.expires_at, now) : null
    body = (
      <>
        <div className="wf-card-h">
          <b>Waiting for a person</b>
          {v.status === 'waiting' && (
            <span className="ai-pill" data-testid="ai-pill" title={`Any member of workspace ${v.tenant} may answer.`}>
              {WAITING_WORD}
            </span>
          )}
          <span className="wf-card-r">
            workspace {v.tenant}
            {expires !== null && ` · ${expires}`}
            {v.status === 'waiting' && v.on_expire !== null && ` · at expiry it ${v.on_expire === 'default' ? 'takes its declared default' : 'fails'}`}
          </span>
        </div>
        {v.prompt !== null && v.prompt !== '' && <p className="ai-prompt">{v.prompt}</p>}
        {v.shows.length > 0 && (
          <ul className="ai-shows" aria-label="What the step shows">
            {v.shows.map((s) => (
              <Shown key={`${s.task_id}:${s.name}`} item={s} />
            ))}
          </ul>
        )}
        {v.status === 'waiting' ? (
          !v.spec_verified ? (
            <p className="c-hint" role="alert">
              This step’s signed declaration did not verify, so it cannot be answered: the platform will not read a form it did not sign.
            </p>
          ) : v.may_answer ? (
            <AnswerForm
              view={v}
              onSent={(words) => {
                setSaid(words)
                reread()
                onAnswered?.()
              }}
            />
          ) : (
            <p className="c-hint" data-testid="ai-not-member">
              Only a member of workspace {v.tenant} may answer, checked when the answer arrives. Switch to {v.tenant} to answer it.
            </p>
          )
        ) : (
          <Closed view={v} now={now} />
        )}
      </>
    )
  }
  return (
    <section className="wf-verdict ai-card" aria-label={`Step ${stepId} waits for a person`} data-testid="ai-card">
      {said !== null && (
        <p className="sb-note" role="status">
          {said}
        </p>
      )}
      {body}
    </section>
  )
}
