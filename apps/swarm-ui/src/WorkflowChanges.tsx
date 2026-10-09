import { useCallback, useEffect, useState, type ReactNode } from 'react'

import { loadArtifactContent, loadAttempts, artifactRawUrl } from './api'
import { patchWindow } from './ArtifactViewer'
import { publishRefusals } from './AgentDetail'
import { Button } from './components'
import { DiffView } from './diff/DiffView'
import { errorHeading, num, type ApiError } from './fetch'
import { agentPath } from './OverviewRegions'
import { addressToPath } from './paths'
import { Mark } from './primitives'
import {
  TERMINAL_STATES,
  artifactKind,
  bytesLabel,
  type ArtifactContent,
  type ArtifactRef,
  type AttemptRow,
  type GitSummary,
  type IssueRun,
  type ResultSummary,
  type Task,
  type Workflow,
} from './types'
import { stateWord } from './words'
import './styles/changes.css'

/**
 * THE CHANGES TAB (docs/design/diff-viewer.md §2 variant 2, owner decision
 * 2026-10-08): wherever work happened -- an agent, a workflow, an issue run --
 * one tab named Changes, holding the shipped viewer (`diff/DiffView`) at the
 * pane's full height.
 *
 * ON AN AGENT it is the agent's own patch (`result_summary.git.patch`), read
 * by its manifest name through the artifact content route -- the same
 * tenant-scoped, redacted-by-window read the Artifacts tab makes (§4). The
 * open file is in the address (`/changes/<path>`), so a link names the file
 * and Back moves the viewer.
 *
 * ON A WORKFLOW AND A RUN it will hold variant 5's files x steps matrix (lane
 * DIFF2b). Until that lands the tab lists the steps that wrote a patch, each
 * linked to its agent's Changes tab: a real way to every step's diff, never a
 * page that only says "later".
 *
 * ABSENCE IS NEVER DRAWN AS ZERO (the mock-up's state row): a step that changed
 * nothing is a measured zero; a running one has written nothing yet; a patch
 * over the cap was discarded, not truncated; a read that failed is not read;
 * and a credential refusal WITHHOLDS the patch -- no line of it is drawn here,
 * redacted or not, exactly as the Code card withholds it
 * (AgentDetail.tsx `GitOutcome`). This is the second place that rule must
 * hold (§2 variant 2, risks).
 */

/** `result_summary.git`, or null when the summary carries none (it is an untyped dict). */
function gitOf(task: Task): GitSummary | null {
  const git = (task.result_summary as ResultSummary | null)?.git
  return typeof git === 'object' && git !== null && !Array.isArray(git) ? git : null
}

function artifactsOf(task: Task): ArtifactRef[] {
  const a = (task.result_summary as ResultSummary | null)?.artifacts
  return Array.isArray(a) ? a : []
}

/** What the tab reads: the run's own patch, or a patch a workflow step handed on (#278), or none. */
type Source =
  | { kind: 'patch'; name: string; handed: boolean }
  | { kind: 'omitted'; bytes: number | null }
  | { kind: 'zero' }
  | { kind: 'unsummarised' }

function sourceOf(task: Task, git: GitSummary | null): Source {
  if (git === null) return { kind: 'unsummarised' }
  if (typeof git.patch === 'string' && git.patch !== '') return { kind: 'patch', name: git.patch, handed: false }
  if (git.patch_omitted === true) return { kind: 'omitted', bytes: typeof git.patch_bytes === 'number' ? git.patch_bytes : null }
  // CODE HANDED ON RATHER THAN PUSHED (#278): a step that changed nothing in
  // its clone and wrote a diff into its artifacts for a dependant to stage.
  // "No changes" would be false for it; its diff is what went on.
  if (task.workflow_id !== null && task.step_id !== null) {
    const handed = artifactsOf(task).find((a) => artifactKind(a.name) === 'diff')
    if (handed !== undefined) return { kind: 'patch', name: handed.name, handed: true }
  }
  return { kind: 'zero' }
}

