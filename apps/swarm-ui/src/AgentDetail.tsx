import { useCallback } from 'react'
import { loadAgentDetail, type AgentDetail } from './api'
import { num } from './fetch'
import { Screen, timeAgo } from './Shell'
import {
  TERMINAL_STATES,
  elapsed,
  stateGlyph,
  stateTone,
  usageOf,
  whyAgent,
  type ArtifactRef,
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

      {/* ---- Why ---- */}
      {why && (
        <section className="section">
          <h2>Why</h2>
          <p className="why-full">{why}</p>
          {task.last_error && task.state !== 'FAILED' && (
            <pre className="err">{task.last_error}</pre>
          )}
        </section>
      )}

      {/* ---- Timeline ---- */}
      <Timeline events={events} detail={eventsDetail} />

      {/* ---- Placement ---- */}
      <Placement events={events} task={task} />

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
    return (
      <section className="section">
        <h2>Timeline</h2>
        <p className="muted">
          The read succeeded and returned no events. Events are written from
          submission onward, so an agent with none has not been admitted yet.
        </p>
      </section>
    )
  }

  return (
    <section className="section">
      <h2>Timeline</h2>
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
                  <dd className="mono uri">{uri}</dd>
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
