import { useEffect, useState, type ReactNode } from 'react'
import { loadCheckpoints, loadTaskLogs } from './api'
import { CheckpointBrowser } from './CheckpointBrowser'
import { FailedPanel } from './Shell'
import type { Result } from './fetch'
import {
  bytesLabel,
  type CheckpointRecord,
  type CheckpointsPage,
  type LogStream,
  type Task,
  type TaskLogs,
} from './types'

/**
 * CHECKPOINTS AND LOGS, READ FROM THE ROUTES THAT SERVE THEM.
 *
 * THIS IS THE FIRST CALLER OF EITHER LOADER. `loadCheckpoints` (api.ts) and
 * `loadTaskLogs` (api.ts) were written in full -- query building, byte-offset
 * paging, the three-state `status` discipline, a comment explaining why no
 * empty predicate is passed -- and NOTHING HAS EVER CALLED THEM. The routes
 * behind them exist too (`GET /v1/tasks/{id}/checkpoints`,
 * `GET /v1/tasks/{id}/logs`, with `tests/unit/control_plane/
 * test_checkpoint_read_path.py` and `test_log_read_path.py` over both). Both
 * ends of the seam were built and the middle was never joined, which is the
 * defect class this repository keeps producing: nothing about it looks broken.
 *
 * It also retires a claim the product was making that had stopped being true.
 * `AgentDetail`'s log footnote said "there is no log-tail read path on the API,
 * so a running agent's output is not readable here at all". There is one, it
 * serves a live window, and this panel reads it.
 *
 * THE THREE-STATE RULE, which both payloads are built around and which this
 * file must not collapse:
 *
 *   content: null  absent or unreadable. NEVER rendered as text.
 *   content: ''    an object that exists and is empty. A real answer.
 *   resumable: null   the manifest could not be read, so nothing is known.
 *   resumable: false  the manifest was read and says it cannot be resumed.
 *
 * `content ?? ''` and `resumable ?? false` are the two lines that would undo
 * all of it, and neither appears here.
 */
export function RunFiles({ task }: { task: Task }) {
  return (
    <>
      <CheckpointsPanel task={task} />
      <LogsPanel task={task} />
    </>
  )
}

/**
 * One read, with its four states kept apart.
 *
 * `Screen` in Shell.tsx owns this for a whole route and draws an `<h1>`, so it
 * cannot be nested inside a panel. This is the same discipline at panel scale:
 * a failure is never rendered as an absence, and a component below it can only
 * be reached with data.
 */
function useRead<T>(load: () => Promise<Result<T>>, key: string): Result<T> {
  const [state, setState] = useState<Result<T>>({ status: 'loading', since: Date.now() })
  useEffect(() => {
    let live = true
    setState({ status: 'loading', since: Date.now() })
    load().then((next) => {
      if (live) setState(next)
    })
    return () => {
      live = false
    }
    // `load` is a fresh closure every render; the task id is what actually
    // changes, and keying on it is what makes a second task refetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])
  return state
}

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="section">
      <h2>{title}</h2>
      {children}
    </section>
  )
}

function Reading() {
  return <p className="muted">Reading…</p>
}

// ---------------------------------------------------------------------------
// Checkpoints
// ---------------------------------------------------------------------------

function CheckpointsPanel({ task }: { task: Task }) {
  const state = useRead<CheckpointsPage>(() => loadCheckpoints(task.id), task.id)

  if (state.status === 'loading') {
    return (
      <Panel title="Checkpoints">
        <Reading />
      </Panel>
    )
  }
  if (state.status === 'error') {
    return (
      <Panel title="Checkpoints">
        {/* A failed listing is NOT "this task has no checkpoints". The route
            answers 503 for a listing that failed precisely so the two stay
            apart, and drawing an empty panel here would throw that away. */}
        <FailedPanel error={state.error} onRetry={() => window.location.reload()} />
      </Panel>
    )
  }
  // `loadCheckpoints` passes no empty predicate, so `empty` is unreachable --
  // but `Result` has five members and a switch that ignores one is how a state
  // goes unhandled. An explicit branch, with the reason, rather than a cast.
  if (state.status === 'empty') {
    return (
      <Panel title="Checkpoints">
        <p className="muted">
          The checkpoint route answered with no body. That is not a listing
          result and nothing can be concluded from it.
        </p>
      </Panel>
    )
  }

  const page = state.data
  return (
    <Panel title="Checkpoints">
      <p className="muted small">
        Prefix <code className="mono">{page.prefix}</code> · {page.count} of{' '}
        {page.total_found} found
        {page.truncated && ' · the scan limit cut the prefix short, so older checkpoints exist beyond these'}
        {page.next_page_token !== null && ' · more pages remain'}
      </p>

      <LatestPointer page={page} />

      {page.checkpoints.length === 0 ? (
        <p className="muted">
          {page.listed
            ? 'The listing succeeded and this task has written no checkpoint. A real zero.'
            : 'The listing did not complete, so whether this task has checkpoints is unknown.'}
        </p>
      ) : (
        <div className="ckpt-rows">
          {page.checkpoints.map((c) => (
            <CheckpointRow key={`${c.attempt_id}/${c.checkpoint_id}`} taskId={task.id} record={c} />
          ))}
        </div>
      )}
    </Panel>
  )
}

