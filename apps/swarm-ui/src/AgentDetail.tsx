import { useCallback, useEffect, useState } from 'react'
import { loadAgentDetail, loadAttempts, type AgentDetail } from './api'
import { num } from './fetch'
import { LivenessBadge } from './Liveness'
import { Screen, timeAgo } from './Shell'
import {
  TERMINAL_STATES,
  elapsed,
  stateGlyph,
  stateTone,
  usageOf,
  whyAgent,
  type ArtifactRef,
  type AttemptRow,
  type GitSummary,
  type ResultSummary,
  type Task,
  type TaskEvent,
} from './types'

/**
 * One agent, in full. Opened from a row on the Agents screen.
 *
 * Five panels: Why, Timeline, Output, Placement, Input. The split is not
 * cosmetic -- each answers a different question, and three of them are the
 * only place certain values may appear at all. Placement in particular costs
 * one subcollection query for one task, which is exactly why it is here and
 * not a column on a 200-row table.
 */
export function AgentDetailScreen({ taskId, onClose }: { taskId: string; onClose: () => void }) {
  const load = useCallback(() => loadAgentDetail(taskId), [taskId])

  return (
    <div className="drawer" role="dialog" aria-label={`Agent ${taskId}`}>
      <button className="drawer-close" onClick={onClose} aria-label="Close">
        ✕
      </button>
      <Screen title={taskId} load={load} summary={(d) => d.task.runner_profile}>
        {(d) => <Detail detail={d} />}
      </Screen>
    </div>
  )
}

function Detail({ detail }: { detail: AgentDetail }) {
  const { task, events, eventsDetail } = detail
  const now = Date.now()
  const el = elapsed(task, now)
  const why = whyAgent(task)
  const summary = (task.result_summary ?? null) as ResultSummary | null
  const usage = usageOf(task)

  return (
    <>
      <section className="section">
        <h2>
          <span className={`st ${stateTone(task.state)}`}>
            <span aria-hidden>{stateGlyph(task.state)}</span> {task.state}
          </span>
          <LivenessBadge task={task} events={events} now={now} />
        </h2>
        <dl className="kv">
          <dt>Elapsed</dt>
          <dd>{el.text}</dd>
          <dt>Attempts</dt>
          <dd>
            {task.attempt_count} of {task.max_attempts}
          </dd>
          <dt>Owner</dt>
          <dd>{task.submitted_by ?? '—'}</dd>
          <dt>Created</dt>
          <dd>{timeAgo(task.created_at)}</dd>
          {task.cancel_requested && !TERMINAL_STATES.has(task.state) && (
            <>
              <dt>Cancel</dt>
              <dd className="warn-text">
                Requested. The state will not change until the worker or the
                reconciler releases the lease — releasing it from the API would
                decrement a pool a live container still occupies.
              </dd>
            </>
          )}
        </dl>
      </section>

      {task.park_reason && (
        <div className="bar amber">
          <strong>{task.park_reason}</strong>
          {task.next_eligible_at && (
            <> — eligible again {new Date(task.next_eligible_at).toLocaleString()}</>
          )}
        </div>
      )}

      {/* NOT the same thing as park_reason, and this is the case a header
          that only renders park_reason gets wrong. record_blockers writes
          blocked_by while deliberately leaving the task READY -- the platform
          being busy is not a durable condition -- so READY with blockers and
          no park reason is the commonest "why is nothing happening", and it
          would otherwise show as a bare READY chip with no explanation. */}
      {task.blocked_by && task.blocked_by.length > 0 && (
        <div className="bar amber">
          {task.blocked_by.map((b, i) => (
            <div className="blocker" key={`${b.reason}-${i}`}>
              {b.pool && <code>{b.pool}</code>} <strong>{b.reason}</strong>
              {typeof b.active === 'number' && typeof b.limit === 'number' && (
                <> · {b.active} active / {b.limit} limit</>
              )}
              <span className="blocker-copy">
                {b.reason === 'TENANT_LIMIT'
                  ? 'Yours to raise.'
                  : b.reason === 'GLOBAL_CONCURRENCY_LIMIT'
                    ? 'The platform is full.'
                    : b.reason === 'MANUAL_PAUSE'
                      ? 'This pool was paused by an operator — a decision, not congestion.'
                      : ''}
              </span>
            </div>
          ))}
        </div>
      )}

      {task.cancel_requested && !TERMINAL_STATES.has(task.state) && (
        <div className="bar red">
          Cancellation requested. The lease is released by the worker or the
          reconciler, not by the API.
        </div>
      )}

      {/* ---- Why ---- */}
      {why && (
        <section className="section">
          <h2>Why</h2>
          <p className="why-full">{why}</p>
        </section>
      )}

      {task.last_error && <ErrorBanner text={task.last_error} />}

      {/* ---- Timeline ---- */}
      <Timeline events={events} detail={eventsDetail} />

      {/* ---- Placement ---- */}
      <Placement events={events} task={task} />

      {/* ---- Attempts ---- */}
      <Attempts taskId={task.id} attemptCount={task.attempt_count} />

      {/* ---- Output ---- */}
      <Output summary={summary} task={task} usage={usage} />

      {/* ---- Input ---- */}
      <section className="section">
        <h2>Input</h2>
        <dl className="kv">
          <dt>Runner profile</dt>
          <dd>{task.runner_profile}</dd>
          <dt>Resource class</dt>
          <dd>{task.resource_class}</dd>
          <dt>Provider</dt>
          <dd>{task.provider ?? '—'}</dd>
          <dt>Model</dt>
          <dd>{task.model ?? '—'}</dd>
          <dt>Repository</dt>
          <dd>
            {task.repository_url ?? '—'}
            {task.repository_ref && ` @ ${task.repository_ref}`}
          </dd>
          <dt>Timeout</dt>
          <dd>{task.timeout_seconds !== null ? `${task.timeout_seconds}s` : '—'}</dd>
        </dl>
        {task.input != null && (
          <pre className="json">{JSON.stringify(task.input, null, 2)}</pre>
        )}
      </section>
    </>
  )
}