/**
 * THE TAB'S COUNT: the patch's file count from `git.files` (DIFF4), a measured
 * 0 for an agent that changed nothing, and otherwise a DASH with its reason --
 * never a 0 for a count nobody measured.
 */
export function changesCount(task: Task | null): { count: number | string | null; say: string | null } {
  if (task === null) return { count: null, say: 'The task has not been read yet.' }
  const git = gitOf(task)
  if (git === null) {
    return {
      count: null,
      say: TERMINAL_STATES.has(task.state)
        ? 'This run wrote no git summary, so what it changed is not known.'
        : 'Changes are recorded when an attempt ends: this agent has written nothing yet.',
    }
  }
  if (Array.isArray(git.files)) {
    return git.files_truncated
      ? { count: `${git.files.length}+`, say: `The worker kept the first ${git.files.length} files and says there were more.` }
      : { count: git.files.length, say: null }
  }
  const src = sourceOf(task, git)
  if (src.kind === 'zero') return { count: 0, say: null }
  if (src.kind === 'omitted') return { count: null, say: 'The patch was discarded for exceeding the size cap, so its files are not counted.' }
  return { count: null, say: 'This worker does not record per-file counts (git.files); the tab reads the patch to list them.' }
}

/**
 * THE AGENT'S CHANGES TAB, with the attempt read the withheld rule needs. The
 * patch is not read until the attempts are: a credential refusal recorded on
 * an earlier attempt must withhold it, and a patch drawn and then taken away
 * has already been read. A failed attempts read falls back to the task's own
 * last error, as the Code card does with no attempt documents.
 */
export function AgentChangesPane({ task, file, onFile }: { task: Task; file: string | null; onFile: (path: string) => void }) {
  const [attempts, setAttempts] = useState<{ task: string; rows: AttemptRow[] | null } | null>(null)
  useEffect(() => {
    let live = true
    void loadAttempts(task.id).then((r) => {
      if (!live) return
      const rows = r.status === 'ok' || r.status === 'stale' ? r.data.attempts : r.status === 'empty' ? [] : null
      setAttempts({ task: task.id, rows })
    })
    return () => {
      live = false
    }
  }, [task.id])
  if (attempts === null || attempts.task !== task.id) {
    return (
      <div className="chg-tab">
        <p className="art-loading">
          <Mark kind="pending" say="Reading this agent's attempts, for any publish refusal that withholds its patch. The read is in flight." />
          <span className="ctl-pending art-loading-bar" />
        </p>
      </div>
    )
  }
  return <AgentChanges task={task} attempts={attempts.rows} file={file} onFile={onFile} />
}