/**
 * What `task.latest_checkpoint` points at, and whether it points anywhere.
 *
 * `outside_this_task` is a FINDING, not a formatting case: a resuming worker
 * ignores such a pointer entirely, so a task carrying one would restart from
 * nothing while the screen showed a checkpoint beside it.
 */
function LatestPointer({ page }: { page: CheckpointsPage }) {
  const p = page.latest_checkpoint
  switch (p.status) {
    case 'unset':
      return (
        <p className="muted small">
          No latest-checkpoint pointer is set on the task. A retry would start
          from the beginning.
        </p>
      )
    case 'present':
      return (
        <p className="muted small">
          The task&apos;s latest-checkpoint pointer names{' '}
          <code className="mono">{p.checkpoint_id}</code>, which is in the list below.
        </p>
      )
    case 'missing':
      return (
        <p className="rollup untrusted">
          The task points at <code>{p.pointer}</code> and the listing did not find it.
          {p.detail ? ` ${p.detail}` : ''}
        </p>
      )
    case 'outside_this_task':
      return (
        <p className="rollup untrusted">
          The task points at <code>{p.pointer}</code>, which is not under this
          task&apos;s prefix. A resuming worker ignores a pointer like that
          entirely, so this task would restart from nothing.
          {p.detail ? ` ${p.detail}` : ''}
        </p>
      )
  }
}

