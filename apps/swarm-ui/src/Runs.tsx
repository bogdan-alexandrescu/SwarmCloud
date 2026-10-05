import { useEffect, useRef, useState, type ReactNode } from 'react'
import { approvePlan, editPlan, loadRun, loadRuns, rejectPlan } from './api'
import type { ApiError, Result } from './fetch'
import { InlineText as RnText, runAddress } from './IssueSubmit'
import { MarkGlyph, type MarkHue, type MarkName } from './marks'
import { Banner, Button, Card, Chip, CIcon, CodeBlock, WarnMark } from './components'
import { Dash } from './components/Chip'
import { FailedPanel, Screen, timeAgo } from './Shell'
import { workflowPullRequest, type WorkflowPullRequest } from './stepviews'
import { pluralise } from './types'
import {
  CostFact, PlanStepLive, progressEntries, redReading, shortSha, StepProgress, stepRows, StepsCard, useRunWorkflows,
  type RunWorkflows, type StepRow,
} from './RunSteps'
import type { IssueRun, IssueRunPage, IssueRunState, OpenWork, PlanOverlap, PlanStepDoc, RunPlan } from './types'
import { useNow } from './useNow'
import './styles/intake.css'
import './styles/runs.css'

/**
 * WORK › RUNS (intake-tenants.html 1A, picked 2026-10-02): `/runs`, this
 * tenant's issue runs newest first, and `/runs/<id>`, one run.
 *
 * EVERY READ ADVANCES THE RUN (routes/runs.py): the API moves a run from
 * PLANNING to PLANNED, and from RUNNING to its end, when a read finds its task
 * or workflow has moved. So a run that is not finished is re-read on a
 * cadence; a finished one is read once.
 *
 * THE DIGEST IS THE APPROVAL (owner decision D3). Approve sends the digest of
 * the plan ON THIS SCREEN, and the API refuses it with 409 `plan_changed` when
 * the plan has been edited since -- this page then re-reads the run and says
 * so, rather than approving whatever the plan had become. Edit and Reject
 * carry the shown digest for the same reason.
 *
 * THE OVERLAPS COME FIRST (#454's acceptance test is that the plan names
 * them): each is a link to the issue or pull request the planner found in
 * flight, with its kind and the planner's note, above the steps. A plan
 * written before the planner read open work does not say, and the page says
 * that -- never "none", which would be a finding nobody made.
 *
 * EVERYTHING READ FROM GITHUB OR WRITTEN BY THE PLANNER IS TEXT. An overlap's
 * ref becomes a link only when it is `owner/repo#N` (issueruns'
 * OVERLAP_REF_PATTERN); a pull request's URL only when it is on github.com;
 * the failing-check excerpt is drawn as preformatted text, never as HTML.
 *
 * WHAT THE RUN HAS NOT GOT YET IS SAID, NOT HIDDEN: a pull request not yet
 * opened, a comment not yet posted, a keyword the review has not decided.
 * Cost is summed from the step tasks that report one, with how many do; none
 * reporting is a dash with that reason, never $0. A workflow not
 * yet created is "none yet", because the run does serve `workflow_id`, and
 * null is a fact.
 *
 * THE PAGE LEADS WITH THE ISSUE (lane U9, owner 2026-10-03). Its title is the
 * issue's title as the run read it at submission (`issue_read`), its meta
 * `owner/repo#N · run_… · created by …`; the plan is drawn from its schema
 * -- a short lead, numbered steps with their prompts folded, the raw plan
 * behind a disclosure -- rather than as one long paragraph.
 *
 * THE PAGE FOLLOWS THE RUN (lane U14, owner 2026-10-04). While the run has a
 * workflow, a Steps card under the status line lists every step with its
 * state and a link to its agent (RunSteps.tsx); Progress interleaves the
 * steps' changes; cost is the step tasks' recorded cost with its coverage.
 * From CHECKING the pull request card leads, and once the plan is approved
 * the overlaps and the plan fold to one line each.
 *
 * Classes are `rn-` so a later pass can swap them for lane U0's components.
 */

/** A run that has not finished is re-read on this cadence: each read advances it. */
export const RUN_POLL_MS = 15_000

/** `issueruns.AUTO_APPROVER`: who `approved_by` names on an automatic approval. */
const AUTO_APPROVER = 'auto-approval'

/** `issueruns.OVERLAP_REF_PATTERN`: the only overlap ref that is made a link. */
const GITHUB_REF = /^([A-Za-z0-9][A-Za-z0-9-]{0,38})\/([A-Za-z0-9._-]{1,100})#([1-9][0-9]{0,9})$/

/**
 * Where an overlap points on GitHub, or null when its ref is not `owner/repo#N`.
 * The planner wrote it, so it is data: anything else is drawn as text.
 */
export function overlapUrl(o: PlanOverlap): string | null {
  const m = GITHUB_REF.exec(o.ref)
  if (m === null) return null
  return `https://github.com/${m[1]}/${m[2]}/${o.kind === 'pull_request' ? 'pull' : 'issues'}/${m[3]}`
}

/** A link the API served from GitHub, kept only when it is on github.com over https. */
function githubLink(url: string | null | undefined): string | null {
  return typeof url === 'string' && url.startsWith('https://github.com/') ? url : null
}

/** A comment on the run's issue, by the id the write-back recorded. */
function commentUrl(run: IssueRun, id: number | null | undefined): string | null {
  const issue = githubLink(run.issue.url)
  return issue === null || typeof id !== 'number' ? null : `${issue}#issuecomment-${id}`
}

/** The brand state marks (marks.tsx) for a run's states. */
const RUN_MARK: Readonly<Record<IssueRunState, { mark: MarkName; hue: MarkHue }>> = {
  // A planner task is working: it can hold capacity, as any task.
  PLANNING: { mark: 'running', hue: 'live' },
  // Waiting for a person, holding nothing (invariant 1): a park.
  PLANNED: { mark: 'parked', hue: 'park' },
  APPROVED: { mark: 'starting', hue: 'live' },
  RUNNING: { mark: 'running', hue: 'live' },
  // Waiting on CI, holding nothing (invariant 1): a park, like PLANNED.
  CHECKING: { mark: 'parked', hue: 'park' },
  // One continuation is fixing CI: a task working, as RUNNING.
  FIXING: { mark: 'running', hue: 'live' },
  DONE: { mark: 'succeeded', hue: 'neu' },
  FAILED: { mark: 'failed', hue: 'bad' },
  REJECTED: { mark: 'cancelled', hue: 'neu' },
  CANCELLED: { mark: 'cancelled', hue: 'neu' },
}

/** A run's state in words: sentence case, never the API's capitals (PICKS.md, no all-caps). */
export function runStateWord(state: IssueRunState): string {
  return state.charAt(0) + state.slice(1).toLowerCase()
}

/**
 * A run's state as the canonical state pill (components.html A, `c-pill`):
 * the brand mark and the state in sentence case. It was the mark beside the
 * API's word in capitals -- "RUNNING", "DONE" -- against the no-all-caps rule
 * (browser QA D11, 2026-10-04). The API's spelling stays its title.
 */
export function RunStateMark({ state }: { state: IssueRunState }) {
  const { mark, hue } = RUN_MARK[state] ?? { mark: 'queued', hue: 'neu' }
  return (
    <span className={`c-pill is-${hue}`} data-mark={mark} data-hue={hue} title={state}>
      <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
        <MarkGlyph mark={mark} />
      </svg>
      {runStateWord(state)}
    </span>
  )
}

/** `sha256:9f2c41…e7`: enough to compare by eye; the whole digest is its title. */
export function shortDigest(digest: string): string {
  return digest.length > 18 ? `${digest.slice(0, 13)}…${digest.slice(-2)}` : digest
}

/** The run a Runs address names (`run=<id>`), or null for the list. */
function runOf(view: string | null): string | null {
  if (view === null || view === '') return null
  const id = new URLSearchParams(view).get('run')
  return id === null || id === '' ? null : id
}

/**
 * THE META CHIP OPENS ON A TAP (owner QA F, 2026-10-04): on a phone the chip
 * `owner/repo#N · run_… · created by …` was cut to its first words, and a
 * title is no help where there is no hover. A tap draws it whole, wrapped; a
 * second folds it. Its text is its children, so the page head's `textOf`
 * still gives the chip its title.
 */
function RunMeta({ children }: { children: string }) {
  const [open, setOpen] = useState(false)
  return (
    <button type="button" className={open ? 'rn-meta is-open' : 'rn-meta'} aria-expanded={open} title={children}
      onClick={() => setOpen((o) => !o)}>
      {children}
    </button>
  )
}

