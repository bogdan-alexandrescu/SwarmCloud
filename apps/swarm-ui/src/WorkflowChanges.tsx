import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'

import { loadArtifactContent, loadAttempts, artifactRawUrl } from './api'
import { patchWindow } from './ArtifactViewer'
import { publishRefusals } from './AgentDetail'
import { Button } from './components'
import { DiffView, httpUrl, type DiffPin } from './diff/DiffView'
import { parseUnifiedDiff, type DiffFile } from './diff/parse'
import { errorHeading, num, type ApiError, type Result } from './fetch'
import { agentPath } from './OverviewRegions'
import { addressToPath } from './paths'
import { Mark } from './primitives'
import { stepRows, useRunWorkflows } from './RunSteps'
import { workflowPullRequest } from './stepviews'
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
import { SEVERITY_LABEL, placeFinding, reviewFindings, type Placement, type ReviewFinding, type ShownPatch } from './wfreview'
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
 * ON A WORKFLOW AND A RUN it holds variant 5's files x steps matrix (lane
 * DIFF2b, `ChangesMatrix` below): which step changed which file, and the open
 * file's hunks stacked by step. The step filter and the open file are in the
 * address (`?step=<step>&file=<path>`), so the step inspector's link opens the
 * matrix filtered to its step.
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


// ---------------------------------------------------------------------------
// VARIANT 5: WHICH STEP CHANGED WHICH FILE (lane DIFF2b)
// ---------------------------------------------------------------------------
//
// A workflow has one patch per step, so "its changes" is a matrix: files down
// the side, steps across the top, each cell that step's +/- on that file.
// Choosing a file draws each step's hunks for it in ONE viewer, stacked and
// tagged by step (`DiffView` `stepOf`). A step filter narrows the rows to the
// files that step changed and the viewer to its hunks.
//
// WHAT IS READ, AND HOW MUCH. A cell needs no read when the step's summary
// lists its files (`git.files`, DIFF4); a step without that list has its patch
// read to find them. Hunks are read ON DEMAND: only the steps that changed the
// open file. Every read is a step's own patch through the API's tenant-scoped,
// redacting content route (`loadArtifactContent`), never a bucket or a signed
// URL (design §4), and at most `MAX_READS_IN_FLIGHT` are in flight at once, so
// a twenty-step workflow is not twenty 512 KiB windows at the same moment.
//
// ABSENCE IS NEVER A CELL'S ZERO. `·` is a measured "this step did not touch
// this file": the step's file list is known and complete. A step that changed
// nothing says so in its header with the hollow ring. Everything else that is
// not known -- not started, nothing written yet, still reading, a read that
// failed, a patch discarded over the cap, a file past the first window -- is a
// hatched cell with its reason.
//
// WITHHELD STAYS WITHHELD. A step whose publish was refused for a credential
// (in ANY attempt: its attempts are read first, as on the agent's tab) has its
// patch never read, and its column draws no file and no count.
//
// LINES ARE NOT ATTRIBUTED. Steps diff against different bases, so a hunk's
// line numbers count that step's own file; which step wrote each line of the
// pull request is recorded nowhere (the integrator keeps no
// `integrated_from`), and the viewer says "not recorded" rather than guess.

/** At most this many step reads (attempts or a patch window) at once. */
export const MAX_READS_IN_FLIGHT = 4

/** Where a workflow's or a run's Changes tab stands, from its address: the step filter and the open file. */
export interface ChangesAt {
  step: string | null
  file: string | null
}

/** `step` and `file` from a route's query; '' is absent. */
export function changesAtOf(view: string | null | undefined): ChangesAt {
  const p = new URLSearchParams(view ?? '')
  const step = p.get('step')
  const file = p.get('file')
  return { step: step === null || step === '' ? null : step, file: file === null || file === '' ? null : file }
}

/** `query` with `step` and `file` written from `at` (each dropped when null). */
export function withChangesAt(query: string, at: ChangesAt): string {
  const p = new URLSearchParams(query)
  p.delete('step')
  p.delete('file')
  if (at.step !== null) p.set('step', at.step)
  if (at.file !== null) p.set('file', at.file)
  return p.toString()
}

/** One column of the matrix: a step, with its task when the read carried it. */
export interface StepColumn {
  /** The step filter's value: the step id, or `<round>:<step id>` for an issue run's CI fix round. */
  key: string
  /** What the column's head says. */
  label: string
  taskId: string | null
  /** Null when the step has no task yet, or the read did not carry it. */
  task: Task | null
  /** The step that opened the workflow's pull request: its head says `integrate`. */
  integrator?: boolean
}

/** One step's change to one file. Null counts: a binary file. */
export interface FileCount {
  add: number | null
  del: number | null
  binary: boolean
}

/** One file's section of a step's patch, as served, with its parse. */
export interface PatchSection {
  text: string
  file: DiffFile
}

/** A step's patch, as far as its read got. */
export type PatchRead =
  | { kind: 'failed'; heading: string; detail: string }
  | {
      kind: 'ok'
      sections: ReadonlyMap<string, PatchSection>
      /** The whole patch was in the window. */
      whole: boolean
      /** Sections the parser refused; their files are not listed. */
      unparsed: number
      masked: number
      /**
       * The sha256 of the patch's bytes, for the findings' digest check
       * (`wfreview.placeFinding`); null when the text served is not those
       * bytes -- a window, a masked line, a byte that is not UTF-8.
       */
      digest: string | null
    }