/** The tab's content once the attempts are known (null: their read failed). */
export function AgentChanges({
  task,
  attempts,
  file,
  onFile,
}: {
  task: Task
  attempts: readonly AttemptRow[] | null
  file: string | null
  onFile: (path: string) => void
}) {
  const git = gitOf(task)
  const refused = publishRefusals(task, attempts).filter((r) => r.kind === 'credential')
  if (refused.length > 0) {
    const named = refused.map((r) => r.file).filter((f): f is string => f !== null)
    return (
      <div className="chg-tab">
        <State
          mark="unread"
          heading="withheld · a file failed the credential scan"
          say="The worker refused to publish this work because a file added a credential. No line of this patch is drawn here, redacted or not; the Code card on Details names the refusal."
        >
          {named.length > 0 ? (
            <>
              Refused: <span className="mono">{[...new Set(named)].join(', ')}</span>. Its content is never shown.
            </>
          ) : null}
        </State>
      </div>
    )
  }
  const src = sourceOf(task, git)
  if (src.kind === 'unsummarised') {
    const done = TERMINAL_STATES.has(task.state)
    return (
      <div className="chg-tab">
        <State
          mark="absent"
          heading={done ? `no git summary · ${stateWord(task.state)}` : `nothing written yet · ${stateWord(task.state)}`}
          say={
            done
              ? 'This run ended without a git summary, so what it changed is not known. That is not the same as no changes.'
              : 'The patch is written when an attempt ends. Until then there is nothing to read, which is not the same as no changes.'
          }
        />
      </div>
    )
  }
  if (src.kind === 'omitted') {
    return (
      <div className="chg-tab">
        <State
          mark="partial"
          heading={src.bytes === null ? 'patch discarded over the size cap' : `patch discarded at ${num(src.bytes)} bytes`}
          say="The patch was discarded for exceeding the size cap. It was NOT truncated: a truncated patch applies cleanly and silently drops the rest of the change. The Code card on Details still counts the commits."
        />
      </div>
    )
  }
  if (src.kind === 'zero') {
    return (
      <div className="chg-tab">
        <State mark="zero" heading="no changes · nothing differed from the clone" say="Nothing differed from the clone, so there is no patch. This is a real zero rather than a patch that failed to upload." />
      </div>
    )
  }
  const pr = git?.pull_request
  return (
    <div className="chg-tab">
      <PatchChanges
        taskId={task.id}
        name={src.name}
        file={file}
        onFile={onFile}
        pullRequest={pr ? { url: pr.url, label: `#${pr.number}` } : undefined}
        lead={
          <>
            {typeof git?.base === 'string' && git.base !== '' && (
              <span>
                base <code className="mono">{git.base.slice(0, 10)}</code> → work tree
              </span>
            )}
            {src.handed && (
              <span>
                <Mark kind="partial" say="This step changed nothing in its clone and handed this diff on to a dependant, which staged it. It is the step's work, not a commit." />{' '}
                handed on, not committed
              </span>
            )}
          </>
        }
      />
    </div>
  )
}

/**
 * ONE PATCH, READ BY ITS MANIFEST NAME, IN THE VIEWER AT FULL HEIGHT. The
 * server's window is drawn as `ArtifactViewer` draws it: a whole patch as it
 * is; a window as its whole files only, the cut file named, `partial` said,
 * Copy copying the window and Download fetching the whole object through the
 * raw route. A masked value stays the server's mask, and the count is shown.
 */