/**
 * A value cut ONLY at a safe point (owner QA A and C, 2026-10-04): after a
 * `/`, `#`, `_` or `::` -- the joints of a reference, a path or a test id --
 * and at a space. Each joint is followed by a `<wbr>`, and the value's
 * `overflow-wrap` stays normal, so a line never breaks mid-identifier
 * ('…_is_rec / orded_on…' was the measured break). `marks` narrows the joints
 * (a test id's list breaks at `/`, `::` and `_` only).
 */
export function breakAt(text: string, marks: RegExp = /::|[/#_]/g): ReactNode[] {
  const out: ReactNode[] = []
  let at = 0
  for (const m of text.matchAll(new RegExp(marks.source, 'g'))) {
    const end = (m.index ?? 0) + m[0].length
    if (end >= text.length || end === at) continue
    out.push(text.slice(at, end), <wbr key={end} />)
    at = end
  }
  out.push(text.slice(at))
  return out.filter((p) => p !== '')
}

/** A link inside the app: an href for a new tab, `go` for a plain click. */
function InApp({ to, href, go, children, cut = false }: {
  to: string; href: string; go: (to: string) => void; children: string
  /** A single-token id: one line, cut with an ellipsis (`.rn-id`), whole in its title. */
  cut?: boolean
}) {
  // Its whole text is its title: an id cut to its card says what it was (D3).
  return (
    <a href={href} className={cut ? 'mono rn-id' : 'mono'} title={children} onClick={(e) => {
      if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
      e.preventDefault()
      go(to)
    }}>{children}</a>
  )
}

export function RunsScreen({ view, go }: { view: string | null; go: (to: string) => void }) {
  const runId = runOf(view)
  const [reloads, setReloads] = useState(0)
  /** Why the run was re-read, when a refusal made this page do it. */
  const [notice, setNotice] = useState<string | null>(null)
  useEffect(() => setNotice(null), [runId])
  /** The issue's title, once the run is read: the page's title (item 4). */
  const [heading, setHeading] = useState<{ run: string; title: string } | null>(null)

  if (runId !== null) {
    // A local, not an inline expression: the nav-heading test reads the
    // literal below as the tab's heading and skips a per-run title. Until the
    // run is read, the id is all this page knows to call it.
    const pageTitle = heading !== null && heading.run === runId ? heading.title : runId
    const reread = (why: string) => {
      setNotice(why)
      setReloads((n) => n + 1)
    }
    // A heading with no space is a reference or an id: one line, cut, never
    // broken at the owner's hyphen (browser QA D11). A title wraps as prose.
    return (
      <div className={/\s/.test(pageTitle) ? 'rn-run' : 'rn-run is-ref'}>
        <a className="rn-back" href="/runs" onClick={(e) => {
          if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
          e.preventDefault()
          go('work/runs')
        }}>‹ All runs</a>
        {notice !== null && (
          <div className="rn-notice" role="status">
            <WarnMark />
            <span>{notice}</span>
          </div>
        )}
        <Screen
          key={`run:${runId}:${reloads}`}
          title={pageTitle}
          load={() => loadRun(runId)}
          pollMs={(d) => (d === null || d.run.terminal ? null : RUN_POLL_MS)}
          summary={(d) => <RunMeta>{`${d.run.issue.ref} · ${d.run.id} · created by ${d.run.created_by || '—'}`}</RunMeta>}
        >
          {(d) => (
            <RunPage
              run={d.run}
              reread={reread}
              go={go}
              onHeading={(title) => setHeading((was) => (was?.run === runId && was.title === title ? was : { run: runId, title }))}
            />
          )}
        </Screen>
      </div>
    )
  }

  return (
    <Screen
      title="Runs"
      load={() => loadRuns()}
      summary={(d) => `${d.runs.length} shown, newest first${d.next_page_token === null ? '' : ' · older runs exist'}`}
      empty={{
        heading: 'No runs yet',
        body: 'This tenant has not planned an issue. Submit › From a GitHub issue starts one.',
      }}
    >
      {(d) => <RunList first={d} go={go} />}
    </Screen>
  )
}

// ---------------------------------------------------------------------------
// the list
// ---------------------------------------------------------------------------

function RunList({ first, go }: { first: IssueRunPage; go: (to: string) => void }) {
  const now = useNow()
  const [older, setOlder] = useState<{ runs: IssueRun[]; next: string | null; error: ApiError | null; reading: boolean }>(
    { runs: [], next: first.next_page_token, error: null, reading: false },
  )
  // A poll or refresh replaced the first page: what was appended belonged to the old one.
  useEffect(() => setOlder({ runs: [], next: first.next_page_token, error: null, reading: false }), [first])

  async function more() {
    if (older.next === null) return
    setOlder((o) => ({ ...o, reading: true, error: null }))
    const r = await loadRuns(older.next)
    if (r.status === 'ok' || r.status === 'stale') {
      setOlder((o) => ({ runs: [...o.runs, ...r.data.runs], next: r.data.next_page_token, error: null, reading: false }))
    } else if (r.status === 'empty') {
      setOlder((o) => ({ ...o, next: null, reading: false }))
    } else if (r.status === 'error') {
      setOlder((o) => ({ ...o, error: r.error, reading: false }))
    }
  }

  // In the order served: the API orders newest first, and this does not re-sort.
  const rows = [...first.runs, ...older.runs]
  return (
    <div className="rn-list">
      <div className="rn-table-wrap">
        {/* FIXED COLUMNS (browser QA D11): the table was 1085px in a 1056px
            card and cut the By column mid-address. Each id and name is one
            line, cut, whole in its title; the issue leads with its title. */}
        <table className="rn-table">
          <thead>
            <tr>
              <th scope="col" data-col="run">Run</th>
              <th scope="col" data-col="issue">Issue</th>
              <th scope="col" data-col="state">State</th>
              <th scope="col" data-col="approval">Plan approval</th>
              <th scope="col" data-col="workflow">Workflow</th>
              <th scope="col" data-col="created">Created</th>
              <th scope="col" data-col="by">By</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} className="rn-row">
                <td data-label="Run" className="rn-cut" title={r.id}><InApp go={go} to={runAddress(r.id)} href={`/runs/${encodeURIComponent(r.id)}`}>{r.id}</InApp></td>
                <td data-label="Issue">
                  {r.issue_read?.title ? (
                    <>
                      <span className="rn-cut rn-issue-t" title={r.issue_read.title}>{r.issue_read.title}</span>
                      <span className="mono rn-cut rn-issue-ref" title={r.issue.ref}>{r.issue.ref}</span>
                    </>
                  ) : (
                    <span className="mono rn-cut" title={r.issue.ref}>{r.issue.ref}</span>
                  )}
                </td>
                <td data-label="State"><RunStateMark state={r.state} /></td>
                <td data-label="Plan approval">{r.plan_approval}</td>
                <td data-label="Workflow">
                  {r.workflow_id === null ? <i className="ctl-em">none yet</i> : <span className="mono rn-cut" title={r.workflow_id}>{r.workflow_id}</span>}
                </td>
                <td data-label="Created" title={r.created_at}>{timeAgo(r.created_at, now)}</td>
                <td data-label="By">{r.created_by ? <span className="rn-cut" title={r.created_by}>{r.created_by}</span> : <i className="ctl-em">&mdash; not recorded</i>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {older.error !== null && <FailedPanel error={older.error} onRetry={() => void more()} />}
      {older.next !== null && (
        <p className="rn-more">
          <Button disabled={older.reading} onClick={() => void more()}>
            {older.reading ? 'Reading older runs…' : 'Show older runs'}
          </Button>
        </p>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// one run
// ---------------------------------------------------------------------------

/** Where a PLANNED run's actions are drawn: the waiting banner, or the plan's sticky foot. */
type ActionsAt = 'top' | 'foot'

type Acting =
  | { kind: 'idle' }
  | { kind: 'busy'; what: 'approve' | 'edit' | 'reject' }
  | { kind: 'failed'; error: ApiError }

/** What the run's state means, in one line under it. */
function stateLine(run: IssueRun): string {
  switch (run.state) {
    case 'PLANNING':
      return 'A planner task is reading the issue and the repository. It changes nothing.'
    case 'PLANNED':
      return run.plan_approval === 'auto'
        ? 'Holds no capacity. Approval is automatic: the next read approves this plan.'
        : 'Holds no capacity. Waiting for a person.'
    case 'APPROVED':
      return 'Approved; the workflow is being submitted.'
    case 'RUNNING':
      return 'The workflow built from the approved plan is running.'
    case 'CHECKING':
      return 'The pull request is open. Holds no capacity: its CI is read until it is green or red.'
    case 'FIXING':
      return `CI was red: fix round ${run.ci_fix_round ?? 1} of ${run.fix_rounds} is pushing to the pull request.`
    case 'DONE':
      // The CI loop ends a run DONE when its PR merged OR when every required
      // check is green; the run does not serve which (`pull_request.merged`).
      return run.green_sha
        ? 'Every required check is green on the pull request. Its merge is not reported by this run.'
        : run.pull_request
          ? 'The pull request ended the run before its checks were green. Whether it merged is not reported by this run.'
          : 'The workflow succeeded.'
    case 'FAILED':
      return 'The run failed.'
    case 'REJECTED':
      return 'The plan was turned down. Nothing ran.'
    case 'CANCELLED':
      return 'The run was cancelled.'
  }
}

/**
 * Whether a fresh read replaced the plan the reader is looking at while it can
 * still be acted on. A plan first appearing (PLANNING -> PLANNED) and a run that
 * left PLANNED are drawn at once: neither leaves an action naming stale text.
 */
export function planMovedUnder(shown: IssueRun, served: IssueRun): boolean {
  return shown.plan_digest !== null && served.state === 'PLANNED' && served.plan_digest !== shown.plan_digest
}

function RunPage({ run: served, reread, go, onHeading }: {
  run: IssueRun
  reread: (why: string) => void
  go: (to: string) => void
  /** Told the page's title: the issue's, or its reference when the run kept none. */
  onHeading: (title: string) => void
}) {
  const now = useNow()
  // THE ANSWER TO AN ACTION IS DRAWN AT ONCE: the API returns the run it
  // moved, and a poll that lands later replaces it with whatever it reads.
  const [run, setRun] = useState(served)
  // A POLL NEVER SWAPS THE PLAN UNDER THE READER. Approve, Edit and Reject
  // carry the digest of the plan on screen (D3); if a 15s poll replaced that
  // plan silently, the next click would carry a digest for text nobody read,
  // and an editor opened on the old plan would overwrite the new one. So a
  // read that serves a different digest for a still-PLANNED run is held, and
  // a notice offers it; the plan on screen stays the one the actions name.
  const [held, setHeld] = useState<IssueRun | null>(null)
  const shown = useRef(run)
  shown.current = run
  useEffect(() => {
    if (planMovedUnder(shown.current, served)) {
      setHeld(served)
    } else {
      setHeld(null)
      setRun(served)
    }
  }, [served])
  const title = run.issue_read?.title || run.issue.ref
  // On the title alone: `onHeading` is a new function on every render of the screen.
  useEffect(() => onHeading(title), [title])
  const [acting, setActing] = useState<Acting>({ kind: 'idle' })
  // The editor records the digest it opened on, and saves with that one.
  // `at`: which of the two action rows asked to reject, so its form opens where the reader is.
  const [open, setOpen] = useState<{ kind: 'none' } | { kind: 'edit'; digest: string } | { kind: 'reject'; at: ActionsAt }>(
    { kind: 'none' },
  )
  const close = () => setOpen({ kind: 'none' })
  const showHeld = () => {
    if (held === null) return
    setRun(held)
    setHeld(null)
    close()
  }

  const canAct = run.state === 'PLANNED' && run.plan_digest !== null && run.plan !== null
  const busy = acting.kind === 'busy'

  async function act(what: 'approve' | 'edit' | 'reject', call: () => Promise<Result<unknown>>) {
    setActing({ kind: 'busy', what })
    const r = await call()
    if (r.status === 'ok' || r.status === 'stale') {
      const next = (r.data as { run?: IssueRun } | null)?.run
      setActing({ kind: 'idle' })
      close()
      setHeld(null)
      if (next !== undefined && next !== null && typeof next.id === 'string') setRun(next)
      else reread('The API accepted this and did not echo the run, so it was read again.')
      return
    }
    if (r.status !== 'error') return
    if (r.error.code === 'plan_changed') {
      setActing({ kind: 'idle' })
      close()
      reread(
        'The plan changed since you opened it, so nothing was done. This is the plan now: read it, then act on it again.',
      )
      return
    }
    if (r.error.code === 'invalid_run_transition' || r.error.kind === 'conflict') {
      setActing({ kind: 'idle' })
      close()
      reread(`The run moved on since you opened it, so nothing was done: ${r.error.message}`)
      return
    }
    setActing({ kind: 'failed', error: r.error })
  }

  const digest = run.plan_digest
  // THE RUN'S WORKFLOWS, READ EACH TIME THE RUN IS (lane U14): the Steps
  // card, the plan's live states, Progress, the cost and the PR the
  // integrator opened all come from this one read.
  const read = useRunWorkflows(run)
  const wfPr = wfPrOf(run, read)
  const rows = stepRows(run, read)
  const approved = planApproved(run)
  // From CHECKING the pull request is the subject: its card leads (item 5).
  const prLeads = lead(run)
  /** Approve, Edit and Reject -- or the rejection form, where it was asked for. Null while editing. */
  const actions = (at: ActionsAt): ReactNode => {
    if (!canAct || open.kind === 'edit') return null
    if (open.kind === 'reject' && open.at === at) {
      return (
        <RejectForm busy={busy} onCancel={close}
          onReject={(reason) => void act('reject', () => rejectPlan(run.id, digest, reason))} />
      )
    }
    return (
      <div className="rn-actions">
        <Button kind="primary" disabled={busy}
          onClick={() => void act('approve', () => approvePlan(run.id, digest!))}>
          {acting.kind === 'busy' && acting.what === 'approve' ? 'Approving…' : 'Approve and run'}
        </Button>
        <Button disabled={busy} onClick={() => setOpen({ kind: 'edit', digest: digest! })}>Edit plan</Button>
        <Button disabled={busy} onClick={() => setOpen({ kind: 'reject', at })}>Reject</Button>
      </div>
    )
  }
  return (
    <div className="rn-page">
      <section className="rn-main">
        <div className="rn-state">
          <RunStateMark state={run.state} />
          <span className="rn-state-t">{stateLine(run)}</span>
          {run.state === 'PLANNED' && <span className="sb-note">planned {timeAgo(run.updated_at, now)}</span>}
          {run.approved_by !== null && (
            <span className="sb-note">
              approved {run.approved_at === null ? '' : timeAgo(run.approved_at, now)} by{' '}
              {run.approved_by === AUTO_APPROVER ? 'automatic approval' : run.approved_by}
            </span>
          )}
        </div>

        {run.error !== null && (
          <p className="rn-error" role="alert"><b>Why:</b> {run.error}</p>
        )}
        {prLeads && <CiCard run={run} go={go} wfPr={wfPr} />}
        <StepsCard run={run} read={read} go={go} now={now} />
        {run.state === 'REJECTED' && (
          <p className="sb-note">
            Rejected by {run.rejected_by ?? '—'}
            {run.rejection_reason ? `: ${run.rejection_reason}` : ' · no reason given'}
          </p>
        )}

        {/* THE ACTIONS ARE AT THE TOP (owner QA D, 2026-10-04): they were
            only at the very foot, under the overlaps, the plan and the
            requirements. The banner carries them; the plan's own foot
            carries them again, in a bar that sticks to the bottom edge. */}
        {canAct && (
          <div className="rn-wait">
            <p className="rn-wait-t">
              {run.plan_approval === 'required' ? (
                <><b>Waiting for a person.</b> Anyone in tenant <code>{run.tenant_id}</code> may approve, edit or reject.</>
              ) : (
                <><b>Approval is automatic:</b> the next read approves this plan. Anyone in tenant <code>{run.tenant_id}</code> may act on it first.</>
              )}{' '}
              Approving carries this plan&rsquo;s digest, so a plan edited by someone else since you opened it is refused
              rather than run.
            </p>
            {actions('top')}
          </div>
        )}

        {held !== null && (
          <div className="rn-notice rn-held" role="status">
            <WarnMark />
            <span>
              The plan changed since this page drew it
              {held.plan_edited_by !== null && <> (edited by {held.plan_edited_by})</>}. The actions below still name the
              plan shown, so the API will refuse them.
            </span>
            <Button disabled={busy} onClick={showHeld}>Show the plan now</Button>
          </div>
        )}

        {run.writeback_error && (
          <div className="rn-writeback">
            <Banner tone="warn" title="The issue on GitHub was not updated">
              {run.writeback_error} · the run carries on; the next read tries the write again.
            </Banner>
          </div>
        )}

        {run.plan !== null && <Overlaps plan={run.plan} openWork={run.open_work ?? null} folded={approved} />}

        <section className="rn-plan" aria-label="The plan">
          <Fold folded={approved && run.plan !== null}
            line={run.plan === null ? null : <span className="sb-note">{pluralise(run.plan.steps.length, 'step')} · approved</span>}
            head={
              <h3>
                The plan
                {digest !== null && <> · <code className="rn-digest" title={digest}>{shortDigest(digest)}</code></>}
              </h3>
            }>
          {run.plan === null ? (
            <p className="sb-note">
              {run.state === 'PLANNING'
                ? 'No plan yet: the planner task is still working.'
                : 'No plan: the planner did not produce one this run could read.'}
            </p>
          ) : open.kind === 'edit' && canAct ? (
            <PlanEditor
              key={open.digest}
              plan={run.plan}
              busy={busy}
              onCancel={close}
              onSave={(plan) => {
                const opened = open.digest
                void act('edit', () => editPlan(run.id, opened, plan))
              }}
            />
          ) : (
            <>
              <p className="sb-note">
                revision {run.plan_revision}
                {run.plan_edited_by !== null && <> · edited by {run.plan_edited_by}</>}
                {' · '}{run.plan_shape ?? `${pluralise(run.plan.steps.length, 'step')}, then a review and a fix`}
                {' '}gated on its verdict · every step runs as claude-code
              </p>
              <p className="rn-plan-facts">
                <Chip title="The planner's call: one agent, or a workflow of several steps">
                  {run.plan.mode === 'single' ? 'single agent' : run.plan.mode === 'workflow' ? 'workflow' : 'mode not stated'}
                </Chip>
                {run.plan.estimate
                  ? <Chip title="The planner's estimate for the whole plan">estimate {run.plan.estimate}</Chip>
                  : <span className="sb-note">no estimate given</span>}
              </p>
              <PlanBody plan={run.plan} unmet={run.requirements_unmet ?? []}
                live={approved ? { read, rows, go } : null} />
            </>
          )}
          {canAct && open.kind !== 'edit' && <div className="rn-actbar">{actions('foot')}</div>}
          </Fold>
        </section>

        {!prLeads && <CiCard run={run} go={go} wfPr={wfPr} />}
        {textList(run.plan?.risks) !== null && (
          <section className="rn-risks" aria-label="Risks">
            <h3>Risks the planner named</h3>
            <ul className="rn-list-items">
              {textList(run.plan?.risks)!.map((r, i) => <li key={i}><RnText text={r} /></li>)}
            </ul>
          </section>
        )}

        {acting.kind === 'failed' && <FailedPanel error={acting.error} onRetry={() => setActing({ kind: 'idle' })} />}

        <section className="rn-history" aria-label="History">
          <h3>Progress</h3>
          {/* THE STEPS' CHANGES UNDER THE RUN'S, OLDEST FIRST (item 3). */}
          <ol>
            {progressEntries(run, rows).map((h) => h.kind === 'step' ? <StepProgress key={h.key} e={h} now={now} /> : (
              <li key={h.key}>
                <RunStateMark state={h.to} />
                <span>{h.from === null ? 'created' : `from ${h.from}`} · by {h.by || '—'}</span>
                <span className="sb-note" title={h.at ?? undefined}>{h.at === null ? '—' : timeAgo(h.at, now)}</span>
              </li>
            ))}
          </ol>
        </section>
      </section>

      <aside className="rn-side">
        <section className="rn-card rn-links" aria-label="Linked">
          <h3>Linked</h3>
          <ul className="ctl-facts">
            <li className="ctl-fact">
              <b>Issue</b>
              <IssueLink run={run} />
            </li>
            <CommentFact label="Plan comment" name="plan comment" url={commentUrl(run, run.plan_comment_id)}
              absent={run.plan === null ? 'not posted · there is no plan yet' : 'not posted yet'} />
            <CommentFact label="Status comment" name="status comment" url={commentUrl(run, run.status_comment_id)}
              absent="not posted yet" />
            <li className="ctl-fact">
              <b>Planner</b>
              {run.planner_task_id === ''
                ? <i className="ctl-em" title="The run records no planner task.">&mdash; not recorded</i>
                : <InApp go={go} cut to={`work/task/${encodeURIComponent(run.planner_task_id)}`}
                  href={`/agents/recent/${encodeURIComponent(run.planner_task_id)}`}>{run.planner_task_id}</InApp>}
            </li>
            <li className={run.workflow_id === null ? 'ctl-fact is-absent' : 'ctl-fact'}>
              <b>Workflow</b>
              {run.workflow_id === null
                ? <i className="ctl-em" title="None yet: the workflow is created when the plan is approved.">none yet · created on approval</i>
                : <InApp go={go} cut to={`work/workflows?${new URLSearchParams({ wf: run.workflow_id }).toString()}`}
                  href={`/workflows/${encodeURIComponent(run.workflow_id)}`}>{run.workflow_id}</InApp>}
            </li>
            <PullRequestFact run={run} wfPr={wfPr} />
          </ul>
        </section>
        <IssueReadCard run={run} now={now} />
        <section className="rn-card rn-chosen" aria-label="Chosen at submission">
          <h3>Chosen at submission</h3>
          <ul className="ctl-facts">
            <li className="ctl-fact"><b>plan approval</b><span>{run.plan_approval}</span></li>
            <li className="ctl-fact"><b>auto-merge</b><span>{run.auto_merge ? 'on' : 'off'}</span></li>
            <li className="ctl-fact"><b>fix rounds</b><span>up to {run.fix_rounds}</span></li>
            <li className="ctl-fact"><b>by</b>{run.created_by
              ? <span className="rn-id" title={run.created_by}>{run.created_by}</span>
              : <i className="ctl-em">&mdash; not recorded</i>}</li>
            <CostFact read={read} />
          </ul>
        </section>
      </aside>
    </div>
  )
}

/** A list as the editor shows it: one entry per line. */
function linesOf(list: string[] | null | undefined): string {
  return (list ?? []).join('\n')
}

/** The entries a textarea holds: one per non-blank line, trimmed. */
function entries(text: string): string[] {
  return text.split('\n').map((l) => l.trim()).filter((l) => l !== '')
}

/**
 * An optional plan field as edited. Untouched, it is the value the plan had --
 * absent, null or a list -- so a save changes nothing the reader did not
 * change; emptied, it is dropped (the API takes no empty estimate); otherwise
 * it is what was typed.
 */
function editedList(original: string[] | null | undefined, text: string): string[] | null | undefined {
  if (text === linesOf(original)) return original
  const l = entries(text)
  return l.length === 0 ? undefined : l
}

function editedText(original: string | null | undefined, text: string): string | null | undefined {
  if (text === (original ?? '')) return original
  return text.trim() === '' ? undefined : text.trim()
}

/** `target[key] = value`, or no key at all when the value is undefined. */
function put<T extends object, K extends keyof T>(target: T, key: K, value: T[K] | undefined): void {
  if (value === undefined) delete target[key]
  else target[key] = value
}

/**
 * `owner/repo#N`, linked. It wrapped at the owner's hyphen ('bogdan- /
 * alexandrescu/SwarmCloud#454', item 6), then was cut to one line past the
 * card's edge (owner QA A, 2026-10-04). It takes the card's whole width under
 * its label and breaks only after `/` or `#`; its whole text is the title.
 */
function IssueLink({ run }: { run: IssueRun }) {
  return (
    <a href={run.issue_read?.url || run.issue.url} target="_blank" rel="noreferrer" className="mono rn-ref" title={run.issue.ref} aria-label={run.issue.ref}>
      {breakAt(run.issue.ref)}
    </a>
  )
}

/** A plan's optional list (`risks`, a step's `files` or `tests`): its strings, or null when it has none. */
export function textList(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null
  const items = value.filter((v): v is string => typeof v === 'string' && v.trim() !== '')
  return items.length === 0 ? null : items
}

/** The most a plan's lead may run to, in RENDERED characters: two or three lines in the plan's column. */
export const PLAN_LEAD_CHARS = 240

/** What a run of plan text draws: its code spans' backticks are markup, not text (`InlineText`). */
function rendered(text: string): string {
  return text.replace(/`([^`\n]+)`/g, '$1')
}

/**
 * The plan's summary cut to its lead (item 5): its first sentences, up to
 * three and `PLAN_LEAD_CHARS`, and whether anything was left out. The whole
 * summary is one "Read more" away. A first sentence longer than the limit is
 * cut at a word with an ellipsis.
 *
 * CUT ON THE RENDERED TEXT (owner QA N18, 2026-10-04): the limit counted the
 * markdown source, and a cut inside `` `apps/…` `` left an unpaired backtick
 * that `InlineText` drew raw. A code span is now one word: the lead keeps it
 * whole or stops before it, and the limit counts what is drawn. A sentence
 * ends at `.`, `!` or `?` before a space, so `runs.py` is not two sentences.
 */
export function planLead(summary: string): { lead: string; cut: boolean } {
  const text = summary.trim().replace(/\s+/g, ' ')
  const sentences = text.split(/(?<=[.!?])\s+(?=[^\s])/)
  let lead = ''
  for (const sentence of sentences.slice(0, 3)) {
    const next = lead === '' ? sentence : `${lead} ${sentence}`
    if (lead !== '' && rendered(next).length > PLAN_LEAD_CHARS) break
    lead = next
  }
  if (rendered(lead).length > PLAN_LEAD_CHARS) {
    // Words, each code span whole inside the word it sits in.
    const words = lead.match(/(?:`[^`\n]+`|[^\s`]|`)+/g) ?? [lead]
    let kept = ''
    for (const w of words) {
      const next = kept === '' ? w : `${kept} ${w}`
      if (rendered(next).length > PLAN_LEAD_CHARS - 1) break
      kept = next
    }
    // One word longer than the whole lead: its drawn text, cut.
    if (kept === '') kept = rendered(words[0]!).slice(0, PLAN_LEAD_CHARS - 1)
    return { lead: oneEllipsis(kept), cut: true }
  }
  const cut = lead.length < text.length
  return { lead: cut ? oneEllipsis(lead) : lead, cut }
}

/**
 * ONE ELLIPSIS AT THE END OF A CUT LEAD (browser QA N18): a lead the planner
 * had already ended in `…` or `...` got a second one, "… …". Whatever trailing
 * dots or ellipsis it has become exactly one.
 */
function oneEllipsis(lead: string): string {
  return `${lead.replace(/[\s.…]+$/u, '').trimEnd()}…`
}

/**
 * THE PLAN FROM ITS SCHEMA (item 5): the lead, then the numbered steps --
 * title, step id, what it waits for, the files, tests and estimate when the
 * plan states them, and the prompt folded -- then the raw plan behind a
 * disclosure. On a phone the plan was one paragraph with no end.
 */
/** What a plan step needs to draw its live state once the run is approved; null before. */
type PlanLive = { read: RunWorkflows; rows: StepRow[]; go: (to: string) => void } | null

function PlanBody({ plan, unmet, live }: { plan: RunPlan; unmet: string[]; live: PlanLive }) {
  const { lead, cut } = planLead(plan.summary)
  // "READ MORE" BESIDE THE CUT (owner QA F, 2026-10-04): the lead ended in
  // `…` and the rest was behind "Show the full plan", under the steps.
  const [whole, setWhole] = useState(false)
  return (
    <>
      <p className="rn-summary">
        <RnText text={whole ? plan.summary.trim().replace(/\s+/g, ' ') : lead} />
        {cut && (
          <>
            {' '}
            <button type="button" className="rn-more-btn" aria-expanded={whole} onClick={() => setWhole((w) => !w)}>
              {whole ? 'Show less' : 'Read more'}
            </button>
          </>
        )}
      </p>
      <Requirements plan={plan} unmet={unmet} />
      <ol className="rn-steps">
        {plan.steps.map((s, i) => (
          <PlanStep key={s.step_id} step={s} n={i + 1} live={live} />
        ))}
      </ol>
      <details className="rn-raw">
        <summary>Show the full plan</summary>
        <p className="rn-full"><RnText text={plan.summary} /></p>
        <pre className="rn-raw-json">{JSON.stringify(plan, null, 2)}</pre>
      </details>
    </>
  )
}

/**
 * One plan step. BEFORE APPROVAL it says what it waits for ("starts at once",
 * "after api"); ONCE APPROVED (lane U14 item 2) "starts at once" is replaced
 * by the step's live state and its agent link -- a dependent step keeps
 * "after …" beside them.
 */
function PlanStep({ step, n, live }: { step: PlanStepDoc; n: number; live: PlanLive }) {
  const files = textList(step.files)
  const tests = textList(step.tests)
  const estimate = typeof step.estimate === 'string' && step.estimate.trim() !== '' ? step.estimate : null
  return (
    <li className="rn-step">
      <b className="rn-step-h">{n} · <RnText text={step.title} /></b>
      <span className="rn-step-id sb-note">
        <span className="mono">{step.step_id}</span>
        {step.depends_on !== undefined && (live === null || step.depends_on.length > 0) && (
          <span className="rn-deps">
            {' · '}
            {step.depends_on.length === 0 ? 'starts at once' : <>after <span className="mono">{step.depends_on.join(', ')}</span></>}
          </span>
        )}
      </span>
      {live !== null && (
        <PlanStepLive read={live.read} go={live.go}
          row={live.rows.find((r) => r.round === 0 && r.stepId === step.step_id) ?? null} />
      )}
      {files !== null && <StepList label="Touches" items={files} />}
      {tests !== null && <StepList label="Tests" items={tests} />}
      {estimate !== null && <span className="rn-step-meta sb-note">{estimate}</span>}
      <details className="rn-prompt-d">
        <summary>Prompt</summary>
        <p className="rn-prompt"><RnText text={step.prompt} /></p>
      </details>
    </li>
  )
}

/** A path or test id breaks after `/`, `::` or `_`, never inside a name (owner QA C). */
const STEP_JOINTS = /::|[/_]/g

/**
 * A STEP'S TOUCHES OR TESTS AS A LIST (owner QA C, 2026-10-04): one comma
 * line broke anywhere ('…_is_rec / orded_on…'). One item per line, broken
 * only at a joint, each with a copy button -- a reader pastes these.
 */
function StepList({ label, items }: { label: 'Touches' | 'Tests'; items: string[] }) {
  return (
    <div className="rn-step-l">
      <b className="rn-sub">{label}</b>
      <ul className="rn-step-list" aria-label={label}>
        {items.map((item, i) => (
          <li key={`${i}-${item}`}>
            <code title={item}>{breakAt(item, STEP_JOINTS)}</code>
            <CopyText text={item} />
          </li>
        ))}
      </ul>
    </div>
  )
}

/** A copy button for one value; says so when the browser refused the clipboard. */
function CopyText({ text }: { text: string }) {
  const [said, setSaid] = useState<'copied' | 'refused' | null>(null)
  const copy = () => {
    // `navigator.clipboard` is undefined outside a secure context and a write can be denied.
    const clip = typeof navigator === 'undefined' ? undefined : navigator.clipboard
    if (clip === undefined) {
      setSaid('refused')
      return
    }
    clip.writeText(text).then(() => setSaid('copied'), () => setSaid('refused'))
  }
  return (
    <button type="button" className="rn-copy" aria-label={`Copy ${text}`}
      title={said === 'refused' ? 'This browser refused the clipboard: select the text instead.' : said === 'copied' ? 'Copied' : `Copy ${text}`}
      onClick={copy}>
      <CIcon name={said === 'copied' ? 'check' : 'copy'} />
    </button>
  )
}

/**
 * WHAT THE ISSUE SAID WHEN THE RUN WAS CREATED (item 4): the preview's read,
 * kept on the run (`issue_read`), masked and bounded as the preview is. A run
 * created before runs kept it, or whose read failed, says which, per fact --
 * never a blank, and never a 0 for comments it did not read.
 */
function IssueReadCard({ run, now }: { run: IssueRun; now: number }) {
  const read = run.issue_read ?? null
  const failed = run.issue_read_error ?? null
  // ONE SHORT LINE FOR WHAT WAS NOT KEPT (browser QA D11): the reason was
  // printed under each of five keys, a column of the same sentence.
  const why =
    failed !== null
      ? `Not read at submission: ${failed.message}`
      : 'Not kept: this run is older than the issue snapshot.'
  return (
    <section className="rn-card rn-read" aria-label="Read from the issue">
      <h3>Read from the issue</h3>
      <ul className="ctl-facts">
        <li className="ctl-fact"><b>issue</b><IssueLink run={run} /></li>
        {read === null ? null : (
          <>
            <li className="ctl-fact"><b>title</b><span title={read.title}><RnText text={read.title} /></span></li>
            <li className="ctl-fact"><b>state</b><span>{read.state}</span></li>
            <li className="ctl-fact"><b>labels</b><span>{read.labels.length === 0 ? 'none' : breakAt(read.labels.join(' · '))}</span></li>
            <li className="ctl-fact">
              <b>body</b>
              <span>
                {read.body === '' ? 'empty' : `${read.body.length.toLocaleString('en-US')} chars`}
                {read.body_truncated && ' · cut at the preview\u2019s length'}
                {read.body_redacted && ' · masked'}
              </span>
            </li>
            <li className="ctl-fact"><b>comments</b><span>{read.comments}</span></li>
            <li className="ctl-fact">
              <b>read</b>
              <span title={read.read_at ?? undefined}>{read.read_at === null ? '—' : `${timeAgo(read.read_at, now)}, at submission`}</span>
            </li>
          </>
        )}
      </ul>
      {read === null && (
        <p className="ctl-em rn-read-none" title="Title, state, labels, body and comments are not on this run.">
          {why}
        </p>
      )}
      {read !== null && read.body !== '' && (
        <details className="rn-read-body">
          <summary>Show the body</summary>
          <p className="rn-prompt">{read.body}</p>
        </details>
      )}
    </section>
  )
}

type StepDraft = { step: PlanStepDoc; title: string; prompt: string; files: string; tests: string; estimate: string }

/**
 * The plan, editable where a plan is: the summary, the estimate and the
 * requirements, and each step's title, prompt, files, tests and estimate.
 * The mode and the overlaps are the planner's findings and are kept as
 * written. Every field the plan carries goes back, so an edit never drops one.
 */
function PlanEditor({ plan, busy, onCancel, onSave }: {
  plan: RunPlan; busy: boolean; onCancel: () => void; onSave: (plan: RunPlan) => void
}) {
  const [summary, setSummary] = useState(plan.summary)
  const [estimate, setEstimate] = useState(plan.estimate ?? '')
  const [requirements, setRequirements] = useState(linesOf(plan.requirements))
  const [steps, setSteps] = useState<StepDraft[]>(plan.steps.map((s) => ({
    step: s, title: s.title, prompt: s.prompt, files: linesOf(s.files), tests: linesOf(s.tests), estimate: s.estimate ?? '',
  })))
  const blank = summary.trim() === '' || steps.some((s) => s.title.trim() === '' || s.prompt.trim() === '')
  const set = (i: number, key: 'title' | 'prompt' | 'files' | 'tests' | 'estimate', value: string) =>
    setSteps((all) => all.map((s, j) => (j === i ? { ...s, [key]: value } : s)))

  function edited(): RunPlan {
    const out: RunPlan = {
      ...plan,
      summary,
      steps: steps.map((d) => {
        const step: PlanStepDoc = { ...d.step, title: d.title, prompt: d.prompt }
        put(step, 'files', editedList(d.step.files, d.files))
        put(step, 'tests', editedList(d.step.tests, d.tests))
        put(step, 'estimate', editedText(d.step.estimate, d.estimate))
        return step
      }),
    }
    put(out, 'estimate', editedText(plan.estimate, estimate))
    put(out, 'requirements', editedList(plan.requirements, requirements))
    return out
  }

  return (
    <form className="rn-edit" onSubmit={(e) => {
      e.preventDefault()
      if (!blank) onSave(edited())
    }}>
      <label className="rn-field">
        <b>Summary</b>
        <textarea value={summary} rows={3} onChange={(e) => setSummary(e.target.value)} />
      </label>
      <label className="rn-field">
        <b>Estimate</b>
        <input value={estimate} maxLength={200} onChange={(e) => setEstimate(e.target.value)} />
      </label>
      <label className="rn-field">
        <b>Requirements, one per line</b>
        <textarea value={requirements} rows={4} onChange={(e) => setRequirements(e.target.value)} />
      </label>
      <p className="sb-note">
        The requirements are what the review checks, and what decides <code>Closes</code> against{' '}
        <code>part of</code>. The mode and the overlaps are kept as the planner found them.
      </p>
      {steps.map((s, i) => (
        <fieldset key={s.step.step_id} className="rn-field-set">
          <legend className="mono">{i + 1} · {s.step.step_id}</legend>
          <label className="rn-field">
            <b>Title</b>
            <input value={s.title} onChange={(e) => set(i, 'title', e.target.value)} />
          </label>
          <label className="rn-field">
            <b>Prompt</b>
            <textarea value={s.prompt} rows={4} onChange={(e) => set(i, 'prompt', e.target.value)} />
          </label>
          <label className="rn-field">
            <b>Files, one per line</b>
            <textarea className="mono" value={s.files} rows={2} onChange={(e) => set(i, 'files', e.target.value)} />
          </label>
          <label className="rn-field">
            <b>Tests, one per line</b>
            <textarea value={s.tests} rows={2} onChange={(e) => set(i, 'tests', e.target.value)} />
          </label>
          <label className="rn-field">
            <b>Step estimate</b>
            <input value={s.estimate} maxLength={100} onChange={(e) => set(i, 'estimate', e.target.value)} />
          </label>
        </fieldset>
      ))}
      <p className="sb-note">Saving sends the digest of the plan you opened; an edit made by someone else since is refused.</p>
      <div className="rn-actions">
        <Button type="submit" kind="primary" disabled={busy || blank}>Save the plan</Button>
        <Button disabled={busy} onClick={onCancel}>Cancel</Button>
      </div>
    </form>
  )
}

/**
 * WHAT THE PLANNER DECIDED ABOUT ONE OVERLAP, read off its note. The plan
 * schema has no verdict field (`issueruns.PlanOverlap` is ref, kind, note);
 * the planner is told to say "what this plan does about it", and says "No
 * action: ..." when it does nothing. A note that does not open with those
 * words is counted as needing action: an unread verdict is never "no action".
 */
export function overlapVerdict(note: string): { needs: boolean; word: string; why: string } {
  if (typeof note !== 'string') return { needs: true, word: 'Needs action', why: '' }
  const m = /^\s*no action\b\s*(?:[:.;,]|--?|—|–)?\s*/i.exec(note)
  if (m !== null) return { needs: false, word: 'No action', why: note.slice(m[0].length) || note }
  return { needs: true, word: 'Needs action', why: note }
}

/**
 * ONE OVERLAP AS SERVED, read as `PlanOverlap`. A plan stored before overlaps
 * were objects carries each as a bare sentence; the planner wrote it either
 * way, so it is drawn as its note with no ref, never dropped and never allowed
 * to throw while the page renders.
 */
function overlapRead(o: unknown): PlanOverlap {
  if (typeof o === 'string') return { ref: '', kind: 'issue', note: o }
  const r = (o ?? {}) as Partial<Record<keyof PlanOverlap, unknown>>
  return {
    ref: typeof r.ref === 'string' ? r.ref : '',
    kind: r.kind === 'pull_request' ? 'pull_request' : 'issue',
    note: typeof r.note === 'string' ? r.note : '',
  }
}

/**
 * THE OVERLAPS THE PLANNER FOUND, above the plan. Each is a link to the issue
 * or pull request in flight, with its kind and the planner's verdict, and
 * opens to the planner's reasoning. A plan that has no `overlaps` field was
 * written before the planner read open work, and says so; an empty list is a
 * finding, and names what was read.
 *
 * AMBER ONLY WHEN ONE NEEDS ACTION (owner QA E, 2026-10-04): five long
 * paragraphs that each said "no action" sat in a warn-edged card that read as
 * a warning. The card is neutral until an overlap needs action, and its
 * heading counts both: "5 checked · 0 need action".
 */
function Overlaps({ plan, openWork, folded }: { plan: RunPlan; openWork: OpenWork | null; folded: boolean }) {
  const overlaps = plan.overlaps === undefined || plan.overlaps === null ? plan.overlaps : plan.overlaps.map(overlapRead)
  const found = overlaps !== undefined && overlaps !== null && overlaps.length > 0
  const verdicts = found ? overlaps!.map((o) => overlapVerdict(o.note)) : []
  const needing = verdicts.filter((v) => v.needs).length
  const title = found
    ? `Overlaps the planner found · ${overlaps!.length} checked · ${needing} ${needing === 1 ? 'needs' : 'need'} action`
    : 'Overlaps the planner found'
  const body = (
    <>
      {overlaps === undefined || overlaps === null ? (
        <p className="sb-note">
          This plan does not say: it was written without the planner&rsquo;s read of the repository&rsquo;s open issues
          and pull requests, so nothing here rules overlaps out.
        </p>
      ) : overlaps.length === 0 ? (
        <p className="sb-note">
          None found{openWork === null ? '.' : <>
            {' '}in {pluralise(openWork.issues.length, 'open issue')} and{' '}
            {pluralise(openWork.pull_requests.length, 'open pull request')} read when the run was created
            {openWork.issues_truncated || openWork.pull_requests_truncated ? ' (the list was cut, so not every one was read)' : ''}.
          </>}
        </p>
      ) : (
        <ul className="rn-overlap-list">
          {overlaps.map((o, i) => {
            const url = overlapUrl(o)
            const v = verdicts[i]!
            return (
              <li key={`${o.ref}-${i}`}>
                <details className={v.needs ? 'rn-overlap is-needs' : 'rn-overlap'}>
                  <summary>
                    {o.ref === ''
                      ? null
                      : url === null
                      ? <span className="mono">{o.ref}</span>
                      : <a href={url} target="_blank" rel="noreferrer" className="mono">{o.ref}</a>}
                    {o.ref === '' ? null : <Chip>{o.kind === 'pull_request' ? 'pull request' : 'issue'}</Chip>}
                    <span className="rn-overlap-v" title="Read from the planner's note">{v.word}</span>
                  </summary>
                  <p className="rn-overlap-note"><RnText text={v.why} /></p>
                </details>
              </li>
            )
          })}
        </ul>
      )}
    </>
  )
  const className = `rn-overlaps${needing > 0 ? ' is-found' : ''}`
  // ONE LINE ONCE THE PLAN IS APPROVED (lane U14 item 5): it was read before
  // approval; after it, it sat ~1,000px above nothing a reader needed.
  if (folded) {
    return (
      <Card level={3} className={className}>
        <Fold folded head={<h3>{title}</h3>} line={null}>{body}</Fold>
      </Card>
    )
  }
  return <Card level={3} className={className} title={title}>{body}</Card>
}

/**
 * A CARD FOLDED TO ONE LINE (lane U14 item 5): its heading and a short line
 * as a disclosure, the body under it. Unfolded, the heading and body as they
 * were.
 */
function Fold({ folded, head, line, children }: { folded: boolean; head: ReactNode; line: ReactNode; children: ReactNode }) {
  if (!folded) return <>{head}{children}</>
  return (
    <details className="rn-fold">
      <summary>{head}{line}</summary>
      <div className="rn-fold-b">{children}</div>
    </details>
  )
}

/** Whether the plan has been approved: it is no longer the page's question. */
function planApproved(run: IssueRun): boolean {
  return run.approved_by !== null || run.workflow_id !== null
}

/** The issue's requirements as the planner listed them; one the review left open is marked. */
function Requirements({ plan, unmet }: { plan: RunPlan; unmet: string[] }) {
  const reqs = plan.requirements
  if (reqs === undefined || reqs === null || reqs.length === 0) {
    return <p className="sb-note">The plan lists no requirements, so a review cannot confirm every one: the pull request says part of.</p>
  }
  const open = new Set(unmet)
  return (
    <div className="rn-reqs-wrap">
      <b className="rn-sub">Requirements · {reqs.length}</b>
      <ol className="rn-reqs">
        {reqs.map((r, i) => (
          <li key={`${i}-${r}`} className={open.has(r) ? 'is-unmet' : undefined}>
            <RnText text={r} />{open.has(r) && <i className="sbf-bad"> · not confirmed by the review</i>}
          </li>
        ))}
      </ol>
    </div>
  )
}

/** A comment the write-back posted on the issue, or why there is none. */
function CommentFact({ label, name, url, absent }: { label: string; name: string; url: string | null; absent: string }) {
  return (
    <li className={url === null ? 'ctl-fact is-absent' : 'ctl-fact'}>
      <b>{label}</b>
      {url === null ? <i className="ctl-em" title={absent}>{absent}</i> : <a href={url} target="_blank" rel="noreferrer" title={url}>{name}</a>}
    </li>
  )
}

/**
 * THE PULL REQUEST THE RUN'S WORKFLOW OPENED, READ FROM ITS INTEGRATOR STEP
 * (browser QA N15, 2026-10-04). A run created before the CI loop records no
 * `pull_request` of its own, and its page said "none yet · the workflow opens
 * it" under a Done run whose workflow had opened PR #545. When the run has
 * none, the workflow is read once and its integrator's
 * `result_summary.git.pull_request` is the answer (`workflowPullRequest`, the
 * same reading the Workflows screen draws). Null until read, or when the run
 * names its own PR or no workflow.
 */
/**
 * What the workflow read said about its pull request. `none` when nothing was
 * asked (the run names its own PR, or it has no workflow); `reading` and
 * `error` are unknowns, and an unknown is never drawn as "none".
 */
type WfPrRead =
  | { kind: 'none' }
  | { kind: 'reading' }
  | { kind: 'error'; why: string }
  | { kind: 'read'; pr: WorkflowPullRequest | null }

/**
 * The PR the integrator opened, from the run's workflow read (`useRunWorkflows`,
 * which the Steps card shares), asked only when the run names no PR itself.
 */
function wfPrOf(run: IssueRun, read: RunWorkflows): WfPrRead {
  if ((run.pull_request ?? null) !== null || run.workflow_id === null) return { kind: 'none' }
  const main = read.loads?.[0]
  if (main === undefined || main.wf !== run.workflow_id) return { kind: 'reading' }
  if (main.data === null) return { kind: 'error', why: main.error ?? 'the read did not finish' }
  const pr = workflowPullRequest(main.data.workflow, new Map(main.data.tasks.map((t) => [t.id, t])))
  return { kind: 'read', pr }
}

/** The PR the workflow's integrator opened, once read; null otherwise. */
function prOf(read: WfPrRead): WorkflowPullRequest | null {
  return read.kind === 'read' ? read.pr : null
}

/** The run's pull request: a link once the workflow opened one. */
function PullRequestFact({ run, wfPr: read }: { run: IssueRun; wfPr: WfPrRead }) {
  const pr = run.pull_request ?? null
  const url = githubLink(pr?.url)
  const label = pr?.number === null || pr?.number === undefined ? 'pull request' : `#${pr.number}`
  const wfPr = prOf(read)
  if (pr === null && wfPr !== null) {
    const title = `#${wfPr.number}, read from the workflow's integrator result (step ${wfPr.stepId}); the run itself records no pull request.`
    return (
      <li className="ctl-fact">
        <b>Pull request</b>
        {wfPr.href === null
          ? <span className="mono" title={title}>#{wfPr.number}</span>
          : <a href={wfPr.href} target="_blank" rel="noreferrer" className="mono" title={title}>#{wfPr.number}</a>}
      </li>
    )
  }
  // AN UNREAD WORKFLOW IS AN UNKNOWN, NOT "NONE" (review of U11b N15): while
  // the workflow is being read, or after its read failed, whether it opened a
  // pull request is not known.
  if (pr === null && (read.kind === 'reading' || read.kind === 'error')) {
    const why = read.kind === 'reading'
      ? 'The run records no pull request; its workflow is still being read for the one its integrator opened.'
      : `The run records no pull request, and its workflow could not be read for the one its integrator opened: ${read.why}`
    return (
      <li className="ctl-fact is-absent">
        <b>Pull request</b>
        <Dash why={why} />
      </li>
    )
  }
  const none = run.terminal ? 'none recorded' : 'none yet · the workflow opens it'
  return (
    <li className={pr === null ? 'ctl-fact is-absent' : 'ctl-fact'}>
      <b>Pull request</b>
      {pr === null
        ? <i className="ctl-em" title={run.terminal ? (run.workflow_id === null ? 'The run started no workflow, so no pull request was opened.' : 'Neither the run nor its workflow recorded a pull request.') : 'None yet: the workflow opens it.'}>{none}</i>
        : url === null ? <span className="mono" title={label}>{label}</span>
          : <a href={url} target="_blank" rel="noreferrer" className="mono" title={url}>{label}</a>}
    </li>
  )
}

/** Whether the run has reached the pull request and its CI, so the CI card has something to say. */
function hasCi(run: IssueRun): boolean {
  return (run.pull_request ?? null) !== null || run.state === 'CHECKING' || run.state === 'FIXING'
    || (run.ci_fix_round ?? 0) > 0 || Boolean(run.green_sha) || Boolean(run.failure_excerpt)
    || (run.requirements_met ?? null) !== null
}

/**
 * THE PULL REQUEST AND ITS CI (the CI loop, `issueci`): the checks at the
 * head, the fix round of the cap, the green sha a DONE run is pinned to, the
 * keyword the requirements finding chose, and a FAILED run's excerpt -- the
 * server's redacted text, drawn as text.
 */
function CiCard({ run, go, wfPr: read }: { run: IssueRun; go: (to: string) => void; wfPr: WfPrRead }) {
  if (!hasCi(run)) {
    // A FINISHED RUN DRAWS THE CARD ANYWAY (browser QA N15): no card at all
    // read as "nothing to say", on a run whose workflow opened a PR. The CI
    // loop records checks on the run; a run it never read says so. "Created
    // before the CI loop" is said ONLY where a pull request exists to have
    // been read (review of U11b N15): a rejected run, a failed planner or a
    // run cancelled before its workflow opened nothing for CI to check.
    if (!run.terminal) return null
    const wfPr = prOf(read)
    let note: string | null
    if (wfPr !== null) {
      note = `No CI recorded for this run: the CI loop never read its pull request (#${wfPr.number}, opened by the workflow), which is how a run created before the CI loop ends.`
    } else if (read.kind === 'reading') {
      note = null
    } else if (read.kind === 'error') {
      note = `No CI recorded for this run, and its workflow could not be read to say whether it opened a pull request: ${read.why}`
    } else if (run.workflow_id === null) {
      const why = run.state === 'REJECTED' ? 'the plan was rejected'
        : run.state === 'CANCELLED' ? 'the run was cancelled'
          : run.state === 'FAILED' ? 'the run failed'
            : 'the run ended'
      note = `No pull request was opened, so there is no CI to read: ${why} before any workflow started.`
    } else {
      note = 'No pull request was opened: the workflow ended without one, so there is no CI to read.'
    }
    if (note === null) return null
    return (
      <Card level={3} className="rn-ci" title="Pull request and checks">
        <p className="sb-note">{note}</p>
      </Card>
    )
  }
  const pr = run.pull_request ?? null
  const round = run.ci_fix_round ?? 0
  const fixes = run.ci_fix_workflows ?? []
  const n = run.issue.number
  const prUrl = githubLink(pr?.url)
  const head = pr?.head_sha ?? null
  // THE FAILED CHECKS BY NAME (lane U14 item 5): the run serves no per-check
  // list, but its failure excerpt names them, at the sha it read them at. A
  // reading from before the head moved is said as that, never as the head's.
  const red = redReading(run.failure_excerpt)
  const redAtHead = red !== null && head !== null && head.startsWith(red.sha)
  const notServed = (what: string) =>
    `${what} not served: the run records one aggregate reading of the required checks (pull_request.checks), not each check.`
  const issueUrl = githubLink(run.issue.url)
  return (
    <Card level={3} className={lead(run) ? 'rn-ci is-lead' : 'rn-ci'} title={pr === null || pr.number === null ? 'Pull request and checks' : (
      <>
        Pull request{' '}
        {prUrl === null ? <span className="mono">#{pr.number}</span>
          : <a href={prUrl} target="_blank" rel="noreferrer" className="mono" title={prUrl}>#{pr.number}</a>}
        {' · '}
        <Dash why="The pull request's title is not served: the run records its number, link, head and checks, and no pull_request.title." />
      </>
    )}>
      <ul className="ctl-facts rn-ci-facts">
        <li className="ctl-fact">
          <b>checks</b>
          {pr?.checks
            ? <span>{pr.checks}{head ? <> at <code title={head}>{shortSha(head)}</code><CopyText text={head} /></> : null}</span>
            : <i className="ctl-em">not read yet</i>}
        </li>
        <li className="ctl-fact rn-ci-counts">
          <b>by check</b>
          <span>
            passed <Dash why={notServed('How many passed is')} /> · pending <Dash why={notServed('How many are pending is')} />
            {' · '}failed {redAtHead && pr?.checks === 'red' ? red!.names.length : <Dash why={notServed('How many failed at this head is')} />}
            {' · '}skipped <Dash why={notServed('How many were skipped is')} />
          </span>
        </li>
        {red !== null && red.names.length > 0 && (
          <li className="ctl-fact rn-ci-failed">
            <b>{redAtHead ? 'failed' : `red at ${red.sha}, before the head moved`}</b>
            <ul className="rn-failed-checks">
              {red.names.map((name, i) => (
                <li key={`${i}-${name}`}>
                  <span className="rn-failed-check" title="Each check's own run link is not served; the checks page lists them.">{name}</span>
                </li>
              ))}
            </ul>
          </li>
        )}
        {prUrl !== null && (
          <li className="ctl-fact">
            <b>CI</b>
            <a href={`${prUrl}/checks`} target="_blank" rel="noreferrer">checks on GitHub ↗</a>
          </li>
        )}
        <li className="ctl-fact">
          <b>fix rounds</b>
          <span>
            {round === 0 ? `none spent · up to ${run.fix_rounds}` : `fix round ${round} of ${run.fix_rounds}`}
            {fixes.map((wf) => (
              <span key={wf}>
                {' · '}
                <InApp go={go} to={`work/workflows?${new URLSearchParams({ wf }).toString()}`}
                  href={`/workflows/${encodeURIComponent(wf)}`}>{wf}</InApp>
              </span>
            ))}
          </span>
        </li>
        <li className={run.green_sha ? 'ctl-fact' : 'ctl-fact is-absent'}>
          <b>green at</b>
          {/* SHORT, LIKE CHECKS (item 6): the whole sha is its title and its copy. */}
          {run.green_sha
            ? <span><code title={run.green_sha}>{shortSha(run.green_sha)}</code><CopyText text={run.green_sha} /></span>
            : <i className="ctl-em">not green yet</i>}
        </li>
        {pr !== null && <MergeFact run={run} />}
        <li className="ctl-fact">
          <b>issue</b>
          <span>
            {issueUrl === null ? <span className="mono">#{n}</span>
              : <a href={issueUrl} target="_blank" rel="noreferrer" className="mono" title={issueUrl}>#{n}</a>}
            {' · '}
            <i className="ctl-em" title="The issue's state now is not served: the run read the issue once, at submission (issue_read).">
              state not served{run.issue_read?.state ? ` · ${run.issue_read.state} when the run was created` : ''}
            </i>
          </span>
        </li>
        <li className="ctl-fact">
          <b>keyword</b>
          {run.requirements_met === true ? <code>Closes #{n}</code>
            : run.requirements_met === false ? <code>part of #{n}</code>
              : <i className="ctl-em">not decided · the review has not reported on the requirements</i>}
        </li>
      </ul>
      {run.requirements_met === false && (
        <p className="sb-note">
          The review left {pluralise((run.requirements_unmet ?? []).length, 'requirement')} open
          {run.requirements_note ? <>: {run.requirements_note}</> : '.'}
        </p>
      )}
      {run.failure_excerpt && (
        <div className="rn-excerpt">
          <CodeBlock title="Failing checks" lang="redacted by the API" text={run.failure_excerpt} />
        </div>
      )}
    </Card>
  )
}

/** Whether the pull request is the run's subject now: CHECKING, FIXING or DONE. */
function lead(run: IssueRun): boolean {
  return run.state === 'CHECKING' || run.state === 'FIXING' || run.state === 'DONE'
}

/**
 * WHETHER THE PULL REQUEST MERGED (lane U14 item 6). The CI loop reads it
 * (`pull_request.merged`) and ends the run DONE on a merge OR on green, but
 * `IssueRun.to_api` does not serve it, and no merger or merge time is kept.
 * So a DONE run says "merge not reported"; while CHECKING or FIXING the last
 * read found it open, since a merged PR ends the run and a closed one fails it.
 */
function MergeFact({ run }: { run: IssueRun }) {
  const why = 'Not served: the CI loop reads whether the pull request merged, but the run does not serve pull_request.merged, and records no merger or merge time.'
  return (
    <li className="ctl-fact is-absent">
      <b>merge</b>
      {run.state === 'CHECKING' || run.state === 'FIXING'
        ? <i className="ctl-em" title="A merged pull request ends the run DONE and a closed one fails it, so a CHECKING or FIXING run's was open at its last read.">open · not merged at the last read</i>
        : <i className="ctl-em" title={why}>merge not reported{run.green_sha ? ' · green' : ''}</i>}
    </li>
  )
}

function RejectForm({ busy, onCancel, onReject }: { busy: boolean; onCancel: () => void; onReject: (reason: string) => void }) {
  const [reason, setReason] = useState('')
  return (
    <form className="rn-reject" onSubmit={(e) => {
      e.preventDefault()
      onReject(reason)
    }}>
      <label className="rn-field">
        <b>Why (optional)</b>
        <textarea value={reason} rows={2} maxLength={1024} onChange={(e) => setReason(e.target.value)} />
      </label>
      <div className="rn-actions">
        <Button type="submit" disabled={busy}>Reject this plan</Button>
        <Button disabled={busy} onClick={onCancel}>Keep it</Button>
      </div>
    </form>
  )
}