/** What is known of one step's changes. */
export type ColumnFacts =
  | { kind: 'not-started' }
  | { kind: 'not-in-read' }
  | { kind: 'unsummarised'; done: boolean }
  | { kind: 'zero' }
  | { kind: 'checking' }
  | { kind: 'withheld'; files: string[] }
  | { kind: 'omitted'; bytes: number | null; files: ReadonlyMap<string, FileCount> | null; complete: boolean }
  | {
      kind: 'patch'
      name: string
      handed: boolean
      /** From `git.files`, else from the read patch; null until one of them is known. */
      files: ReadonlyMap<string, FileCount> | null
      /** The list holds every file the step changed: a file not in it is a measured `·`. */
      complete: boolean
      /** The list came from the summary, so the patch is read only for hunks. */
      listed: boolean
      read: PatchRead | undefined
    }

/** `git.files` as a map, and whether it is the whole list. Null when the summary carries none. */
function listedFiles(git: GitSummary): { files: ReadonlyMap<string, FileCount>; complete: boolean } | null {
  if (!Array.isArray(git.files)) return null
  const files = new Map<string, FileCount>()
  for (const f of git.files) {
    if (typeof f?.path !== 'string') continue
    files.set(f.path, { add: f.insertions ?? null, del: f.deletions ?? null, binary: f.binary === true })
  }
  return { files, complete: git.files_truncated !== true }
}

/**
 * A patch cut into its files, each parsed alone and keyed by the path the
 * viewer shows (the new path; the old one for a deletion). A section the
 * parser refuses is counted, never guessed at. Text is kept exactly as
 * served, so a composed file is the patch's own bytes.
 */
export function patchSections(text: string): { sections: Map<string, PatchSection>; unparsed: number } {
  const heads: number[] = []
  const re = /^diff --git /gm
  for (let m = re.exec(text); m !== null; m = re.exec(text)) heads.push(m.index)
  const sections = new Map<string, PatchSection>()
  let unparsed = 0
  heads.forEach((at, i) => {
    const piece = text.slice(at, heads[i + 1] ?? text.length)
    const parsed = parseUnifiedDiff(piece)
    if (!parsed.ok || parsed.files.length !== 1) {
      unparsed += 1
      return
    }
    const file = parsed.files[0]!
    sections.set(file.path, { text: piece.endsWith('\n') ? piece : `${piece}\n`, file })
  })
  if (heads.length === 0 && text.trim() !== '') unparsed += 1
  return { sections, unparsed }
}

/** A read's sections as the column's file list. */
function sectionCounts(sections: ReadonlyMap<string, PatchSection>): Map<string, FileCount> {
  const out = new Map<string, FileCount>()
  for (const [path, s] of sections) {
    out.set(path, s.file.binary ? { add: null, del: null, binary: true } : { add: s.file.additions, del: s.file.deletions, binary: false })
  }
  return out
}

/** What a credential refusal names, from the attempts (undefined: still reading; null: the read failed). */
function credentialRefusals(task: Task, attempts: readonly AttemptRow[] | null): string[] | null {
  const refused = publishRefusals(task, attempts).filter((r) => r.kind === 'credential')
  return refused.length === 0 ? null : [...new Set(refused.map((r) => r.file).filter((f): f is string => f !== null))]
}

/** Whether a column's patch is a read this tab may need: a patch, or a list that must be checked against refusals first. */
function needsAttempts(task: Task): boolean {
  const src = sourceOf(task, gitOf(task))
  return src.kind === 'patch' || src.kind === 'omitted'
}

/**
 * WHAT IS KNOWN OF ONE STEP, from its task, its attempts (undefined while
 * they are read) and its patch read. Pure, so every state is tested without a
 * render. The refusal is checked BEFORE anything is listed: a withheld step
 * contributes no file, no count and no read.
 */
export function columnFacts(col: StepColumn, attempts: readonly AttemptRow[] | null | undefined, read: PatchRead | undefined): ColumnFacts {
  if (col.taskId === null) return { kind: 'not-started' }
  const task = col.task
  if (task === null) return { kind: 'not-in-read' }
  const quick = credentialRefusals(task, null)
  if (quick !== null) return { kind: 'withheld', files: quick }
  const git = gitOf(task)
  const src = sourceOf(task, git)
  if (src.kind === 'unsummarised') return { kind: 'unsummarised', done: TERMINAL_STATES.has(task.state) }
  if (src.kind === 'zero') return { kind: 'zero' }
  if (attempts === undefined) return { kind: 'checking' }
  const refused = credentialRefusals(task, attempts)
  if (refused !== null) return { kind: 'withheld', files: refused }
  // A handed-on diff is not what `git.files` lists (that is the clone, which it left untouched).
  const listed = git === null || (src.kind === 'patch' && src.handed) ? null : listedFiles(git)
  if (src.kind === 'omitted') return { kind: 'omitted', bytes: src.bytes, files: listed?.files ?? null, complete: listed?.complete ?? false }
  if (listed !== null) return { kind: 'patch', name: src.name, handed: src.handed, files: listed.files, complete: listed.complete, listed: true, read }
  if (read?.kind === 'ok') {
    return { kind: 'patch', name: src.name, handed: src.handed, files: sectionCounts(read.sections), complete: read.whole && read.unparsed === 0, listed: false, read }
  }
  return { kind: 'patch', name: src.name, handed: src.handed, files: null, complete: false, listed: false, read }
}

/** One cell: a count, a measured `·`, withheld, or not known with its reason. */
export type MatrixCell =
  | { kind: 'count'; count: FileCount }
  | { kind: 'none' }
  | { kind: 'withheld' }
  | { kind: 'unknown'; why: string; pending: boolean }