export function PatchChanges({
  taskId,
  name,
  file,
  onFile,
  pullRequest,
  lead,
}: {
  taskId: string
  name: string
  file: string | null
  onFile: (path: string) => void
  pullRequest?: { url: string; label?: string } | undefined
  /** The summary line's facts before the patch's own: the base, a handed-on mark. */
  lead?: ReactNode
}) {
  const [offset, setOffset] = useState<number | null>(null)
  const [state, setState] = useState<{ kind: 'loading' } | { kind: 'error'; error: ApiError } | { kind: 'ok'; data: ArtifactContent }>({
    kind: 'loading',
  })
  const load = useCallback(async () => {
    setState({ kind: 'loading' })
    const res = await (offset === null ? loadArtifactContent(taskId, name) : loadArtifactContent(taskId, name, { offset }))
    if (res.status === 'ok' || res.status === 'stale') setState({ kind: 'ok', data: res.data })
    else if (res.status === 'error') setState({ kind: 'error', error: res.error })
    else
      setState({
        kind: 'error',
        error: { kind: 'unreachable', httpStatus: null, code: null, message: 'The read returned a shape this tab does not handle.' },
      })
  }, [taskId, name, offset])
  useEffect(() => {
    void load()
  }, [load])

  if (state.kind === 'loading') {
    return (
      <p className="art-loading">
        <Mark kind="pending" say={`Reading ${name}. The read is in flight.`} />
        <span className="ctl-pending art-loading-bar" />
      </p>
    )
  }
  if (state.kind === 'error') {
    const status = state.error.httpStatus === null ? '' : ` · ${state.error.httpStatus}`
    return (
      <State
        mark="unread"
        heading={`patch not read${status}`}
        say={`${errorHeading(state.error)}. Nothing may be concluded about this patch: it is not empty and it is not missing, the read did not complete.`}
      >
        {state.error.message}{' '}
        <Button size="sm" className="retry" onClick={() => void load()}>
          Retry
        </Button>
      </State>
    )
  }
  const data = state.data
  if (data.status === 'absent') {
    return (
      <State
        mark="absent"
        heading="patch not in the bucket"
        say="This task's manifest names the patch and the object is not there: a missing object, not a run that changed nothing."
      >
        {data.detail}
      </State>
    )
  }
  if (data.status === 'unreadable' || data.status === 'binary' || data.content === null) {
    return (
      <State
        mark="unread"
        heading={data.status === 'binary' ? 'patch is not text' : 'artifact store could not be read'}
        say="The patch could not be drawn as text. Nothing may be concluded about what it changed."
      >
        {data.detail}
      </State>
    )
  }
  if (data.content === '') {
    return <State mark="zero" heading="empty patch" say="The patch was written and holds nothing: a real zero, not a failed read." />
  }

  const whole = data.offset === 0 && data.next_offset === null && !data.truncated
  const w = whole ? null : patchWindow(data.content, data)
  const rawUrl = artifactRawUrl(taskId, name, 'attachment')
  const summary = (
    <p className="chg-sum">
      {lead}
      <span className="mono">
        {name} · {bytesLabel(data.total_bytes)}
      </span>
      {/* THE MASKED COUNT, as the Artifacts tab gives it (ArtifactViewer.tsx):
          masking happens as the patch is served, and the count says how
          many values this window holds as the server's mask. No `?` of its
          own: the console's help glyphs are capped (B7.4), and the Artifacts
          tab's masked key carries the explanation. */}
      <span className="chg-masked">
        masked{' '}
        <span className={`art-masked${data.redacted && data.redaction_count > 0 ? ' is-warn' : ''}`}>
          {data.redacted ? data.redaction_count : `0 of ${data.redaction.rules} families`}
        </span>
      </span>
      {w !== null && (
        <span className="chg-window">
          <Mark
            kind="partial"
            say={`This is a window of the patch, from byte ${data.offset}${data.next_offset !== null ? `, with more from byte ${data.next_offset}` : ''}. Only the files wholly inside it are drawn.`}
          />{' '}
          partial
          {data.next_offset !== null && (
            <Button size="sm" className="copy" onClick={() => setOffset(data.next_offset)}>
              next window
            </Button>
          )}
          {data.offset > 0 && (
            <Button size="sm" className="copy" onClick={() => setOffset(null)}>
              first window
            </Button>
          )}
        </span>
      )}
    </p>
  )
  const view = {
    name,
    size: bytesLabel(data.total_bytes),
    attempt: data.attempt_id,
    fullHeight: true,
    initialFile: file,
    onFileChange: onFile,
    ...(pullRequest === undefined ? {} : { pullRequest }),
  }
  return (
    <div className="chg-patch">
      {summary}
      <div className="chg-diff">
        {w === null ? (
          <DiffView patch={data.content} {...view} />
        ) : (
          <DiffView
            patch={w.patch}
            {...view}
            copy={{ label: 'Copy window', text: data.content }}
            download={{ href: rawUrl }}
            meta={
              w.before || w.after !== null ? (
                <div className="diff-cut" role="note" aria-label="What this window cut">
                  {w.before ? <p>This window starts inside a file the window before it cut; those lines are not drawn here.</p> : null}
                  {w.after !== null ? (
                    <p>
                      <span className="mono">{w.after === '' ? 'the last file' : w.after}</span> cut by the window, read the next one.
                    </p>
                  ) : null}
                </div>
              ) : undefined
            }
            empty={
              <p className="diff-empty" role="status">
                No file lies wholly inside this window, so none is drawn. Read the next window, or download the whole patch.
              </p>
            }
          />
        )}
      </div>
    </div>
  )
}

/** One of the tab's states: a mark, a heading, at most one sentence. */
function State({
  mark,
  heading,
  say,
  children,
}: {
  mark: 'zero' | 'absent' | 'unread' | 'partial'
  heading: string
  say: string
  children?: ReactNode
}) {
  return (
    <div className={`ctl-empty chg-state${mark === 'zero' ? '' : ` is-${mark === 'unread' ? 'failed' : mark}`}`} role={mark === 'zero' ? undefined : 'status'}>
      <h3>
        <Mark kind={mark} say={say} /> {heading}
      </h3>
      {children !== undefined && children !== null ? <p>{children}</p> : null}
    </div>
  )
}