function CheckpointRow({ taskId, record }: { taskId: string; record: CheckpointRecord }) {
  // What is INSIDE the checkpoint (A3): the tree, one file, the archive. See
  // CheckpointBrowser.tsx; it owns its own reads and its own absent states.
  const [browsing, setBrowsing] = useState(false)
  return (
    <div className="ckpt">
      <div className="ckpt-head">
        <span className="mono">{record.checkpoint_id}</span>
        {record.is_latest_pointer && <span className="tag ok">latest</span>}
        {record.label !== null && <span className="tag">{record.label}</span>}
        <button
          type="button"
          className="copy"
          aria-expanded={browsing}
          onClick={() => setBrowsing((v) => !v)}
        >
          files
        </button>
      </div>
      {browsing && (
        <CheckpointBrowser
          taskId={taskId}
          attemptId={record.attempt_id}
          checkpointId={record.checkpoint_id}
          onClose={() => setBrowsing(false)}
        />
      )}
      <dl className="kv">
        <dt>Attempt</dt>
        <dd className="mono">
          {record.attempt_id}
          {!record.attempt_known && (
            <span className="muted small">
              {' '}
              — no attempt document was found for it. That is a missing record,
              not an attempt that never ran.
            </span>
          )}
        </dd>
        <dt>Stored</dt>
        {/* A byte count that came from a listing. `stored_bytes` is summed over
            objects the server actually saw, so a zero here is measured. */}
        <dd>{bytesLabel(record.stored_bytes)} in {record.objects.length} object
          {record.objects.length === 1 ? '' : 's'}</dd>
        <dt>Manifest</dt>
        <dd>
          {record.manifest === 'present' && 'present'}
          {record.manifest === 'absent' &&
            'absent — the worker never wrote the commit marker for this checkpoint.'}
          {record.manifest === 'unreadable' &&
            'unreadable — it is there and could not be parsed, so nothing inside it is known.'}
          {record.manifest_detail !== null && (
            <span className="muted small"> {record.manifest_detail}</span>
          )}
        </dd>
        <dt>Files inside</dt>
        <dd>
          {record.file_count === null ? (
            <span className="ctl-em">not known — the manifest could not be read</span>
          ) : (
            record.file_count
          )}
        </dd>
        <dt>Resumable</dt>
        <dd>
          {/* THREE VALUES. `resumable ?? false` here would turn "we could not
              tell" into "a retry cannot use this", which is a different and
              much more alarming sentence. */}
          {record.resumable === null
            ? 'cannot tell — the manifest could not be read'
            : record.resumable
              ? 'yes'
              : 'no'}
          {record.resumable_detail !== null && (
            <span className="muted small"> — {record.resumable_detail}</span>
          )}
        </dd>
      </dl>
      {record.objects.length > 0 && (
        <details>
          <summary className="muted small">
            {record.objects.length} object{record.objects.length === 1 ? '' : 's'} in the bucket
          </summary>
          <dl className="kv">
            {record.objects.map((o) => (
              <div key={o.key} style={{ display: 'contents' }}>
                <dt className="mono">{o.name}</dt>
                <dd>{bytesLabel(o.bytes)}</dd>
              </div>
            ))}
          </dl>
        </details>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Logs
// ---------------------------------------------------------------------------

function LogsPanel({ task }: { task: Task }) {
  const state = useRead<TaskLogs>(() => loadTaskLogs(task.id), task.id)

  if (state.status === 'loading') {
    return (
      <Panel title="Output, as the agent wrote it">
        <Reading />
      </Panel>
    )
  }
  if (state.status === 'error') {
    return (
      <Panel title="Output, as the agent wrote it">
        <FailedPanel error={state.error} onRetry={() => window.location.reload()} />
      </Panel>
    )
  }
  if (state.status === 'empty') {
    return (
      <Panel title="Output, as the agent wrote it">
        <p className="muted">
          The log route answered with no body, which is not a stream result.
        </p>
      </Panel>
    )
  }

  const logs = state.data
  return (
    <Panel title="Output, as the agent wrote it">
      <p className="muted small">
        {attemptLine(logs)} · redaction{' '}
        {logs.redaction.applied_at_read_time
          ? `applied at read time over ${logs.redaction.rules} rule${logs.redaction.rules === 1 ? '' : 's'}`
          : 'NOT applied at read time by this API'}
        .
      </p>
      {logs.streams.length === 0 ? (
        <p className="muted">
          The route returned no stream at all for this task.
        </p>
      ) : (
        logs.streams.map((s) => <Stream key={s.stream} stream={s} />)
      )}
    </Panel>
  )
}

/** Which attempt the window below belongs to. `no_attempt_yet` is the state a
 *  QUEUED or PARKED task is legitimately in and must not read as a failure. */
function attemptLine(logs: TaskLogs): string {
  switch (logs.attempt.status) {
    case 'latest':
      return `Latest attempt${logs.attempt_id ? ` ${logs.attempt_id}` : ''}`
    case 'requested':
      return `Attempt ${logs.attempt_id ?? 'as requested'}`
    case 'unknown_attempt':
      return 'The attempt asked for is not one this task has'
    case 'no_attempt_yet':
      return 'This task has no attempt yet, so there is nothing to have written a log'
  }
}

function Stream({ stream }: { stream: LogStream }) {
  return (
    <div className="logwin">
      <div className="logwin-head">
        <span className="mono">{stream.stream}</span>
        <span className="muted small">
          {stream.source === null
            ? 'no object served'
            : stream.source === 'live'
              ? 'live tail — the attempt is still writing'
              : 'final — uploaded when the attempt ended'}
          {stream.redacted &&
            ` · ${stream.redaction_count} value${stream.redaction_count === 1 ? '' : 's'} redacted in this window`}
          {stream.truncated && ' · window truncated'}
        </span>
      </div>

      {stream.status === 'absent' && (
        <p className="muted">
          No object exists for this stream. {stream.detail ?? 'Nothing was uploaded.'}
        </p>
      )}
      {stream.status === 'unreadable' && (
        <p className="rollup untrusted">
          The object exists and could not be read.{' '}
          {stream.detail ?? 'No reason was given.'} Nothing below should be taken
          as this stream being empty.
        </p>
      )}
      {stream.status === 'ok' && stream.content === null && (
        /* `status: ok` with no content is the server saying it served a window
           it cannot hand over. Rendering `content ?? ''` here would print an
           empty box that reads as a silent agent. */
        <p className="rollup untrusted">
          The read reported success and returned no content, so this window
          cannot be shown. {stream.detail ?? ''}
        </p>
      )}
      {stream.status === 'ok' && stream.content === '' && (
        <p className="muted">
          This object exists and is empty. The agent wrote nothing to{' '}
          {stream.stream}; this is a real, measured empty stream, not a failed
          read.
        </p>
      )}
      {stream.status === 'ok' && stream.content !== null && stream.content !== '' && (
        <pre className="logwin-body">{stream.content}</pre>
      )}

      <p className="muted small">
        {stream.total_bytes === null ? (
          <>Total size not known — nothing could be read.</>
        ) : (
          <>
            {bytesLabel(stream.returned_bytes)} of {bytesLabel(stream.total_bytes)} from
            byte {stream.offset}
            {stream.next_offset !== null && ` · more from byte ${stream.next_offset}`}
          </>
        )}
        {stream.tail_window !== null && (
          <>
            {' '}· live window at byte {stream.tail_window.object_offset} of a{' '}
            {bytesLabel(stream.tail_window.stream_size)} stream
          </>
        )}
        {stream.uri !== null && (
          <>
            {' '}·{' '}
            <span className="mono uri">{stream.uri}</span>
          </>
        )}
      </p>
    </div>
  )
}
