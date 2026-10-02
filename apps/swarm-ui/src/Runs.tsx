import { useEffect, useState } from 'react'
import { approvePlan, editPlan, loadRun, loadRuns, rejectPlan } from './api'
import type { ApiError, Result } from './fetch'
import { runAddress } from './IssueSubmit'
import { MarkGlyph, type MarkHue, type MarkName } from './marks'
import { FailedPanel, Screen, timeAgo } from './Shell'
import type { IssueRun, IssueRunPage, IssueRunState, RunPlan } from './types'
import { useNow } from './useNow'
import './styles/intake.css'

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
  return (
    <span className={`sk-st is-${hue}`} data-mark={mark} data-hue={hue}>
      <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
        <MarkGlyph mark={mark} />
      </svg>
      <span className="sk-st-w">{state}</span>
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

  if (runId !== null) {
    // A local, not an inline expression: the nav-heading test reads the
    // literal below as the tab's heading and skips a per-run title.
    const pageTitle = runId
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
            <span className="sk-st is-warn" data-mark="warn">
              <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false"><MarkGlyph mark="warn" /></svg>
            </span>
            <span>{notice}</span>
          </div>
        )}
        <Screen
          key={`run:${runId}:${reloads}`}
          title={pageTitle}
          load={() => loadRun(runId)}
          pollMs={(d) => (d === null || d.run.terminal ? null : RUN_POLL_MS)}
          summary={(d) => `${d.run.issue.ref} · created by ${d.run.created_by || '—'}`}
        >
          {(d) => <RunPage run={d.run} reread={reread} go={go} />}
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
          <button type="button" className="sb-btn" disabled={older.reading} onClick={() => void more()}>
            {older.reading ? 'Reading older runs…' : 'Show older runs'}
          </button>
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

function RunPage({ run: served, reread, go }: { run: IssueRun; reread: (why: string) => void; go: (to: string) => void }) {
  const now = useNow()
  // THE ANSWER TO AN ACTION IS DRAWN AT ONCE: the API returns the run it
  // moved, and a poll that lands later replaces it with whatever it reads.
  const [run, setRun] = useState(served)
  useEffect(() => setRun(served), [served])
  const [acting, setActing] = useState<Acting>({ kind: 'idle' })
  const [open, setOpen] = useState<'none' | 'edit' | 'reject'>('none')

  const canAct = run.state === 'PLANNED' && run.plan_digest !== null && run.plan !== null
  const busy = acting.kind === 'busy'

  async function act(what: 'approve' | 'edit' | 'reject', call: () => Promise<Result<unknown>>) {
    setActing({ kind: 'busy', what })
    const r = await call()
    if (r.status === 'ok' || r.status === 'stale') {
      const next = (r.data as { run?: IssueRun } | null)?.run
      setActing({ kind: 'idle' })
      setOpen('none')
      if (next !== undefined && next !== null && typeof next.id === 'string') setRun(next)
      else reread('The API accepted this and did not echo the run, so it was read again.')
      return
    }
    if (r.status !== 'error') return
    if (r.error.code === 'plan_changed') {
      setActing({ kind: 'idle' })
      setOpen('none')
      reread(
        'The plan changed since you opened it, so nothing was done. This is the plan now: read it, then act on it again.',
      )
      return
    }
    if (r.error.code === 'invalid_run_transition' || r.error.kind === 'conflict') {
      setActing({ kind: 'idle' })
      setOpen('none')
      reread(`The run moved on since you opened it, so nothing was done: ${r.error.message}`)
      return
    }
    setActing({ kind: 'failed', error: r.error })
  }

  const digest = run.plan_digest
  return (
    <div className="rn-page">
      <section className="rn-main">
        <h2 className="rn-title">
          {run.state === 'PLANNING' || run.state === 'PLANNED' ? `Plan for ${run.issue.ref}` : run.issue.ref}
        </h2>
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
          ) : open === 'edit' && canAct ? (
            <PlanEditor
              plan={run.plan}
              busy={busy}
              onCancel={() => setOpen('none')}
              onSave={(plan) => void act('edit', () => editPlan(run.id, digest!, plan))}
            />
          ) : (
            <>
              <p className="sb-note">
                revision {run.plan_revision}
                {run.plan_edited_by !== null && <> · edited by {run.plan_edited_by}</>}
                {' · '}{run.plan.steps.length === 1 ? '1 step' : `${run.plan.steps.length} steps`}, then a review and a fix
                gated on its verdict · every step runs as claude-code
              </p>
              <p className="rn-summary">{run.plan.summary}</p>
              <ol className="rn-steps">
                {run.plan.steps.map((s, i) => (
                  <li key={s.step_id} className="rn-step">
                    <b>{i + 1} · {s.title}</b>
                    <span className="mono sb-note">{s.step_id}</span>
                    <p className="rn-prompt">{s.prompt}</p>
                  </li>
                ))}
              </ol>
            </>
          )}
        </section>

        <section className="rn-overlaps" aria-label="Overlaps">
          <h3>Overlaps the planner found</h3>
          <p className="sb-note">
            <i className="ctl-em">&mdash;</i> not served: a plan holds a summary and steps only, so the run carries no
            overlapping pull requests or issues.
          </p>
        </section>

        {canAct && open !== 'edit' && (
          <div className="rn-actions">
            {open === 'reject' ? (
              <RejectForm busy={busy} onCancel={() => setOpen('none')}
                onReject={(reason) => void act('reject', () => rejectPlan(run.id, digest, reason))} />
            ) : (
              <>
                <button type="button" className="sb-btn is-pri" disabled={busy}
                  onClick={() => void act('approve', () => approvePlan(run.id, digest!))}>
                  {acting.kind === 'busy' && acting.what === 'approve' ? 'Approving…' : 'Approve and run'}
                </button>
                <button type="button" className="sb-btn" disabled={busy} onClick={() => setOpen('edit')}>Edit plan</button>
                <button type="button" className="sb-btn" disabled={busy} onClick={() => setOpen('reject')}>Reject</button>
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
              <a href={run.issue.url} target="_blank" rel="noreferrer" className="mono">{run.issue.ref}</a>
            </li>
            <li className="ctl-fact is-absent">
              <b>Plan comment</b>
              <i className="ctl-em">&mdash; not served by the run</i>
            </li>
            <li className="ctl-fact is-absent">
              <b>Status comment</b>
              <i className="ctl-em">&mdash; not served by the run</i>
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
              <i className="ctl-em">&mdash; not served by the run</i>
            </li>
          </ul>
        </section>
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

/** The plan, editable where a plan step is editable: the summary, and each step's title and prompt. */
function PlanEditor({ plan, busy, onCancel, onSave }: {
  plan: RunPlan; busy: boolean; onCancel: () => void; onSave: (plan: RunPlan) => void
}) {
  const [summary, setSummary] = useState(plan.summary)
  const [steps, setSteps] = useState(plan.steps.map((s) => ({ step_id: s.step_id, title: s.title, prompt: s.prompt })))
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
        <button type="submit" className="sb-btn is-pri" disabled={busy || blank}>Save the plan</button>
        <button type="button" className="sb-btn" disabled={busy} onClick={onCancel}>Cancel</button>
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
        <button type="submit" className="sb-btn" disabled={busy}>Reject this plan</button>
        <button type="button" className="sb-btn" disabled={busy} onClick={onCancel}>Keep it</button>
      </div>
    </form>
  )
}