/**
 * The error banner. Full text, monospace, NEVER one-line-truncated -- it is
 * the reason the page was opened.
 *
 * Three flavours, distinguished by prefix because each means something
 * different about who decided the task had failed.
 */
function ErrorBanner({ text }: { text: string }) {
  const reconciled = text.startsWith('reconciled:')
  // A dispatch failure is written as `<STABLE_CODE> (attempt att_...)`.
  const dispatch = /^[A-Z][A-Z0-9_]+ \(attempt /.test(text)

  return (
    <section className="section">
      <h2>Error</h2>
      <div className="state failed">
        <p className="err-origin">
          {reconciled
            ? 'Written by the reconciler, which found this attempt in a state it could not repair.'
            : dispatch
              ? 'A dispatch failure, recorded by the scheduler. The full message is in the operator logs — this field is truncated at 1000 characters.'
              : "The agent's own error at finish, or the cancellation reason if it was cancelled before it held capacity."}
        </p>
        <pre className="err full">{text}</pre>
      </div>
    </section>
  )
}

/**
 * Every attempt, not just the last one.
 *
 * This is the panel `result_summary` cannot provide. It is written once, by
 * finish(), at terminal state -- so a task that failed twice and succeeded on
 * the third carries ONLY attempt three's numbers. The first two attempts'
 * exit codes, errors and peak RSS live on the attempt documents, and until
 * the P1 read path existed nothing could reach them.
 */
function Attempts({ taskId, attemptCount }: { taskId: string; attemptCount: number }) {
  const [rows, setRows] = useState<AttemptRow[] | null>(null)
  const [failed, setFailed] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    loadAttempts(taskId).then((r) => {
      if (!live) return
      if (r.status === 'ok' || r.status === 'stale') setRows(r.data.attempts)
      else if (r.status === 'empty') setRows([])
      else if (r.status === 'error') setFailed(r.error.message)
    })
    return () => {
      live = false
    }
  }, [taskId])

  if (failed !== null) {
    return (
      <section className="section">
        <h2>Attempts</h2>
        <div className="state partial" role="status">
          <h3>Attempt history could not be read</h3>
          <p>This is a failed read, not a task that never ran. {failed}</p>
        </div>
      </section>
    )
  }
  if (rows === null) return <div className="skeleton" style={{ height: 60 }} />

  return (
    <section className="section">
      <h2>Attempts</h2>
      {rows.length === 0 ? (
        <p className="muted">
          No attempt documents. A task that has never been admitted has none —
          this is not the same as an attempt that failed.
        </p>
      ) : (
        <>
          {rows.length < attemptCount && (
            <p className="warn-text">
              The task records {attemptCount} attempts and {rows.length}{' '}
              {rows.length === 1 ? 'document' : 'documents'} came back. The rest
              are missing, not absent.
            </p>
          )}
          <div className="table-wrap">
            <table className="pools">
              <thead>
                <tr>
                  <th scope="col" className="n">Gen</th>
                  <th scope="col">Backend</th>
                  <th scope="col" className="n">Exit</th>
                  <th scope="col">Error</th>
                  <th scope="col" className="n">Peak RSS</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((a) => (
                  <tr key={a.attempt_id} className={a.exit_code ? 'over' : undefined}>
                    <td className="n">{a.generation}</td>
                    <td>
                      {a.backend}
                      {a.oom_near_miss && (
                        <span className="tag full" title="Came close to the memory ceiling">
                          OOM near miss
                        </span>
                      )}
                    </td>
                    {/* null exit code means still running or never finished,
                        which is not the same as exit 0. */}
                    <td className="n">{a.exit_code === null ? '\u2014' : a.exit_code}</td>
                    <td>{a.error ?? '\u2014'}</td>
                    <td className="n">
                      {a.peak_rss_bytes === null
                        ? '\u2014'
                        : `${(a.peak_rss_bytes / 1e9).toFixed(2)} GB`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="muted small">
            Token and cost columns are omitted rather than shown empty: they
            are null on every attempt that ran before the worker capture was
            fixed, and null is not zero.
          </p>
        </>
      )}
    </section>
  )
}

function Timeline({ events, detail }: { events: TaskEvent[] | null; detail: string | null }) {
  if (events === null) {
    return (
      <section className="section">
        <h2>Timeline</h2>
        <div className="state partial" role="status">
          <h3>The event history could not be read</h3>
          <p>
            This is a failed read, not an empty history. {detail}
          </p>
        </div>
      </section>
    )
  }
  if (events.length === 0) {
    // THERE IS NO LEGITIMATE EMPTY STATE HERE, and this is where the whole
    // rule pays for itself. create_tasks writes the task document and its
    // `submitted` event in the SAME BATCH (swarm_api/store.py:337-366),
    // explicitly so a partially written submission cannot leave a task with no
    // event trail. So a task that exists has at least one event, and a 200
    // with zero rows is a failed query wearing a success code.
    return (
      <section className="section">
        <h2>Timeline</h2>
        <div className="state failed">
          <h3>Timeline unavailable</h3>
          <p>
            This task must have at least a <code>submitted</code> event — it is
            written in the same batch as the task itself — and the query
            returned none. This is a failed read, not an empty history.
          </p>
        </div>
      </section>
    )
  }

  // The endpoint orders `at` ASCENDING, applies the page limit and returns NO
  // page token, so a task with more than 200 events hands back the OLDEST 200
  // and the newest are unreachable. That is not a corner case: the worker
  // heartbeats every 30s, so a two-hour attempt writes ~240 heartbeat events
  // before anything else, and the end of the timeline -- the part the page was
  // opened for -- is exactly the part the API drops.
  const truncated = events.length >= 200

  return (
    <section className="section">
      <h2>Timeline</h2>
      {truncated && (
        <div className="state partial" role="status">
          <h3>Showing the oldest 200 events</h3>
          <p>
            Newer events are not reachable through this endpoint yet — it orders
            oldest-first, caps the page and returns no page token. The end of
            this task&apos;s history is missing, not absent.
          </p>
        </div>
      )}
      <ol className="timeline">
        {events.map((e) => (
          <li key={e.event_id}>
            <span className="ev-type">{e.type}</span>
            <span className="ev-at">{timeAgo(e.at)}</span>
            {e.generation !== null && (
              <span className="ev-gen" title="Fencing generation for this event">
                gen {e.generation}
              </span>
            )}
            {/* Only the reconciler labels itself. THE TEST IS THE VALUE, NOT
                THE KEY: the worker's quota_exhausted events also carry a
                detail.source, describing where the quota signal came from, so
                a presence check badges them as reconciler work. Scheduler-,
                worker- and API-written events are indistinguishable by payload
                alone, and the honest label for those rows is the event type. */}
            {e.detail?.['source'] === 'reconciler' && (
              <span className="ev-badge">reconciler</span>
            )}
            {e.detail && Object.keys(e.detail).length > 0 && (
              <pre className="ev-detail">{JSON.stringify(e.detail, null, 2)}</pre>
            )}
          </li>
        ))}
      </ol>
    </section>
  )
}

/**
 * Where the agent actually ran.
 *
 * There is NO API read path for the `attempts` or `leases` collections. The
 * documents are written, the indexes exist and the decoders exist -- only the
 * routes are missing. So execution name and backend come from the DISPATCHED
 * event's detail, which the scheduler writes (scheduler/store.py:240-247).
 *
 * That is one subcollection query per task, which is affordable exactly here
 * and nowhere near a 200-row table.
 */
function Placement({ events, task }: { events: TaskEvent[] | null; task: Task }) {
  const dispatched = events?.find((e) => e.type === 'dispatched')
  const exec = dispatched?.detail?.['execution_name']
  const backend = dispatched?.detail?.['backend']

  return (
    <section className="section">
      <h2>Placement</h2>
      <dl className="kv">
        <dt>Backend</dt>
        <dd>{typeof backend === 'string' ? backend : '—'}</dd>
        <dt>Execution</dt>
        <dd className="mono">{typeof exec === 'string' ? exec : '—'}</dd>
        <dt>Tenant</dt>
        <dd>{task.tenant_id}</dd>
      </dl>
      <p className="muted small">
        Taken from the <code>dispatched</code> event. Pod name, exit code, peak
        RSS and heartbeat age are written to the <code>attempts</code> and{' '}
        <code>leases</code> collections, which have no API read path — so they
        are not shown rather than guessed.
      </p>
    </section>
  )
}

function Output({
  summary,
  task,
  usage,
}: {
  summary: ResultSummary | null
  task: Task
  usage: Record<string, number | string[]> | null
}) {
  const artifacts = (summary?.artifacts ?? []) as ArtifactRef[]
  const logs = summary?.logs ?? {}
  const terminal = TERMINAL_STATES.has(task.state)

  return (
    <section className="section">
      <h2>Output</h2>

      {!terminal ? (
        <p className="muted">
          A result summary is written only when an attempt finishes. This agent
          has not finished, so there is nothing here yet — which is not the same
          as producing nothing.
        </p>
      ) : summary === null ? (
        <p className="muted">
          This agent reached a terminal state without a result summary. A parked
          attempt puts its summary in the event detail instead.
        </p>
      ) : (
        <>
          <GitOutcome git={summary.git} artifacts={artifacts} />

          {artifacts.length > 0 ? (
            <table className="pools">
              <thead>
                <tr>
                  <th scope="col">Artifact</th>
                  <th scope="col" className="n">Bytes</th>
                  <th scope="col">URI</th>
                </tr>
              </thead>
              <tbody>
                {artifacts.map((a) => (
                  <tr key={a.uri}>
                    <th scope="row">{a.name}</th>
                    <td className="n">{num(a.bytes)}</td>
                    {/* Passed by reference. No download URL is minted here --
                        the reader uses their own credentials against GCS,
                        which keeps the tenant boundary in one place. */}
                    <td className="mono uri">{a.uri}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="muted">This attempt uploaded no artifacts.</p>
          )}

          {summary.artifacts_skipped && summary.artifacts_skipped.length > 0 && (
            <p className="warn-text">
              {summary.artifacts_skipped.length} artifact
              {summary.artifacts_skipped.length === 1 ? ' was' : 's were'} skipped
              for exceeding the size cap, so this list is incomplete.
            </p>
          )}

          {Object.keys(logs).length > 0 && (
            <dl className="kv">
              {Object.entries(logs).map(([label, uri]) => (
                <div key={label} style={{ display: 'contents' }}>
                  <dt>{label}</dt>
                  <dd className="mono uri">
                    {uri}
                    {/* No download link, by design: nothing here mints a signed
                        URL. The caller reads GCS with their own credentials,
                        which keeps the tenant boundary in one place. */}
                    <button
                      className="copy"
                      onClick={() => navigator.clipboard?.writeText(`gsutil cat ${uri}`)}
                    >
                      copy gsutil
                    </button>
                  </dd>
                </div>
              ))}
            </dl>
          )}

          <Usage usage={usage} profile={task.runner_profile} />
        </>
      )}
    </section>
  )
}

/**
 * Token and cost, when they exist.
 *
 * Only claude-code and codex produce these -- both go through
 * `cliagent.run_cli_agent`. mock, generic and browser report nothing, and
 * NOTHING IS NOT ZERO: an em dash, never $0.00. A zero here would be read as
 * "this run was free".
 */
function Usage({ usage, profile }: { usage: Record<string, number | string[]> | null; profile: string }) {
  const produces = profile === 'claude-code' || profile === 'codex'

  if (!produces) {
    return (
      <p className="muted small">
        The <code>{profile}</code> runner does not report tokens or cost. That is
        an absence of measurement, not a run that cost nothing.
      </p>
    )
  }
  if (usage === null) {
    return (
      <p className="muted small">
        No usage was recorded for this attempt. Until the worker fix ships in a
        new <code>agent-runtime-base</code> image and attempts run on it, this is
        expected rather than a sign the run was free.
      </p>
    )
  }

  const n = (k: string) => (typeof usage[k] === 'number' ? (usage[k] as number) : null)
  const cost = n('total_cost_usd')

  return (
    <dl className="kv">
      <dt>Input tokens</dt>
      <dd>{num(n('input_tokens'))}</dd>
      <dt>Output tokens</dt>
      <dd>{num(n('output_tokens'))}</dd>
      <dt>Cache read</dt>
      <dd>{num(n('cache_read_input_tokens'))}</dd>
      <dt>Cost</dt>
      <dd>{cost === null ? '—' : `$${cost.toFixed(4)}`}</dd>
      {Array.isArray(usage['models']) && (
        <>
          <dt>Models</dt>
          <dd>{(usage['models'] as string[]).join(', ')}</dd>
        </>
      )}
    </dl>
  )
}

/**
 * What happened to the code, if the task had any.
 *
 * WHY THIS PANEL LEADS WITH A REASON RATHER THAN A LINK. A pull request is
 * absent for at least six different causes, and they need completely different
 * responses from the person reading this:
 *
 *   the tenant's token has no push permission   -> grant write scope
 *   the attempt parked                          -> wait; it is not finished
 *   the agent changed nothing                   -> the run did nothing useful
 *   the host is not a forge we can publish to   -> apply the patch by hand
 *   the push was rejected (non-fast-forward)    -> something else moved the branch
 *   the pull request call failed                -> the branch IS pushed; retry the PR
 *
 * The first is the expected one today and is not a failure, so it is rendered
 * as a fact with its reason, never as an error. Collapsing all six into "no
 * pull request" would be exactly the truth bug the rest of this UI exists to
 * avoid.
 *
 * DIRTY FILES ARE SHOWN EVEN WHEN THERE ARE NO COMMITS, because that is the
 * common case: most agents edit files and never run `git commit`. A panel that
 * only counted commits would report "no changes" for the majority of real runs.
 */
function GitOutcome({
  git,
  artifacts,
}: {
  git: GitSummary | undefined
  artifacts: ArtifactRef[]
}) {
  if (!git) return null

  const commits = git.commits ?? []
  const dirty = git.dirty ?? []
  const pr = git.pull_request
  // The patch is an ordinary artifact; matching by name is what turns the
  // recorded name into the GCS uri without minting a second copy of it.
  const patch = git.patch ? artifacts.find((a) => a.name === git.patch) : undefined

  return (
    <div className="section panel git-outcome">
      <h3>Code</h3>

      {git.error ? (
        <p className="warn-text">
          The change could not be read from the workspace: {git.error}. This says
          nothing about whether the agent did work — only that git could not be
          asked.
        </p>
      ) : null}

      <dl className="kv">
        <dt>Commits</dt>
        <dd>
          {commits.length === 0 ? (
            <span className="muted">
              none
              {dirty.length > 0 && ' — the agent edited files without committing'}
            </span>
          ) : (
            <>
              {git.commit_count ?? commits.length} on top of{' '}
              <span className="mono">{git.base ? git.base.slice(0, 10) : '—'}</span>
              {typeof git.insertions === 'number' && typeof git.deletions === 'number' && (
                <span className="muted small">
                  {' '}
                  · +{git.insertions} −{git.deletions}
                </span>
              )}
            </>
          )}
        </dd>

        {dirty.length > 0 && (
          <>
            <dt>Uncommitted</dt>
            <dd>
              {git.dirty_count ?? dirty.length} file
              {(git.dirty_count ?? dirty.length) === 1 ? '' : 's'}
              {git.dirty_truncated && (
                <span className="muted small"> · list truncated</span>
              )}
            </dd>
          </>
        )}

        <dt>Patch</dt>
        <dd>
          {patch ? (
            <span className="mono uri">{patch.uri}</span>
          ) : git.patch_omitted ? (
            <span className="warn-text">
              discarded at {num(git.patch_bytes)} bytes — over the cap. It was
              not truncated: a truncated patch applies cleanly and silently
              drops the rest of the change.
            </span>
          ) : (
            <span className="muted">none — nothing differed from the clone</span>
          )}
        </dd>

        <dt>Pull request</dt>
        <dd>
          {pr ? (
            <>
              <a href={pr.url} target="_blank" rel="noreferrer">
                #{pr.number}
              </a>{' '}
              <span className={`tag ${pr.state === 'open' ? 'ok' : 'wait'}`}>{pr.state}</span>
              {pr.created === false && (
                <span className="muted small"> · already existed, reused</span>
              )}
            </>
          ) : (
            <span className="muted">
              none — {git.publish_reason ?? 'no reason was recorded'}
            </span>
          )}
        </dd>

        {git.branch && (
          <>
            <dt>Branch</dt>
            <dd className="mono">{git.branch}</dd>
          </>
        )}
      </dl>

      {git.auto_committed && (
        <p className="muted small">
          One commit on this branch was made by the worker, not the agent: the
          agent left changes uncommitted and they would otherwise not have
          reached the branch at all.
        </p>
      )}

      {commits.length > 0 && (
        <div className="table-wrap">
          <table className="pools">
            <thead>
              <tr>
                <th scope="col">Commit</th>
                <th scope="col">Subject</th>
                <th scope="col" className="n">Files</th>
                <th scope="col" className="n">+/−</th>
              </tr>
            </thead>
            <tbody>
              {commits.map((c) => (
                <tr key={c.sha}>
                  <th scope="row" className="mono">{c.sha.slice(0, 10)}</th>
                  <td>{c.subject}</td>
                  <td className="n">
                    {c.files_changed}
                    {c.binary_files > 0 && (
                      <span className="muted small"> ({c.binary_files} binary)</span>
                    )}
                  </td>
                  <td className="n">
                    +{c.insertions} −{c.deletions}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {(git.commit_count ?? 0) > commits.length && (
        <p className="muted small">
          {git.commit_count} commits were made; the newest {commits.length} are
          listed. The patch above carries all of them.
        </p>
      )}
    </div>
  )
}