const UNKNOWN_WHY: Readonly<Record<'not-started' | 'not-in-read' | 'checking', string>> = {
  'not-started': 'This step has not started, so it has changed nothing yet.',
  'not-in-read': 'This step’s task was not in this read, so what it changed is not known.',
  checking: 'Reading this step’s attempts, for a publish refusal that would withhold its patch.',
}

export function cellOf(facts: ColumnFacts, path: string): MatrixCell {
  switch (facts.kind) {
    case 'zero':
      return { kind: 'none' }
    case 'withheld':
      return { kind: 'withheld' }
    case 'not-started':
    case 'not-in-read':
    case 'checking':
      return { kind: 'unknown', why: UNKNOWN_WHY[facts.kind], pending: facts.kind === 'checking' }
    case 'unsummarised':
      return {
        kind: 'unknown',
        why: facts.done ? 'This step ended without a git summary, so what it changed is not known.' : 'This step has written nothing yet: its changes are recorded when an attempt ends.',
        pending: false,
      }
    case 'omitted':
    case 'patch': {
      if (facts.files !== null) {
        const c = facts.files.get(path)
        if (c !== undefined) return { kind: 'count', count: c }
        if (facts.complete) return { kind: 'none' }
        return { kind: 'unknown', why: 'This file is past what was listed or read of this step’s patch.', pending: false }
      }
      if (facts.kind === 'omitted') return { kind: 'unknown', why: 'This step’s patch was discarded over the size cap and its files were not listed.', pending: false }
      if (facts.read?.kind === 'failed') return { kind: 'unknown', why: `This step’s patch was not read: ${facts.read.heading}.`, pending: false }
      return { kind: 'unknown', why: 'Reading this step’s patch for the files it changed.', pending: true }
    }
  }
}

/** Every file any step is known to have changed, sorted by path. */
export function matrixFiles(facts: readonly ColumnFacts[]): string[] {
  const all = new Set<string>()
  for (const f of facts) if ((f.kind === 'patch' || f.kind === 'omitted') && f.files !== null) for (const p of f.files.keys()) all.add(p)
  return [...all].sort()
}

/**
 * ONE FILE, EACH STEP'S HUNKS STACKED IN STEP ORDER, as one file section the
 * viewer draws with a `from step` row above each step's run (`DiffView`
 * `stepOf`). The header is the first text section's, as served; the hunks
 * are each step's, as served. A binary section has no hunks to stack: it
 * stands alone only when no step changed the file as text.
 */
export function composeFile(parts: readonly { step: string; section: PatchSection }[]): { patch: string; stepOfHunk: string[] } {
  const text = parts.filter((p) => !p.section.file.binary)
  if (text.length === 0) return { patch: parts[0]?.section.text ?? '', stepOfHunk: [] }
  const split = (s: string): [string, string] => {
    const at = s.search(/^@@/m)
    return at === -1 ? [s, ''] : [s.slice(0, at), s.slice(at)]
  }
  const [header] = split(text[0]!.section.text)
  const stepOfHunk: string[] = []
  let body = ''
  for (const p of text) {
    body += split(p.section.text)[1]
    for (let i = 0; i < p.section.file.hunks.length; i++) stepOfHunk.push(p.step)
  }
  return { patch: header + body, stepOfHunk }
}

/**
 * THE DIGEST OF THE PATCH AS STORED, measured from the text served, or null
 * when that text is not the stored bytes. The review's worker hashed the file
 * it staged (agent_worker/inputs.py `_sha256`); the API serves the same
 * object as UTF-8 text, so the two agree exactly when the read is the whole
 * object, nothing was masked, and no byte was replaced for not being UTF-8.
 * `invalid_utf8_bytes` must be a measured 0: an older API that does not say
 * is not compared, because a replaced byte would read as a different patch.
 */
export async function patchDigest(data: ArtifactContent): Promise<string | null> {
  const whole = data.offset === 0 && data.next_offset === null && !data.truncated
  if (!whole || data.content === null || data.redaction_count > 0 || data.invalid_utf8_bytes !== 0) return null
  try {
    const sum = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(data.content))
    return [...new Uint8Array(sum)].map((b) => b.toString(16).padStart(2, '0')).join('')
  } catch {
    return null
  }
}

/** A patch read's answer as this tab keeps it. */
async function patchReadOf(res: Result<ArtifactContent>): Promise<PatchRead> {
  if (res.status !== 'ok' && res.status !== 'stale') {
    const error =
      res.status === 'error' ? res.error : ({ kind: 'unreachable', httpStatus: null, code: null, message: 'The read returned a shape this tab does not handle.' } satisfies ApiError)
    const status = error.httpStatus === null ? '' : ` · ${error.httpStatus}`
    return { kind: 'failed', heading: `${errorHeading(error)}${status}`, detail: error.message }
  }
  const data = res.data
  if (data.status === 'absent') return { kind: 'failed', heading: 'the patch is not in the bucket', detail: data.detail ?? '' }
  if (data.status !== 'ok' || data.content === null) return { kind: 'failed', heading: 'the patch could not be read as text', detail: data.detail ?? '' }
  const whole = data.offset === 0 && data.next_offset === null && !data.truncated
  const text = whole ? data.content : patchWindow(data.content, data).patch
  const { sections, unparsed } = patchSections(text)
  return { kind: 'ok', sections, whole, unparsed, masked: data.redacted ? data.redaction_count : 0, digest: await patchDigest(data) }
}

