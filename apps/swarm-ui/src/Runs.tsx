import { Fragment, useEffect, useRef, useState } from 'react'
import { approvePlan, editPlan, loadRun, loadRuns, rejectPlan } from './api'
import type { ApiError, Result } from './fetch'
import { runAddress } from './IssueSubmit'
import type { MarkHue, MarkName } from './marks'
import { Button, NamedMark, WarnMark } from './components'
import { FailedPanel, Screen, timeAgo } from './Shell'
import type { IssueRun, IssueRunPage, IssueRunState, PlanStepDoc, RunPlan } from './types'
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
 * WHAT THE RUN DOCUMENT DOES NOT SERVE IS NAMED, NOT HIDDEN. `IssueRun.to_api`
 * has no pull request, no plan or status comment on the issue, no overlaps
 * and no cost; each is a dash with that reason. A workflow not yet created is
 * "none yet", because the run does serve `workflow_id`, and null is a fact.
 *
 * THE PAGE LEADS WITH THE ISSUE (lane U9, owner 2026-10-03). Its title is the
 * issue's title as the run read it at submission (`issue_read`), its meta
 * `owner/repo#N · run_… · created by …`; the plan is drawn from its schema
 * -- a short lead, numbered steps with their prompts folded, the raw plan
 * behind a disclosure -- rather than as one long paragraph.
 *
 * Classes are `rn-` so a later pass can swap them for lane U0's components.
 */

/** A run that has not finished is re-read on this cadence: each read advances it. */
export const RUN_POLL_MS = 15_000

/** `issueruns.AUTO_APPROVER`: who `approved_by` names on an automatic approval. */
const AUTO_APPROVER = 'auto-approval'

/** The brand state marks (marks.tsx) for a run's states. */
const RUN_MARK: Readonly<Record<IssueRunState, { mark: MarkName; hue: MarkHue }>> = {
  // A planner task is working: it can hold capacity, as any task.
  PLANNING: { mark: 'running', hue: 'live' },
  // Waiting for a person, holding nothing (invariant 1): a park.
  PLANNED: { mark: 'parked', hue: 'park' },
  APPROVED: { mark: 'starting', hue: 'live' },
  RUNNING: { mark: 'running', hue: 'live' },
  DONE: { mark: 'succeeded', hue: 'neu' },
  FAILED: { mark: 'failed', hue: 'bad' },
  REJECTED: { mark: 'cancelled', hue: 'neu' },
  CANCELLED: { mark: 'cancelled', hue: 'neu' },
}