/** A step's agent, on its Changes tab: under the list its state belongs to, as `OpenAgent` routes. */
function agentChangesHref(taskId: string, task: Task | undefined): string {
  const base = task === undefined ? addressToPath(`work/task/${encodeURIComponent(taskId)}`) : agentPath({ id: taskId, state: task.state })
  return `${base}/changes`
}

/**
 * THE STEPS' PATCHES, ONE LINK EACH, until variant 5's matrix fills this tab
 * (lane DIFF2b). Each step is listed with what its own summary says -- its
 * file count, a measured zero, nothing written yet -- by `changesCount`, and
 * links to its agent's Changes tab, where its patch is read.
 */
function StepChanges({ steps }: { steps: { stepId: string; taskId: string | null; task: Task | undefined }[] }) {
  if (steps.length === 0) return <State mark="zero" heading="no steps" say="This has no steps, so nothing changed any file." />
  return (
    <ul className="chg-steps">
      {steps.map((s) => {
        const c = s.task === undefined ? { count: null, say: 'This step’s task was not in this read.' } : changesCount(s.task)
        return (
          <li key={s.stepId} className="chg-step">
            <span className="mono chg-step-id">{s.stepId}</span>
            <span className="chg-step-n" title={c.say ?? undefined}>
              {c.count === null ? '—' : `${c.count} ${c.count === 1 ? 'file' : 'files'}`}
            </span>
            {s.taskId === null ? (
              <span className="ctl-sub">not started</span>
            ) : (
              <a className="ctl-link" href={agentChangesHref(s.taskId, s.task)}>
                Changes ›
              </a>
            )}
          </li>
        )
      })}
    </ul>
  )
}

/** The note above the step list: what this tab will hold, and where each step's diff is now. */
function MatrixNote({ of }: { of: 'workflow' | 'run' }) {
  return (
    <p className="chg-note">
      Which step changed which file is drawn here once each step&apos;s patch is read side by side. Until then, each
      step&apos;s own changes are one link away, on its agent&apos;s Changes tab{of === 'run' ? ', and the run’s workflow lists the same steps' : ''}.
    </p>
  )
}

/** A workflow's Changes tab (`/workflows/<id>/changes`). */
export function WorkflowChangesTab({ workflow, taskById }: { workflow: Workflow; taskById: ReadonlyMap<string, Task> | null }) {
  const steps = workflow.steps.map((s) => {
    const taskId = s.task_id ?? null
    return { stepId: s.step_id, taskId, task: taskId === null ? undefined : taskById?.get(taskId) }
  })
  return (
    <section className="chg-tab chg-wf" aria-label="Changes in this workflow">
      <MatrixNote of="workflow" />
      <StepChanges steps={steps} />
    </section>
  )
}

/** An issue run's Changes tab (`/runs/<id>/changes`). */
export function RunChangesTab({ run }: { run: IssueRun }) {
  const wf = run.workflow_id ?? null
  const pr = run.pr_task_id ?? null
  return (
    <section className="chg-tab chg-run" aria-label="Changes in this run">
      <MatrixNote of="run" />
      {wf === null ? (
        <State
          mark="zero"
          heading="no workflow yet"
          say="The run's workflow is created when its plan is approved. Until then no step has run, so nothing has changed."
        />
      ) : (
        <ul className="chg-steps">
          <li className="chg-step">
            <span className="chg-step-id">The run’s workflow, step by step</span>
            <a className="ctl-link" href={`/workflows/${encodeURIComponent(wf)}/changes`}>
              Changes ›
            </a>
          </li>
          {pr !== null && (
            <li className="chg-step">
              <span className="chg-step-id">The step that opened the pull request</span>
              <a className="ctl-link" href={agentChangesHref(pr, undefined)}>
                Changes ›
              </a>
            </li>
          )}
        </ul>
      )}
    </section>
  )
}