interface ReadJob<T> {
  key: string
  load: () => Promise<T>
}

/**
 * THE READ QUEUE: `start` is handed every wanted job, in order, on each
 * render; it starts the ones neither answered nor in flight, at most
 * `MAX_READS_IN_FLIGHT` at once, and keeps each answer by key. A landing
 * frees a slot and renders, so the next render starts the next job. `forget`
 * drops an answer so its job is read again (Retry).
 */
function useReadQueue<T>(): { done: ReadonlyMap<string, T>; start: (jobs: readonly ReadJob<T>[]) => void; forget: (key: string) => void } {
  const [done, setDone] = useState<ReadonlyMap<string, T>>(() => new Map())
  const doneRef = useRef(done)
  doneRef.current = done
  const flying = useRef(new Set<string>())
  const live = useRef(true)
  useEffect(() => {
    live.current = true
    return () => {
      live.current = false
    }
  }, [])
  const start = useCallback((jobs: readonly ReadJob<T>[]) => {
    for (const j of jobs) {
      if (flying.current.size >= MAX_READS_IN_FLIGHT) break
      if (doneRef.current.has(j.key) || flying.current.has(j.key)) continue
      flying.current.add(j.key)
      void j.load().then((r) => {
        flying.current.delete(j.key)
        if (live.current) setDone((m) => new Map(m).set(j.key, r))
      })
    }
  }, [])
  const forget = useCallback((key: string) => {
    setDone((m) => {
      const next = new Map(m)
      next.delete(key)
      return next
    })
  }, [])
  return { done, start, forget }
}

const attemptsKey = (t: Task) => `a ${t.id} ${t.attempt_count} ${t.state}`
const patchKey = (t: Task, name: string) => `p ${t.id} ${t.attempt_count} ${name}`

type Answer = { kind: 'attempts'; rows: AttemptRow[] | null } | { kind: 'patch'; read: PatchRead }

/** `+N −N`, `binary`, or a dash for a count the summary left out. */
function CountText({ c }: { c: FileCount }) {
  if (c.binary) return <>binary</>
  return (
    <>
      <span className="diff-plus">+{c.add ?? '—'}</span> <span className="diff-minus">−{c.del ?? '—'}</span>
    </>
  )
}

/** A column head's second line: what the step is known to have changed, or why that is not known. */
function ColumnSay({ facts, onRetry }: { facts: ColumnFacts; onRetry: () => void }) {
  switch (facts.kind) {
    case 'zero':
      return (
        <>
          <Mark kind="zero" say="Nothing differed from this step's clone: a measured zero, not an unread patch." /> changed nothing
        </>
      )
    case 'withheld':
      return (
        <>
          <Mark kind="unread" say="The worker refused to publish this step's work because a file added a credential. Its patch is never read here." /> withheld
        </>
      )
    case 'checking':
      return <Mark kind="pending" say={UNKNOWN_WHY.checking} />
    case 'not-started':
      return <span className="ctl-sub">not started</span>
    case 'not-in-read':
      return <Mark kind="absent" say={UNKNOWN_WHY['not-in-read']} />
    case 'unsummarised':
      return (
        <>
          <Mark kind="absent" say={facts.done ? 'This step ended without a git summary.' : 'This step has written nothing yet.'} />{' '}
          {facts.done ? 'no summary' : 'nothing yet'}
        </>
      )
    case 'omitted':
    case 'patch': {
      const n = facts.files === null ? null : facts.files.size
      const failed = facts.kind === 'patch' && facts.read?.kind === 'failed' ? facts.read : null
      return (
        <>
          {facts.kind === 'omitted' && (
            <Mark kind="partial" say={`This step's patch was discarded${facts.bytes === null ? '' : ` at ${num(facts.bytes)} bytes`} for exceeding the size cap. It was not truncated, so no hunk of it can be drawn.`} />
          )}
          {facts.kind === 'patch' && facts.handed && <Mark kind="partial" say="This step changed nothing in its clone and handed this diff on to a dependant." />}
          {n !== null ? ` ${n}${facts.complete ? '' : '+'} ${n === 1 ? 'file' : 'files'}` : failed !== null ? null : <Mark kind="pending" say="Reading this step's patch for the files it changed." />}
          {failed !== null && (
            <>
              <Mark kind="unread" say={`This step's patch was not read: ${failed.heading}. ${failed.detail}`} />{' '}
              <Button size="sm" className="retry" onClick={onRetry}>
                Retry
              </Button>
            </>
          )}
        </>
      )
    }
  }
}

function Cell({ cell }: { cell: MatrixCell }) {
  if (cell.kind === 'count') {
    return (
      <span className="chg-mx-cell">
        <CountText c={cell.count} />
      </span>
    )
  }
  if (cell.kind === 'none') {
    return (
      <span className="chg-mx-cell is-none" title="This step did not change this file.">
        ·
      </span>
    )
  }
  if (cell.kind === 'withheld') {
    return <span className="chg-mx-cell is-unknown" title="Withheld: this step's publish was refused for a credential.">withheld</span>
  }
  return (
    <span className={`chg-mx-cell is-unknown${cell.pending ? ' is-pending' : ''}`} role="img" aria-label={cell.why} title={cell.why} />
  )
}

/**
 * THE FILES × STEPS MATRIX, with its step filter and the open file's hunks by
 * step. `at` is the address's half of it; the caller writes `onAt` back.
 */