/** A run's state, as the API names it, with its mark. */
export function RunStateMark({ state }: { state: IssueRunState }) {
  const { mark, hue } = RUN_MARK[state] ?? { mark: 'queued', hue: 'neu' }
  return <NamedMark mark={mark} hue={hue} word={state} />
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

/** A link inside the app: an href for a new tab, `go` for a plain click. */
function InApp({ to, href, go, children }: { to: string; href: string; go: (to: string) => void; children: string }) {
  return (
    <a href={href} className="mono" onClick={(e) => {
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
    return (
      <>
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
          summary={(d) => `${d.run.issue.ref} · ${d.run.id} · created by ${d.run.created_by || '—'}`}
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
      </>
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
        <table className="rn-table">
          <thead>
            <tr>
              <th scope="col">Run</th>
              <th scope="col">Issue</th>
              <th scope="col">State</th>
              <th scope="col">Plan approval</th>
              <th scope="col">Workflow</th>
              <th scope="col">Created</th>
              <th scope="col">By</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} className="rn-row">
                <td data-label="Run"><InApp go={go} to={runAddress(r.id)} href={`/runs/${encodeURIComponent(r.id)}`}>{r.id}</InApp></td>
                <td data-label="Issue"><span className="mono">{r.issue.ref}</span></td>
                <td data-label="State"><RunStateMark state={r.state} /></td>
                <td data-label="Plan approval">{r.plan_approval}</td>
                <td data-label="Workflow">
                  {r.workflow_id === null ? <i className="ctl-em">none yet</i> : <span className="mono">{r.workflow_id}</span>}
                </td>
                <td data-label="Created" title={r.created_at}>{timeAgo(r.created_at, now)}</td>
                <td data-label="By">{r.created_by || <i className="ctl-em">&mdash; not recorded</i>}</td>
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
    case 'DONE':
      return 'The workflow succeeded.'
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
  const [open, setOpen] = useState<{ kind: 'none' } | { kind: 'edit'; digest: string } | { kind: 'reject' }>(
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
        {run.state === 'REJECTED' && (
          <p className="sb-note">
            Rejected by {run.rejected_by ?? '—'}
            {run.rejection_reason ? `: ${run.rejection_reason}` : ' · no reason given'}
          </p>
        )}

        {run.state === 'PLANNED' && run.plan_approval === 'required' && (
          <div className="rn-wait">
            <b>Waiting for a person.</b> Anyone in tenant <code>{run.tenant_id}</code> may approve, edit or reject.
            Approving carries this plan&rsquo;s digest, so a plan edited by someone else since you opened it is refused
            rather than run.
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

        <section className="rn-plan" aria-label="The plan">
          <h3>
            The plan
            {digest !== null && <> · <code className="rn-digest" title={digest}>{shortDigest(digest)}</code></>}
          </h3>
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
                {' · '}{run.plan_shape ?? `${run.plan.steps.length === 1 ? '1 step' : `${run.plan.steps.length} steps`}, then a review and a fix`}
                {' '}gated on its verdict · every step runs as claude-code
              </p>
              <PlanBody plan={run.plan} />
            </>
          )}
        </section>

        <section className="rn-overlaps" aria-label="Overlaps">
          <h3>Overlaps the planner found</h3>
          {textList(run.plan?.overlaps) !== null ? (
            <ul className="rn-list-items">
              {textList(run.plan?.overlaps)!.map((o, i) => <li key={i}>{o}</li>)}
            </ul>
          ) : (
            <p className="sb-note">
              <i className="ctl-em">&mdash;</i> not served: a plan holds a summary and steps only, so the run carries no
              overlapping pull requests or issues.
            </p>
          )}
        </section>
        {textList(run.plan?.risks) !== null && (
          <section className="rn-risks" aria-label="Risks">
            <h3>Risks the planner named</h3>
            <ul className="rn-list-items">
              {textList(run.plan?.risks)!.map((r, i) => <li key={i}>{r}</li>)}
            </ul>
          </section>
        )}

        {canAct && open.kind !== 'edit' && (
          <div className="rn-actions">
            {open.kind === 'reject' ? (
              <RejectForm busy={busy} onCancel={close}
                onReject={(reason) => void act('reject', () => rejectPlan(run.id, digest, reason))} />
            ) : (
              <>
                <Button kind="primary" disabled={busy}
                  onClick={() => void act('approve', () => approvePlan(run.id, digest!))}>
                  {acting.kind === 'busy' && acting.what === 'approve' ? 'Approving…' : 'Approve and run'}
                </Button>
                <Button disabled={busy} onClick={() => setOpen({ kind: 'edit', digest: digest! })}>Edit plan</Button>
                <Button disabled={busy} onClick={() => setOpen({ kind: 'reject' })}>Reject</Button>
              </>
            )}
          </div>
        )}
        {acting.kind === 'failed' && <FailedPanel error={acting.error} onRetry={() => setActing({ kind: 'idle' })} />}

        <section className="rn-history" aria-label="History">
          <h3>Progress</h3>
          <ol>
            {run.history.map((h, i) => (
              <li key={`${h.to}-${i}`}>
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
            <li className="ctl-fact is-absent">
              <b>Plan comment</b>
              <i className="ctl-em">&mdash; {WRITE_BACK}</i>
            </li>
            <li className="ctl-fact is-absent">
              <b>Status comment</b>
              <i className="ctl-em">&mdash; {WRITE_BACK}</i>
            </li>
            <li className="ctl-fact">
              <b>Planner</b>
              {run.planner_task_id === ''
                ? <i className="ctl-em">&mdash; not recorded</i>
                : <InApp go={go} to={`work/task/${encodeURIComponent(run.planner_task_id)}`}
                  href={`/agents/recent/${encodeURIComponent(run.planner_task_id)}`}>{run.planner_task_id}</InApp>}
            </li>
            <li className={run.workflow_id === null ? 'ctl-fact is-absent' : 'ctl-fact'}>
              <b>Workflow</b>
              {run.workflow_id === null
                ? <i className="ctl-em">none yet · created on approval</i>
                : <InApp go={go} to={`work/workflows?${new URLSearchParams({ wf: run.workflow_id }).toString()}`}
                  href={`/workflows/${encodeURIComponent(run.workflow_id)}`}>{run.workflow_id}</InApp>}
            </li>
            <li className="ctl-fact is-absent">
              <b>Pull request</b>
              <i className="ctl-em">&mdash; {WRITE_BACK}</i>
            </li>
          </ul>
        </section>
        <IssueReadCard run={run} now={now} />
        <section className="rn-card" aria-label="Chosen at submission">
          <h3>Chosen at submission</h3>
          <ul className="ctl-facts">
            <li className="ctl-fact"><b>plan approval</b>{run.plan_approval}</li>
            <li className="ctl-fact"><b>auto-merge</b>{run.auto_merge ? 'on' : 'off'}</li>
            <li className="ctl-fact"><b>fix rounds</b>up to {run.fix_rounds}</li>
            <li className="ctl-fact"><b>by</b>{run.created_by || <i className="ctl-em">&mdash; not recorded</i>}</li>
            <li className="ctl-fact is-absent"><b>cost so far</b><i className="ctl-em">&mdash; not served per run</i></li>
          </ul>
        </section>
      </aside>
    </div>
  )
}

/** Why the plan comment, the status comment and the pull request are dashes (#454). */
const WRITE_BACK = 'GitHub write-back is not built yet (#454)'

/**
 * `owner/repo#N`, linked, on ONE line (item 6): it wrapped at the owner's
 * hyphen ('bogdan- / alexandrescu/SwarmCloud#454'). Cut with an ellipsis where
 * it does not fit, its whole text in the title.
 */
function IssueLink({ run }: { run: IssueRun }) {
  return (
    <a href={run.issue_read?.url || run.issue.url} target="_blank" rel="noreferrer" className="mono rn-ref" title={run.issue.ref}>
      {run.issue.ref}
    </a>
  )
}

/** A plan's optional list (`overlaps`, `risks`, a step's `files`): its strings, or null when it has none. */
export function textList(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null
  const items = value.filter((v): v is string => typeof v === 'string' && v.trim() !== '')
  return items.length === 0 ? null : items
}

/** The most a plan's lead may run to: two or three lines in the plan's column. */
export const PLAN_LEAD_CHARS = 240

/**
 * The plan's summary cut to its lead (item 5): its first sentences, up to
 * three and `PLAN_LEAD_CHARS`, and whether anything was left out. The whole
 * summary is under "Show the full plan". A first sentence longer than the
 * limit is cut at a word with an ellipsis.
 */
export function planLead(summary: string): { lead: string; cut: boolean } {
  const text = summary.trim().replace(/\s+/g, ' ')
  const sentences = text.match(/[^.!?]+(?:[.!?]+|$)\s*/g) ?? [text]
  let lead = ''
  for (const sentence of sentences.slice(0, 3)) {
    if (lead !== '' && (lead + sentence).trim().length > PLAN_LEAD_CHARS) break
    lead += sentence
  }
  lead = lead.trim()
  if (lead.length > PLAN_LEAD_CHARS) {
    const at = lead.lastIndexOf(' ', PLAN_LEAD_CHARS - 1)
    return { lead: `${lead.slice(0, at > 0 ? at : PLAN_LEAD_CHARS - 1)}…`, cut: true }
  }
  return { lead, cut: lead.length < text.length }
}

/**
 * THE PLAN FROM ITS SCHEMA (item 5): the lead, then the numbered steps --
 * title, step id, what it waits for, the files, tests and estimate when the
 * plan states them, and the prompt folded -- then the raw plan behind a
 * disclosure. On a phone the plan was one paragraph with no end.
 */
function PlanBody({ plan }: { plan: RunPlan }) {
  const { lead, cut } = planLead(plan.summary)
  return (
    <>
      <p className="rn-summary">
        {lead}
        {cut && !lead.endsWith('…') ? ' …' : ''}
      </p>
      <ol className="rn-steps">
        {plan.steps.map((s, i) => (
          <PlanStep key={s.step_id} step={s} n={i + 1} />
        ))}
      </ol>
      <details className="rn-raw">
        <summary>Show the full plan</summary>
        <p className="rn-full">{plan.summary}</p>
        <pre className="rn-raw-json">{JSON.stringify(plan, null, 2)}</pre>
      </details>
    </>
  )
}

function PlanStep({ step, n }: { step: PlanStepDoc; n: number }) {
  const files = textList(step.files)
  const tests = textList(step.tests)
  const estimate = typeof step.estimate === 'string' && step.estimate.trim() !== '' ? step.estimate : null
  return (
    <li className="rn-step">
      <b className="rn-step-h">{n} · {step.title}</b>
      <span className="rn-step-id sb-note">
        <span className="mono">{step.step_id}</span>
        {step.depends_on !== undefined && (
          <span className="rn-deps">
            {' · '}
            {step.depends_on.length === 0 ? 'starts at once' : <>after <span className="mono">{step.depends_on.join(', ')}</span></>}
          </span>
        )}
      </span>
      {(files !== null || tests !== null || estimate !== null) && (
        <span className="rn-step-meta sb-note">
          {files !== null && <span>touches {files.map((f, i) => <Fragment key={f}>{i > 0 && ', '}<code>{f}</code></Fragment>)}</span>}
          {tests !== null && <span>tests {tests.map((t, i) => <Fragment key={t}>{i > 0 && ', '}<code>{t}</code></Fragment>)}</span>}
          {estimate !== null && <span>{estimate}</span>}
        </span>
      )}
      <details className="rn-prompt-d">
        <summary>Prompt</summary>
        <p className="rn-prompt">{step.prompt}</p>
      </details>
    </li>
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
  const why =
    failed !== null
      ? `not read at submission: ${failed.message}`
      : 'not kept: this run was created before runs kept what the issue said'
  const absent = (key: string) => (
    <li key={key} className="ctl-fact is-absent"><b>{key}</b><i className="ctl-em">&mdash; {why}</i></li>
  )
  return (
    <section className="rn-card rn-read" aria-label="Read from the issue">
      <h3>Read from the issue</h3>
      <ul className="ctl-facts">
        <li className="ctl-fact"><b>issue</b><IssueLink run={run} /></li>
        {read === null ? (
          ['title', 'state', 'labels', 'body', 'comments'].map(absent)
        ) : (
          <>
            <li className="ctl-fact"><b>title</b><span className="rn-read-v">{read.title}</span></li>
            <li className="ctl-fact"><b>state</b><span>{read.state}</span></li>
            <li className="ctl-fact"><b>labels</b><span className="rn-read-v">{read.labels.length === 0 ? 'none' : read.labels.join(' · ')}</span></li>
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
      {read !== null && read.body !== '' && (
        <details className="rn-read-body">
          <summary>Show the body</summary>
          <p className="rn-prompt">{read.body}</p>
        </details>
      )}
    </section>
  )
}

/** The plan, editable where a plan step is editable: the summary, and each step's title and prompt. */
function PlanEditor({ plan, busy, onCancel, onSave }: {
  plan: RunPlan; busy: boolean; onCancel: () => void; onSave: (plan: RunPlan) => void
}) {
  const [summary, setSummary] = useState(plan.summary)
  // `depends_on` is carried through untouched: dropping it would turn a staged plan back into a chain on save.
  const [steps, setSteps] = useState<PlanStepDoc[]>(plan.steps.map((s) => ({
    step_id: s.step_id, title: s.title, prompt: s.prompt,
    ...(s.depends_on !== undefined ? { depends_on: [...s.depends_on] } : {}),
  })))
  const blank = summary.trim() === '' || steps.some((s) => s.title.trim() === '' || s.prompt.trim() === '')
  const set = (i: number, key: 'title' | 'prompt', value: string) =>
    setSteps((all) => all.map((s, j) => (j === i ? { ...s, [key]: value } : s)))
  return (
    <form className="rn-edit" onSubmit={(e) => {
      e.preventDefault()
      if (!blank) onSave({ summary, steps })
    }}>
      <label className="rn-field">
        <b>Summary</b>
        <textarea value={summary} rows={3} onChange={(e) => setSummary(e.target.value)} />
      </label>
      {steps.map((s, i) => (
        <fieldset key={s.step_id} className="rn-field-set">
          <legend className="mono">{i + 1} · {s.step_id}</legend>
          <label className="rn-field">
            <b>Title</b>
            <input value={s.title} onChange={(e) => set(i, 'title', e.target.value)} />
          </label>
          <label className="rn-field">
            <b>Prompt</b>
            <textarea value={s.prompt} rows={4} onChange={(e) => set(i, 'prompt', e.target.value)} />
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