export function ChangesMatrix({
  cols,
  at,
  onAt,
  pullRequest,
}: {
  cols: readonly StepColumn[]
  at: ChangesAt
  onAt: (at: ChangesAt) => void
  pullRequest?: { url: string; label: string } | null
}) {
  const queue = useReadQueue<Answer>()
  const answers = queue.done
  const jobs: ReadJob<Answer>[] = []

  // Pass one: the facts from what has landed.
  const attemptsOf = (t: Task): AttemptRow[] | null | undefined => {
    const a = answers.get(attemptsKey(t))
    return a?.kind === 'attempts' ? a.rows : undefined
  }
  const readOf = (t: Task, name: string): PatchRead | undefined => {
    const a = answers.get(patchKey(t, name))
    return a?.kind === 'patch' ? a.read : undefined
  }
  const facts = cols.map((c) => {
    if (c.task === null) return columnFacts(c, undefined, undefined)
    const src = sourceOf(c.task, gitOf(c.task))
    return columnFacts(c, attemptsOf(c.task), src.kind === 'patch' ? readOf(c.task, src.name) : undefined)
  })

  // The review's findings, each checked against the digest of the patch it
  // would be pinned on (variant 3, `wfreview.placeFinding`).
  const findings = reviewFindings(cols.flatMap((c) => (c.task === null ? [] : [c.task])))
  const shownPatches = shownPatchesOf(cols, facts)
  const placements = findings.map((f) => placeFinding(f, shownPatches))

  const filter = at.step !== null && cols.some((c) => c.key === at.step) ? at.step : null
  // Files named by the review sort first, in patch order otherwise: grouped by
  // what the review said, never by a risk score nobody measured (design §2 variant 3).
  const named = new Set(findings.flatMap((f) => (f.file === null ? [] : [f.file])))
  const every = matrixFiles(facts)
  const all = [...every.filter((p) => named.has(p)), ...every.filter((p) => !named.has(p))]
  const filterAt = filter === null ? -1 : cols.findIndex((c) => c.key === filter)
  const rows = filterAt === -1 ? all : all.filter((p) => cellOf(facts[filterAt]!, p).kind === 'count')
  const file = at.file !== null && rows.includes(at.file) ? at.file : (rows[0] ?? null)

  // Pass two: what to read. Attempts first (a refusal withholds), then the
  // patches whose files are not listed, then the ones the open file needs.
  cols.forEach((c) => {
    if (c.task !== null && needsAttempts(c.task) && credentialRefusals(c.task, null) === null) {
      const task = c.task
      jobs.push({
        key: attemptsKey(task),
        load: async () => {
          const r = await loadAttempts(task.id)
          return { kind: 'attempts', rows: r.status === 'ok' || r.status === 'stale' ? r.data.attempts : r.status === 'empty' ? [] : null }
        },
      })
    }
  })
  const patchJob = (task: Task, name: string): ReadJob<Answer> => ({
    key: patchKey(task, name),
    load: async () => ({ kind: 'patch', read: await patchReadOf(await loadArtifactContent(task.id, name)) }),
  })
  facts.forEach((f, i) => {
    const task = cols[i]!.task
    if (f.kind === 'patch' && !f.listed && task !== null) jobs.push(patchJob(task, f.name))
  })
  facts.forEach((f, i) => {
    const task = cols[i]!.task
    if (file === null || task === null || f.kind !== 'patch' || !f.listed) return
    if (filterAt !== -1 && filterAt !== i) return
    if (f.files?.has(file)) jobs.push(patchJob(task, f.name))
  })
  // Last, the patches a located finding would be pinned on, for their digests.
  facts.forEach((f, i) => {
    const task = cols[i]!.task
    if (task === null || f.kind !== 'patch' || f.read !== undefined) return
    if (findings.some((x) => x.line !== null && x.patches.some((p) => p.taskId === task.id && p.filename === f.name))) jobs.push(patchJob(task, f.name))
  })
  // Every render: what is wanted changes with each landing and each choice.
  useEffect(() => queue.start(jobs))
  const retry = (task: Task, name: string) => queue.forget(patchKey(task, name))
  const retryOf = (i: number) => () => {
    const f = facts[i]!
    const task = cols[i]!.task
    if (f.kind === 'patch' && task !== null) retry(task, f.name)
  }

  const shown = cols.map((_, i) => filterAt === -1 || filterAt === i)
  const anyUnknown = facts.some((f) => !(f.kind === 'zero' || ((f.kind === 'patch' || f.kind === 'omitted') && f.files !== null && f.complete)))
  const masked = facts.reduce((n, f) => n + (f.kind === 'patch' && f.read?.kind === 'ok' ? f.read.masked : 0), 0)

  return (
    <div className="chg-mx">
      <div className="chg-mx-bar">
        <div className="chg-mx-chips" role="group" aria-label="Steps">
          <button type="button" className="chg-mx-chip" aria-pressed={filter === null} onClick={() => onAt({ step: null, file })}>
            All steps
          </button>
          {cols.map((c) => (
            <button key={c.key} type="button" className="chg-mx-chip mono" aria-pressed={filter === c.key} onClick={() => onAt({ step: c.key, file })}>
              {c.label}
            </button>
          ))}
        </div>
        <p className="chg-sum">
          <span>
            {all.length}
            {anyUnknown ? '+' : ''} {all.length === 1 && !anyUnknown ? 'file' : 'files'} across {cols.length} {cols.length === 1 ? 'step' : 'steps'}
          </span>
          {masked > 0 && (
            <span className="chg-masked">
              masked <span className="art-masked is-warn">{masked}</span>
            </span>
          )}
          {pullRequest && httpUrl(pullRequest.url) !== null && (
            <a className="ctl-link" href={pullRequest.url} target="_blank" rel="noopener noreferrer">
              {pullRequest.label} ↗
            </a>
          )}
        </p>
      </div>
      {findings.length > 0 && (
        <ReviewFindings
          findings={findings}
          placements={placements}
          rows={rows}
          onOpen={(path, key) => onAt({ step: filter === null || filter === key ? filter : null, file: path })}
        />
      )}
      {cols.length === 0 ? (
        <State mark="zero" heading="no steps" say="This has no steps, so nothing changed any file." />
      ) : (
        <div className="chg-mx-card">
          <table className="chg-mx-table" aria-label="Files by step">
            <thead>
              <tr>
                <th scope="col">File</th>
                {cols.map((c, i) => (
                  <th key={c.key} scope="col" className={shown[i] ? 'chg-mx-step' : 'chg-mx-step is-dim'} data-step={c.key}>
                    <span className="mono">{c.integrator ? `integrate · ${c.label}` : c.label}</span>
                    <span className="chg-mx-say">
                      <ColumnSay facts={facts[i]!} onRetry={retryOf(i)} />
                    </span>
                    {c.taskId !== null && (
                      <a className="ctl-link chg-mx-agent" href={agentChangesHref(c.taskId, c.task ?? undefined)} title={`${c.label}'s own Changes tab`}>
                        Changes ›
                      </a>
                    )}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((path) => (
                <tr key={path} className={path === file ? 'is-on' : undefined} data-file={path}>
                  <th scope="row">
                    <button type="button" className="chg-mx-file mono" aria-pressed={path === file} onClick={() => onAt({ step: filter, file: path })}>
                      {path}
                    </button>
                    {named.has(path) && <span className="chg-fd-named">named by the review</span>}
                  </th>
                  {cols.map((c, i) => (
                    <td key={c.key} className={shown[i] ? undefined : 'is-dim'}>
                      <Cell cell={cellOf(facts[i]!, path)} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          {rows.length === 0 && <EmptyRows cols={cols} facts={facts} filterAt={filterAt} />}
        </div>
      )}
      {file !== null && (
        <FileByStep
          path={file}
          cols={cols}
          facts={facts}
          shown={shown}
          pullRequest={pullRequest ?? null}
          onRetry={retry}
          pins={pinsOf(findings, placements)}
        />
      )}
    </div>
  )
}

/** Each column's patch as the digest check sees it: its task, name, measured digest and files. */
function shownPatchesOf(cols: readonly StepColumn[], facts: readonly ColumnFacts[]): ShownPatch[] {
  return cols.flatMap((c, i) => {
    const f = facts[i]!
    if (c.task === null || f.kind !== 'patch') return []
    const digest = f.read === undefined ? undefined : f.read.kind === 'ok' ? f.read.digest : null
    return [{ key: c.key, label: c.label, taskId: c.task.id, name: f.name, digest, files: f.files === null ? null : new Set(f.files.keys()) }]
  })
}

const TONE: Readonly<Record<ReviewFinding['severity'], DiffPin['tone']>> = { blocker: 'bad', major: 'warn', minor: 'info', none: 'info' }

/** The pinned findings, as the viewer's pins, each bound to the step whose hunks its line counts in. */
function pinsOf(findings: readonly ReviewFinding[], placements: readonly Placement[]): DiffPin[] {
  return findings.flatMap((f, i) => {
    const p = placements[i]!
    if (p.kind !== 'pinned') return []
    return [{ id: f.id, path: p.file, side: p.side, line: p.line, step: p.label, severity: SEVERITY_LABEL[f.severity], tone: TONE[f.severity], summary: f.summary }]
  })
}

/**
 * THE REVIEW'S FINDINGS, every one, each saying where it is: pinned beside a
 * line of a named step's patch (a button that opens the file), from an
 * earlier patch than the one shown, not placed, or not compared -- and why.
 * Only a pinned finding has a mark in the viewer.
 */
function ReviewFindings({
  findings,
  placements,
  rows,
  onOpen,
}: {
  findings: readonly ReviewFinding[]
  placements: readonly Placement[]
  rows: readonly string[]
  onOpen: (path: string, key: string | null) => void
}) {
  const count = (k: Placement['kind']) => placements.filter((p) => p.kind === k).length
  const pinned = count('pinned')
  const earlier = count('earlier')
  const unplaced = count('unplaced')
  return (
    <section className="chg-fd" aria-label="Review findings">
      <p className="chg-fd-head">
        <span>
          {findings.length} review {findings.length === 1 ? 'finding' : 'findings'}
        </span>
        <span>{pinned} pinned</span>
        {earlier > 0 && <span>{earlier} from an earlier patch</span>}
        {unplaced > 0 && <span>{unplaced} not placed</span>}
      </p>
      <ol className="chg-fd-list">
        {findings.map((f, i) => {
          const p = placements[i]!
          const where = f.file === null ? null : f.line === null ? f.file : `${f.file} · ${f.side} ${f.line}`
          return (
            <li key={f.id} className={`chg-fd-item is-${p.kind}`} data-finding={f.id}>
              <span className={`chg-fd-sev is-${TONE[f.severity]}`}>{SEVERITY_LABEL[f.severity]}</span> <span className="chg-fd-text">{f.summary}</span>{' '}
              <span className="chg-fd-where">
                {p.kind === 'pinned' ? (
                  <button type="button" className="chg-mx-file" onClick={() => onOpen(p.file, p.key)}>
                    pinned · {p.label} · {where}
                  </button>
                ) : (
                  <>
                    {where !== null && <span className="mono">{where}</span>}
                    {where !== null && ' · '}
                    {placementWords(p)}
                    {f.file !== null && rows.includes(f.file) && (
                      <>
                        {' '}
                        <button type="button" className="chg-mx-file" aria-label={`Open ${f.file}`} onClick={() => onOpen(f.file!, null)}>
                          open file
                        </button>
                      </>
                    )}
                  </>
                )}
              </span>
            </li>
          )
        })}
      </ol>
    </section>
  )
}

function placementWords(p: Exclude<Placement, { kind: 'pinned' }>): string {
  switch (p.kind) {
    case 'earlier':
      return `from an earlier patch: the review read a patch ${p.label} has since replaced, so its line numbers may have drifted; not pinned`
    case 'unplaced':
      return `not placed: ${p.why}`
    case 'checking':
      return 'checking: reading the patch the review read, to compare its digest; not pinned yet'
    case 'uncompared':
      return `not compared: the patch shown for ${p.label} is not its stored bytes (a window, masked, or not UTF-8 throughout, or not read), so its digest cannot be measured here; not pinned`
  }
}

/** Why the matrix has no row: a measured zero, or not known yet -- never one dressed as the other. */
function EmptyRows({ cols, facts, filterAt }: { cols: readonly StepColumn[]; facts: readonly ColumnFacts[]; filterAt: number }) {
  const scope = filterAt === -1 ? facts : [facts[filterAt]!]
  const known = scope.every((f) => f.kind === 'zero' || ((f.kind === 'patch' || f.kind === 'omitted') && f.files !== null && f.complete))
  if (known) {
    const who = filterAt === -1 ? 'No step' : cols[filterAt]!.label
    return (
      <State
        mark="zero"
        heading={`${who} changed no files`}
        say={`${filterAt === -1 ? 'Every step' : 'This step'} has a known, complete file list and none is listed: a measured zero, not an unread patch.`}
      />
    )
  }
  if (scope.every((f) => f.kind === 'withheld')) {
    return (
      <State
        mark="unread"
        heading="withheld · a file failed the credential scan"
        say="The worker refused to publish this work because a file added a credential. No file and no line of it is drawn here."
      />
    )
  }
  return (
    <State
      mark="absent"
      heading="no file known yet"
      say="No step's changes are known yet: not started, nothing written, still reading, or not read. That is not the same as no changes."
    />
  )
}

/**
 * THE OPEN FILE, EACH STEP'S HUNKS STACKED AND TAGGED. A step that changed
 * the file but whose hunks are not in hand says why, under the bar: still
 * reading, not read (Retry), past the first window, discarded over the cap.
 */
function FileByStep({
  path,
  cols,
  facts,
  shown,
  pullRequest,
  onRetry,
  pins,
}: {
  path: string
  cols: readonly StepColumn[]
  facts: readonly ColumnFacts[]
  shown: readonly boolean[]
  pullRequest: { url: string; label: string } | null
  onRetry: (task: Task, name: string) => void
  /** The pinned findings: only those on a drawn step's section of this file reach the viewer. */
  pins: readonly DiffPin[]
}) {
  const parts: { step: string; section: PatchSection }[] = []
  const missing: { col: StepColumn; mark: 'pending' | 'unread' | 'partial'; say: string; retry: (() => void) | null }[] = []
  const changedBy: string[] = []
  cols.forEach((col, i) => {
    const f = facts[i]!
    if (!shown[i] || cellOf(f, path).kind !== 'count') return
    changedBy.push(col.label)
    if (f.kind === 'omitted') {
      missing.push({ col, mark: 'partial', say: 'its patch was discarded over the size cap, so no hunk of it can be drawn', retry: null })
      return
    }
    if (f.kind !== 'patch') return
    const r = f.read
    if (r === undefined) missing.push({ col, mark: 'pending', say: 'reading its patch', retry: null })
    else if (r.kind === 'failed') {
      const task = col.task
      missing.push({ col, mark: 'unread', say: `its patch was not read: ${r.heading}`, retry: task === null ? null : () => onRetry(task, f.name) })
    } else {
      const s = r.sections.get(path)
      if (s !== undefined) parts.push({ step: col.label, section: s })
      else missing.push({ col, mark: 'partial', say: 'this file lies past the first window of its patch; read it on the step’s own Changes tab', retry: null })
    }
  })
  // Keyed by the parts' text, not the array, which is new on every render.
  const partsKey = parts.map((p) => `${p.step}\n${p.section.text}`).join('\0')
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const composed = useMemo(() => composeFile(parts), [partsKey])
  const stepOf = useCallback((_: string, hunk: number) => composed.stepOfHunk[hunk] ?? null, [composed])
  const here = pins.filter((p) => parts.some((x) => x.step === p.step && (p.path === x.section.file.path || p.path === x.section.file.newPath || p.path === x.section.file.oldPath)))
  const pinsKey = here.map((p) => p.id).join('\0')
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const filePins = useMemo(() => here, [pinsKey])

  const meta = (
    <div className="chg-mx-meta" role="note" aria-label="Where these hunks come from">
      <p>
        <Mark
          kind="partial"
          say="Each step diffed against its own base, so a hunk's line numbers count that step's file. Which step wrote each line of the pull request is not recorded anywhere."
        />{' '}
        which step wrote each line: not recorded
      </p>
      {missing.map((m) => (
        <p key={m.col.key}>
          <Mark kind={m.mark} say={`${m.col.label}: ${m.say}.`} /> <span className="mono">{m.col.label}</span>: {m.say}.{' '}
          {m.retry !== null && (
            <Button size="sm" className="retry" onClick={m.retry}>
              Retry
            </Button>
          )}
          {m.col.taskId !== null && m.mark === 'partial' && (
            <a className="ctl-link" href={agentChangesHref(m.col.taskId, m.col.task ?? undefined)}>
              Changes ›
            </a>
          )}
        </p>
      ))}
    </div>
  )
  return (
    <section className="chg-mx-file-view" aria-label={`${path}, by step`}>
      <p className="chg-sum">
        <span className="mono">{path}</span>
        <span>changed by {changedBy.join(', ')}</span>
      </p>
      {parts.length === 0 ? (
        meta
      ) : (
        <div className="chg-diff chg-mx-diff">
          <DiffView
            key={path}
            patch={composed.patch}
            name={path}
            fullHeight
            initialFile={path}
            stepOf={stepOf}
            pins={filePins}
            copy={{ label: 'Copy hunks', text: composed.patch }}
            download={{ refused: 'These are several steps’ hunks for one file, each against its own base: not a patch that applies. Each step’s own Changes tab downloads its patch.' }}
            meta={meta}
            {...(pullRequest === null ? {} : { pullRequest })}
          />
        </div>
      )}
    </section>
  )
}

/** The tab's own address when its caller keeps none: the matrix still works, it is just not a link. */
function useLocalAt(at: ChangesAt | undefined, onAt: ((at: ChangesAt) => void) | undefined): [ChangesAt, (at: ChangesAt) => void] {
  const [local, setLocal] = useState<ChangesAt>({ step: null, file: null })
  return onAt !== undefined && at !== undefined ? [at, onAt] : [local, setLocal]
}

/** The workflow's pull request, for the bar and the viewer, or null. */
function prOf(workflow: Workflow, taskById: ReadonlyMap<string, Task> | null): { pr: { url: string; label: string } | null; stepId: string | null } {
  const pr = workflowPullRequest(workflow, taskById)
  if (pr === null) return { pr: null, stepId: null }
  return { pr: pr.href === null ? null : { url: pr.href, label: `PR #${pr.number}` }, stepId: pr.stepId }
}

/** A workflow's Changes tab (`/workflows/<id>/changes?step=<step>&file=<path>`). */
export function WorkflowChangesTab({
  workflow,
  taskById,
  at,
  onAt,
}: {
  workflow: Workflow
  taskById: ReadonlyMap<string, Task> | null
  at?: ChangesAt
  onAt?: (at: ChangesAt) => void
}) {
  const [here, go] = useLocalAt(at, onAt)
  const { pr, stepId } = prOf(workflow, taskById)
  const cols: StepColumn[] = workflow.steps.map((s) => {
    const taskId = s.task_id ? s.task_id : null
    return { key: s.step_id, label: s.step_id, taskId, task: taskId === null ? null : (taskById?.get(taskId) ?? null), integrator: s.step_id === stepId }
  })
  return (
    <section className="chg-tab chg-wf" aria-label="Changes in this workflow">
      <ChangesMatrix cols={cols} at={here} onAt={go} pullRequest={pr} />
    </section>
  )
}

/** An issue run's Changes tab (`/runs/<id>/changes`): its workflow's steps, then each CI fix round's. */
export function RunChangesTab({ run, at, onAt }: { run: IssueRun; at?: ChangesAt; onAt?: (at: ChangesAt) => void }) {
  const [here, go] = useLocalAt(at, onAt)
  const read = useRunWorkflows(run)
  if ((run.workflow_id ?? null) === null) {
    return (
      <section className="chg-tab chg-run" aria-label="Changes in this run">
        <State
          mark="zero"
          heading="no workflow yet"
          say="The run's workflow is created when its plan is approved. Until then no step has run, so nothing has changed."
        />
      </section>
    )
  }
  if (read.loads === null) {
    return (
      <section className="chg-tab chg-run" aria-label="Changes in this run">
        <p className="art-loading">
          <Mark kind="pending" say="Reading the run's workflow for its steps. The read is in flight." />
          <span className="ctl-pending art-loading-bar" />
        </p>
      </section>
    )
  }
  const main = read.loads.find((l) => l.round === 0)?.data ?? null
  const { pr, stepId } = main === null ? { pr: null, stepId: null } : prOf(main.workflow, new Map(main.tasks.map((t) => [t.id, t])))
  const cols: StepColumn[] = stepRows(run, read).map((r) => ({
    key: r.round === 0 ? r.stepId : `${r.round}:${r.stepId}`,
    label: r.round === 0 ? r.stepId : `fix ${r.round} · ${r.stepId}`,
    taskId: r.taskId,
    task: r.task,
    integrator: r.round === 0 && r.stepId === stepId,
  }))
  const failed = read.loads.filter((l) => l.error !== null)
  return (
    <section className="chg-tab chg-run" aria-label="Changes in this run">
      {failed.map((l) => (
        <p key={l.wf} className="chg-note" role="status">
          <Mark kind="unread" say={`${l.round === 0 ? 'The workflow' : `Fix round ${l.round}'s workflow`} could not be read: ${l.error}`} />{' '}
          {l.round === 0 ? 'The workflow' : `Fix round ${l.round}'s workflow`} <span className="mono">{l.wf}</span> could not be read
          {l.data === null ? ', so its steps are not drawn' : ', so its columns are the last read'}.
        </p>
      ))}
      <ChangesMatrix cols={cols} at={here} onAt={go} pullRequest={pr} />
    </section>
  )
}
