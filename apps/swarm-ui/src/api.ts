import { noteFixtureProbe, read, route, write, type ApiRoute, type Result } from './fetch'
// A VALUE import, not a type: the attempt fixture needs the terminal-state set
// so a live task's newest attempt is rendered as live, and the task fixture
// needs the concurrency set to decide which rows hold a lease -- invariant 1's
// four states, not a second list written out here.
import { CONCURRENCY_STATES, TERMINAL_STATES } from './types'
// The one rule for summing a nullable measurement: null until something
// actually reported it, so an unmeasured sample never becomes a confident zero.
import { sumReported } from './measure'
import type {
  ArtifactContent, ArtifactListing, LogStream, LogStreamName, TaskAnswer, TaskTranscript, TranscriptStep,
  CheckpointsPage, TaskLogs,
  Capacity, DispatchControl, Me, ProvidersPage, Stats, Task, TaskEvent, TaskPage,
  AttemptRow, LeasePage, LeaseRow, Pool, ProfileAdmission, QuotaState, ResourceClassSpec,
  RunnerInputContract, Runtime, TaskState,
  TaskWindow, Tenant,
  Workflow, WorkflowPage,
  Account, AccountStateName, AccountsPage, RefreshResponse,
  AccountAuthorization, AccountExchangeResponse,
} from './types'

// The fetch contract lives in fetch.ts. This file is only the list of reads
// this product performs, and what "empty" means for each of them -- which is
// per-endpoint and cannot be guessed: a capacity response with no pools and a
// task page with no tasks are both non-empty objects.

export type { Result, ApiError } from './fetch'

/** Fixtures, so the UI can be worked on before DNS resolves. Exported so a
 *  component that keeps its own loaders (CheckpointBrowser.tsx) reads this one
 *  flag rather than restating the expression. */
export const USE_FIXTURES = import.meta.env.DEV && !import.meta.env.VITE_LIVE

export async function loadCapacity(): Promise<Result<Capacity>> {
  if (USE_FIXTURES) return fixtureCapacity()
  return read<Capacity>(route('/v1/capacity'), (d) => d.pools.length === 0)
}

/**
 * The largest page `GET /v1/tasks` serves. Page size caps at 200 server-side
 * (deps.py:194-199); asking for more is silently clamped, which would make
 * "200 tasks" look like the whole truth.
 */
export const TASK_PAGE_LIMIT = 200

/**
 * A page of the task list. `limit` is the full page unless a caller has a
 * reason to ask for less -- the Agents list at phone width asks for 50
 * (Agents.tsx, `PHONE_PAGE_LIMIT`).
 */
export async function loadTasks(limit: number = TASK_PAGE_LIMIT): Promise<Result<TaskPage>> {
  if (USE_FIXTURES) return fixtureTasks()
  // One route with the `?state=` and paged reads below: `/v1/tasks` (CH-18),
  // whatever page size the caller asked for.
  return read<TaskPage>(route('/v1/tasks', {}, `limit=${limit}`), (d) => d.tasks.length === 0)
}

/**
 * Workflows plus the task documents their steps point at.
 *
 * Two requests, because `GET /v1/workflows` returns steps with NO state
 * (codec.py:354-365) -- a step's state only exists on the task it created. The
 * list endpoint does not join for us the way `GET /v1/workflows/{id}` does, so
 * the join happens here, once.
 *
 * The second request is allowed to fail on its own. When it does the board
 * still renders -- the DAG shape, the dependencies and the step names are all
 * in the first response -- but `taskById` is null and every node says its state
 * could not be read. That is the point: a failed join must not look like a
 * workflow full of idle steps.
 */
export interface WorkflowBoard {
  workflows: Workflow[]
  /** null means the task read failed; an EMPTY map means it succeeded with no tasks. */
  taskById: Map<string, Task> | null
  /** Why states are missing, for the banner. null when the join succeeded. */
  statesDetail: string | null
}

export async function loadWorkflowBoard(): Promise<Result<WorkflowBoard>> {
  if (USE_FIXTURES) return fixtureWorkflowBoard()

  const [wf, tasks] = await Promise.all([
    read<{ workflows: Workflow[] }>(route('/v1/workflows?limit=100'), (d) => d.workflows.length === 0),
    read<TaskPage>(route('/v1/tasks', {}, 'limit=200'), (d) => d.tasks.length === 0),
  ])

  // The workflow read decides whether there is a screen at all.
  if (wf.status === 'loading' || wf.status === 'error') return wf
  if (wf.status === 'stale') {
    return { status: 'stale', data: emptyBoard(wf.data.workflows), fetchedAt: wf.fetchedAt, error: wf.error }
  }
  if (wf.status === 'empty') return wf

  // A task read that did not produce rows is not the same as one that failed.
  // `empty` is a real answer -- no tasks exist -- so the join is complete and
  // every step is legitimately "not started". Only a FAILURE leaves states
  // unknown, and only that sets statesDetail.
  if (tasks.status === 'error' || tasks.status === 'stale') {
    return {
      status: 'ok',
      fetchedAt: wf.fetchedAt,
      serverAt: wf.serverAt,
      data: { workflows: wf.data.workflows, taskById: null, statesDetail: tasks.error.message },
    }
  }

  const taskById = new Map<string, Task>()
  if (tasks.status === 'ok') for (const t of tasks.data.tasks) taskById.set(t.id, t)
  return {
    status: 'ok',
    fetchedAt: wf.fetchedAt,
    serverAt: wf.serverAt,
    data: { workflows: wf.data.workflows, taskById, statesDetail: null },
  }
}

function emptyBoard(workflows: Workflow[]): WorkflowBoard {
  return { workflows, taskById: null, statesDetail: 'The task read did not complete.' }
}

/**
 * The workflow LIST alone -- no join, for the landing screen.
 *
 * `loadWorkflowBoard` above exists to draw the graph, so it pays for a second
 * request to learn each step's state. Overview does not draw a graph: it asks
 * whether anything has stopped moving, and `GET /v1/workflows` already answers
 * that without help. Every row carries `rollup.counts`, which the route derived
 * server-side from the step tasks it read (routes/workflows.py:64-84), so the
 * per-state breakdown this screen needs is in the first response and a join
 * here would be a second read of documents the API has already read.
 *
 * The page also carries `rollup_report`, which says whether the derivation was
 * complete. A caller that ignores it can report an all-clear over workflows
 * whose state was never established.
 */
export async function loadWorkflows(): Promise<Result<WorkflowPage>> {
  if (USE_FIXTURES) return fixtureWorkflows()
  // 100, matching loadWorkflowBoard: `max_workflow_steps` is 50 and the route's
  // step-read budget is what actually binds, so a larger page buys rows whose
  // rollup is incomplete rather than more information.
  return read<WorkflowPage>(route('/v1/workflows?limit=100'), (d) => d.workflows.length === 0)
}

/**
 * HOW MANY EVENTS A RUN SCREEN ASKS FOR, and why it has to ask at all.
 *
 * `GET /v1/tasks/{id}/events` with no `limit` answers with the API's DEFAULT
 * page, `default_page_size = 50` (swarm_api/settings.py), not its maximum.
 * Every comment in this client -- and redesign-v2 §4 -- reasons about "the
 * 200-event page", and every run screen was in fact reading 50: at one
 * heartbeat event per 150s plus a checkpoint event per 120s, that page ends
 * around the half-hour mark of an attempt, so the peak-memory line, the
 * checkpoint strip and the checkpoint table's locations all stopped there.
 *
 * 200 is `max_page_size`, the server's own cap (`paged_limit` clamps to it,
 * silently). Asking for more would be clamped to the same 200 and would make
 * a full page look like one that asked for less. The route pages -- since #19
 * it returns `next_page_token` whenever more events exist, and takes
 * `order=desc` -- but this client reads ONE page, oldest-first, and does not
 * follow the page token, so a page that comes back FULL is a window, and the
 * charts say so. That limit is this client's, not the API's (help topic
 * `event-paging`).
 *
 * WHAT THIS CANNOT SEE. `MAX_PAGE_SIZE` is environment-overridable and set
 * nowhere in this repository today. A deployment that lowered it would clamp
 * this request below 200, and a full page would then arrive SHORT of this
 * number and not be flagged as full. Nothing here ever reads a short page as
 * complete in the other direction either: an interval with no recorded end
 * stays open whatever the page length (duration.ts).
 */
export const EVENT_PAGE_LIMIT = 200

/**
 * HOW MANY ATTEMPT DOCUMENTS A RUN SCREEN ASKS FOR: the same cap, for the
 * same reason.
 *
 * `GET /v1/tasks/{id}/attempts` goes through the same `paged_limit`
 * (routes/tasks.py), so with no `limit` it returned 50 attempts, NEWEST
 * first. task_d18d8d8b044d469cb43c reached 83 attempts on 2026-09-23 before
 * the retry cap was enforced on the dispatch-failure path; the inspector
 * would have shown 50 of them and the phase chart's sum would have said
 * "over 50 of 50". The sum now counts against `task.attempt_count`, which
 * catches a short read whatever its cause; this constant makes the read
 * short less often.
 *
 * Stated as its own constant, not as `EVENT_PAGE_LIMIT`, because the two
 * routes happen to share a cap today and nothing makes them share one.
 */
export const ATTEMPT_PAGE_LIMIT = 200

/**
 * One agent, with its event timeline.
 *
 * Two reads, not three. `GET /v1/tasks/{id}/artifacts` is not called because
 * it would be a second copy of something the first read already carries. The
 * route used to read a Firestore subcollection nothing writes, and answered
 * [] for every task; it has since been pointed at the real writer, and
 * `store.list_artifacts` now serves the manifest out of `task.result_summary`
 * (`artifacts`, `artifacts_skipped`, and a `complete` flag that is simply
 * whether `result_summary` exists). That is the same field this screen reads
 * off the task document, so a third request would fetch the same manifest
 * again -- one more read that could fail on its own and disagree with the
 * first. See ResultSummary in types.ts.
 */
export interface AgentDetail {
  task: Task
  /** null means the event read FAILED. An empty array means there are none. */
  events: TaskEvent[] | null
  eventsDetail: string | null
}

export async function loadAgentDetail(taskId: string): Promise<Result<AgentDetail>> {
  if (USE_FIXTURES) return fixtureAgentDetail(taskId)

  const id = { id: taskId }
  const [task, events] = await Promise.all([
    read<{ task: Task } | Task>(route('/v1/tasks/{id}', id), () => false),
    read<{ events: TaskEvent[] }>(route(`/v1/tasks/{id}/events?limit=${EVENT_PAGE_LIMIT}`, id), () => false),
  ])

  if (task.status === 'loading' || task.status === 'error') return task
  if (task.status === 'empty') {
    return {
      status: 'error',
      error: {
        kind: 'not_found',
        httpStatus: 404,
        code: 'not_found',
        message: 'This task does not exist, or it belongs to another tenant.',
      },
    }
  }

  // GET /v1/tasks/{id} returns {"task": {...}} (routes/tasks.py), NOT the bare
  // document -- verified against the live API. The fallback stays because the
  // create and cancel routes return the same wrapper and a caller could route
  // through either, but the wrapper is the norm rather than the exception.
  const raw = task.data as { task?: Task } & Task
  const unwrapped: Task = raw.task ?? raw

  const eventList =
    events.status === 'ok' ? events.data.events
    : events.status === 'empty' ? []
    : null

  return {
    status: task.status === 'stale' ? 'stale' : 'ok',
    data: {
      task: unwrapped,
      events: eventList,
      eventsDetail:
        eventList === null
          ? events.status === 'error' || events.status === 'stale'
            ? events.error.message
            : 'The event read did not complete.'
          : null,
    },
    fetchedAt: task.fetchedAt,
    error: task.status === 'stale' ? task.error : undefined,
  } as Result<AgentDetail>
}

/**
 * `_publish`'s return value, for the strategy this task actually chose.
 *
 * The three shapes are the worker's, not invented ones, INCLUDING the reason
 * strings -- those are what `PublishOutcome` falls back to matching when a task
 * carries no dispatch block, so a paraphrase here would exercise the fallback
 * against text production never produces.
 */
function fixtureGit(task: Task): Record<string, unknown> {
  const common = {
    base: 'd41f0c9a7b',
    commits: [
      {
        sha: '9b1c7f00aa', subject: 'Harden the destroy guard against an unreadable cluster',
        author: 'agent', committed_at: new Date().toISOString(),
        files_changed: 3, insertions: 61, deletions: 12, binary_files: 0,
      },
    ],
    commit_count: 1, insertions: 61, deletions: 12,
    dirty: [], patch: 'diff.patch', patch_bytes: 91233,
    repository: 'bogdan-alexandrescu/SwarmCloud',
  }
  const d = task.dispatch
  if (d?.strategy === 'collect' || d === undefined || d === null) {
    return {
      ...common,
      strategy: 'collect',
      published: false,
      publish_reason:
        "strategy is 'collect': the patch is harvested and nothing is pushed. " +
        "Submit with strategy 'direct-pr' to open a pull request from this agent",
    }
  }
  if (d.role === 'contributor') {
    return {
      ...common,
      role: 'contributor',
      default_branch: 'main', can_push: true,
      branch: `swarm/${task.id}`, pushed_head: '9b1c7f00aa', auto_committed: false,
      published: true,
      publish_reason:
        "strategy is 'integrate' and this step is a contributor: its branch was " +
        'pushed and no pull request was opened. The integrator step merges this ' +
        'branch and opens the single pull request for the whole workflow.',
    }
  }
  return {
    ...common,
    role: d.role,
    default_branch: 'main', can_push: true,
    branch: `swarm/${task.id}`, pushed_head: '9b1c7f00aa', auto_committed: false,
    published: true,
    pull_request: { number: 412, url: 'https://github.com/bogdan-alexandrescu/SwarmCloud/pull/412', state: 'open', created: true },
    ...(d.role === 'integrator'
      ? { integrated: { merged: d.integrates, conflicted: [], missing: [], complete: true } }
      : {}),
  }
}

async function fixtureAgentDetail(taskId: string): Promise<Result<AgentDetail>> {
  const page = await fixtureTasks()
  const task = page.status === 'ok' ? page.data.tasks.find((t) => t.id === taskId) : undefined
  if (!task) {
    return {
      status: 'error',
      error: {
        kind: 'not_found',
        httpStatus: 404,
        code: 'not_found',
        message: 'This task does not exist, or it belongs to another tenant.',
      },
    }
  }
  const at = (minsAgo: number) => new Date(Date.now() - minsAgo * 60_000).toISOString()
  // THE SAME ATTEMPT ID `fixtureAttempts` MINTS FOR ITS NEWEST ROW. It used to
  // be a differently-spelled id, so in development every event landed under
  // "an attempt no document describes" and the per-attempt joins -- checkpoint
  // sizes, live heartbeat readings, events grouped under their attempt -- were
  // the one part of the screen nobody ever saw working. A fixture that
  // exercises fewer code paths than production is a fixture that hides bugs.
  const attemptId = `att_${task.id.slice(-4)}_3`
  // `event_id` is a COUNTER, not the type: two heartbeats in one attempt are
  // ordinary and a type-derived id made React render duplicate keys the moment
  // this fixture grew a second one.
  let seq = 0
  const ev = (type: string, minsAgo: number, detail: Record<string, unknown> | null = null) => ({
    event_id: `${task.id}-${++seq}-${type}`,
    task_id: task.id,
    type,
    at: at(minsAgo),
    attempt_id: attemptId,
    lease_id: `lease-${task.id.slice(-4)}`,
    // The generation of the attempt these events belong to, matching
    // `fixtureAttempts`' newest row -- not the task's attempt counter, which is
    // a different number and made the timeline claim gen 1 under attempt 3.
    generation: 3,
    detail,
  })
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      task: {
        ...task,
        result_summary:
          task.state === 'SUCCEEDED'
            ? {
                artifacts: [
                  { name: 'report.md', bytes: 8241, uri: 'gs://swarm-artifacts-dev/u-bogdan/report.md' },
                  { name: 'diff.patch', bytes: 91233, uri: 'gs://swarm-artifacts-dev/u-bogdan/diff.patch' },
                ],
                artifact_bytes: 99474,
                // The git block the worker's `_publish` writes, DERIVED FROM
                // THIS TASK'S OWN STRATEGY rather than fixed. The publish
                // outcome and the dispatch are two renderings of one decision,
                // and a fixture that let them disagree would make the screen
                // look right while showing an impossible run.
                git: fixtureGit(task),
                logs: {
                  stdout: 'gs://swarm-artifacts-dev/u-bogdan/logs/stdout.log',
                  stderr: 'gs://swarm-artifacts-dev/u-bogdan/logs/stderr.log',
                },
                runner: {
                  usage: {
                    input_tokens: 8,
                    output_tokens: 402,
                    total_cost_usd: 0.0642028,
                    models: ['claude-opus-5'],
                  },
                },
              }
            : task.result_summary,
      },
      events: [
        ev('ready', 12),
        ev('lease_acquired', 10),
        // The workaround for having no attempts/leases read path: the
        // DISPATCHED event's detail carries the execution name.
        ev('dispatched', 9, { execution_name: 'swarm-job-u-bogdan-claude-code-abc12', backend: 'CLOUD_RUN_JOB' }),
        ev('starting', 8),
        ev('running', 8),
        // Every fifth heartbeat carries a live resource reading
        // (lifecycle._heartbeat). It is the ONLY per-attempt measurement a
        // running agent has: `peak_rss_bytes` on the attempt document is
        // written at the end.
        ev('heartbeat', 6, { elapsed_seconds: 120.4, peak_rss_bytes: 1_610_612_736, checkpoints: 1 }),
        // The worker writes all four keys (control.record_checkpoint). The
        // attempt document stores only the id, so the size and the uri exist
        // HERE and nowhere else.
        ev('checkpoint_completed', 4, {
          checkpoint_id: 'ckpt-00001',
          uri: 'gs://swarm-artifacts-dev/u-bogdan/checkpoints/ckpt-00001/workspace.tar.zst',
          size_bytes: 41_235_988,
          seq: 1,
        }),
        ev('heartbeat', 3, { elapsed_seconds: 300.1, peak_rss_bytes: 1_842_000_000, checkpoints: 2 }),
        // Deliberately NO matching event for ckpt-00002, which `fixtureAttempts`
        // lists on the attempt: that is the real paging case -- an id with no
        // size and no uri, which must render as "the event is off this page",
        // not as a blank cell.
        ...(task.state === 'SUCCEEDED' ? [ev('succeeded', 1, { exit_code: 0 })] : []),
        ...(task.state === 'FAILED' ? [ev('failed', 1, { exit_code: 1, error: task.last_error })] : []),
      ],
      eventsDetail: null,
    },
  }
}

/**
 * The resource-class catalogue: what a class is GIVEN.
 *
 * `GET /v1/resource-classes` serves `RESOURCE_CLASSES` from the frozen
 * contract. Added for exactly one screen -- the agent run detail, which
 * renders peak RSS and peak disk against the ceiling they were measured
 * under -- and it is a route rather than a table because a served value
 * cannot drift at all. (`check-contract-parity.sh` gained a TypeScript section
 * and now asserts the copies this client does keep; a copy of these five
 * numbers would still be a copy, edited in two places every time a class is
 * resized. The route is the better answer, not merely the safer one.)
 *
 * There is no empty case: the catalogue always has three classes, so a 200
 * with an empty object is a failure wearing a success code, not a platform
 * with no sizes.
 */
export type ResourceClasses = Record<string, ResourceClassSpec>

export async function loadResourceClasses(): Promise<Result<{ resource_classes: ResourceClasses }>> {
  if (USE_FIXTURES) return fixtureResourceClasses()
  return read<{ resource_classes: ResourceClasses }>(
    route('/v1/resource-classes'),
    (d) => Object.keys(d.resource_classes ?? {}).length === 0,
  )
}

/**
 * A task's checkpoints, across every attempt.
 *
 * `GET /v1/tasks/{id}/checkpoints`. Newest attempt first, newest checkpoint
 * first within an attempt -- which is the order a resume would consider them
 * in, because `CheckpointManager.find_latest` scans the whole task prefix.
 *
 * NO EMPTY PREDICATE IS PASSED, deliberately. `checkpoints: []` with
 * `listed: true` is a real answer -- this task has written none -- and the
 * server never returns it for a listing that failed: a failed listing is a
 * 503, which `read` classifies as an error. Collapsing the two into `empty`
 * here would throw away the distinction the endpoint was built to keep.
 *
 * Per-checkpoint failures survive INSIDE the rows: `manifest: 'unreadable'`
 * with `resumable: null` is a checkpoint nothing could be established about,
 * and it must not be drawn as one that cannot be resumed.
 */
export async function loadCheckpoints(
  taskId: string,
  options: { attemptId?: string; pageToken?: string; limit?: number } = {},
): Promise<Result<CheckpointsPage>> {
  if (USE_FIXTURES) return fixtureCheckpoints(taskId)
  const query = new URLSearchParams()
  if (options.attemptId) query.set('attempt_id', options.attemptId)
  if (options.pageToken) query.set('page_token', options.pageToken)
  if (options.limit) query.set('limit', String(options.limit))
  // The path is a literal of its own and the query is passed beside it,
  // rather than interpolated into the same template. That is not style: the
  // UI/API seam test in test_runtimes_screen.py scans this file for versioned
  // path literals and compares their SHAPE against the router's declarations,
  // and a trailing `${suffix}` inside the template normalises to a path
  // segment the API does not serve. Keeping them apart keeps the seam
  // checkable -- and `route()` keys the registry by the literal (CH-18).
  return read<CheckpointsPage>(route('/v1/tasks/{id}/checkpoints', { id: taskId }, query), () => false)
}

/**
 * A window of an attempt's stdout and stderr, already redacted by the server.
 *
 * `GET /v1/tasks/{id}/logs`. The API applies credential redaction at READ
 * time, unconditionally, because the worker's own pass replaces registered
 * literal values only and is skipped entirely for a run that registered none.
 * NOTHING IN THIS CLIENT MAY UNDO THAT, and nothing may render `content`
 * without reading `status` first: `null` means absent or unreadable, and `''`
 * means an object that exists and is empty.
 *
 * Paging is by RAW BYTE OFFSET (`next_offset`), not by line and not by the
 * length of the text returned -- redaction makes the text shorter than the
 * bytes it came from. Windows are aligned to whitespace by the server so a
 * credential can never be split across two of them.
 *
 * `stream` IS REPEATABLE (#184), and a list is sent as one `stream=` per
 * name, in order. No stream means `[stdout, stderr]` -- the RUNNER process's
 * streams, exactly as before -- so every existing caller is unchanged.
 * `agent_stdout` and `agent_stderr` are the agent CLI's own.
 */
export async function loadTaskLogs(
  taskId: string,
  options: {
    attemptId?: string
    stream?: LogStreamName | readonly LogStreamName[]
    source?: 'auto' | 'final' | 'live'
    offset?: number
    limitBytes?: number
  } = {},
): Promise<Result<TaskLogs>> {
  if (USE_FIXTURES) return fixtureTaskLogs(taskId, options.stream)
  const query = new URLSearchParams()
  if (options.attemptId) query.set('attempt_id', options.attemptId)
  const streams: readonly LogStreamName[] =
    options.stream === undefined ? [] : typeof options.stream === 'string' ? [options.stream] : options.stream
  for (const s of streams) query.append('stream', s)
  if (options.source) query.set('source', options.source)
  if (options.offset !== undefined) query.set('offset', String(options.offset))
  if (options.limitBytes !== undefined) query.set('limit_bytes', String(options.limitBytes))
  // Path literal and query kept apart; see loadCheckpoints above for why.
  return read<TaskLogs>(route('/v1/tasks/{id}/logs', { id: taskId }, query), () => false)
}

/**
 * THE CHECKPOINT FIXTURE EXERCISES THE ABSENCES, not the happy path.
 *
 * One checkpoint whose manifest is present and resumable, and one whose
 * manifest is `unreadable` with `resumable: null` -- the row that must not
 * render as "cannot be resumed". A fixture that only ever produced good rows
 * would let `resumable ?? false` ship looking correct in development, which is
 * exactly how the absent-measurement bugs in this product got in.
 */
async function fixtureCheckpoints(taskId: string): Promise<Result<CheckpointsPage>> {
  await new Promise((r) => setTimeout(r, 90))
  noteFixtureProbe(route('/v1/tasks/{id}/checkpoints', { id: taskId }), 90, true)
  const prefix = `tenants/u-bogdan/tasks/${taskId}/checkpoints`
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      task_id: taskId,
      tenant_id: 'u-bogdan',
      prefix,
      count: 2,
      total_found: 2,
      next_page_token: null,
      listed: true,
      truncated: false,
      latest_checkpoint: {
        pointer: `${prefix}/ckpt_0002`,
        status: 'present',
        checkpoint_id: 'ckpt_0002',
      },
      checkpoints: [
        {
          checkpoint_id: 'ckpt_0002',
          attempt_id: 'att_2',
          attempt_known: true,
          attempt_created_at: new Date(Date.now() - 9 * 60_000).toISOString(),
          attempt_completed_at: null,
          prefix: `${prefix}/ckpt_0002`,
          uri: `gs://swarm-workspaces/${prefix}/ckpt_0002`,
          is_latest_pointer: true,
          objects: [
            { name: 'manifest.json', key: `${prefix}/ckpt_0002/manifest.json`, bytes: 412 },
            { name: 'workspace.tar.zst', key: `${prefix}/ckpt_0002/workspace.tar.zst`, bytes: 18_442_240 },
          ],
          stored_bytes: 18_442_652,
          manifest: 'present',
          manifest_detail: null,
          created_at: new Date(Date.now() - 4 * 60_000).toISOString(),
          seq: 2,
          generation: 2,
          label: 'periodic',
          archive_bytes: 18_442_240,
          archive_sha256: '3b1f…c7a2',
          file_count: 184,
          resumable: true,
          resumable_detail: null,
        },
        {
          checkpoint_id: 'ckpt_0001',
          attempt_id: 'att_1',
          attempt_known: false,
          attempt_created_at: null,
          attempt_completed_at: null,
          prefix: `${prefix}/ckpt_0001`,
          uri: `gs://swarm-workspaces/${prefix}/ckpt_0001`,
          is_latest_pointer: false,
          objects: [
            { name: 'workspace.tar.zst', key: `${prefix}/ckpt_0001/workspace.tar.zst`, bytes: 9_102_336 },
          ],
          stored_bytes: 9_102_336,
          manifest: 'unreadable',
          manifest_detail: 'The object is there and did not parse as JSON.',
          created_at: null,
          seq: null,
          generation: 1,
          label: null,
          archive_bytes: null,
          archive_sha256: null,
          file_count: null,
          // NULL, NOT FALSE. "cannot tell" and "cannot resume" are different
          // sentences and the row renders them differently.
          resumable: null,
          resumable_detail: 'Nothing could be established without the manifest.',
        },
      ],
    },
  }
}

/**
 * THE LOG FIXTURE CARRIES ALL THREE CONTENT STATES.
 *
 * stdout has text and is a LIVE tail; stderr exists and is EMPTY (`''`, which
 * is a real answer about a quiet agent and must not render as a failed read).
 * The distinction between `null` and `''` is the whole reason `LogStream.content`
 * is nullable, and a fixture that never produced `''` would leave the branch
 * that renders it unexercised.
 *
 * THE AGENT STREAMS (#184) are answered only when asked for by name, as the
 * route does: `agent_stdout` is a live NDJSON tail published seconds ago, and
 * `agent_stderr` is a measured empty object -- the reference task's shape.
 */
async function fixtureTaskLogs(
  taskId: string,
  asked?: LogStreamName | readonly LogStreamName[],
): Promise<Result<TaskLogs>> {
  await new Promise((r) => setTimeout(r, 110))
  noteFixtureProbe(route('/v1/tasks/{id}/logs', { id: taskId }), 110, true)
  const body = [
    '[13:42:01] cloning https://github.com/saga-xyz/example.git',
    '[13:42:09] workspace ready at /workspace (tmpfs)',
    '[13:42:09] claude-code: starting',
    '[13:44:31] checkpoint ckpt_0002 written (18.4 MB)',
  ].join('\n')
  const names: readonly LogStreamName[] =
    asked === undefined ? ['stdout', 'stderr'] : typeof asked === 'string' ? [asked] : asked
  if (names.some((n) => n === 'agent_stdout' || n === 'agent_stderr')) {
    const agentOut = [
      '{"type":"system","subtype":"init","model":"claude-opus-5"}',
      '{"type":"assistant","message":{"content":[{"type":"text","text":"Reading the repository."}]}}',
    ].join('\n')
    const pick = (n: LogStreamName): LogStream => {
      const content = n === 'agent_stdout' ? agentOut : n === 'agent_stderr' ? '' : n === 'stdout' ? body : ''
      const size = new TextEncoder().encode(content).length
      return {
        stream: n,
        source: 'live',
        status: 'ok',
        detail: null,
        key: `tenants/u-bogdan/tasks/${taskId}/attempts/att_2/logs/live/${n}.tail.log`,
        uri: `gs://swarm-logs/tenants/u-bogdan/tasks/${taskId}/attempts/att_2/logs/live/${n}.tail.log`,
        content,
        total_bytes: size,
        offset: 0,
        returned_bytes: size,
        next_offset: null,
        truncated: false,
        redacted: false,
        redaction_count: 0,
        tail_window: size === 0 ? null : { object_offset: 0, stream_size: size, published_at: new Date(Date.now() - 3_000).toISOString() },
        object_updated_at: new Date(Date.now() - 3_000).toISOString(),
        age_seconds: 3,
      }
    }
    return {
      status: 'ok',
      fetchedAt: Date.now(),
      data: {
        task_id: taskId,
        tenant_id: 'u-bogdan',
        attempt_id: 'att_2',
        attempt: {
          status: 'latest',
          known: true,
          generation: 2,
          created_at: new Date(Date.now() - 9 * 60_000).toISOString(),
          completed_at: null,
          exit_code: null,
        },
        read_at: new Date().toISOString(),
        prefix: `tenants/u-bogdan/tasks/${taskId}/attempts/att_2/`,
        redaction: { applied_at_read_time: true, rules: 6 },
        streams: names.map(pick),
      },
    }
  }
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      task_id: taskId,
      tenant_id: 'u-bogdan',
      attempt_id: 'att_2',
      attempt: {
        status: 'latest',
        known: true,
        generation: 2,
        created_at: new Date(Date.now() - 9 * 60_000).toISOString(),
        completed_at: null,
        exit_code: null,
      },
      prefix: `tenants/u-bogdan/tasks/${taskId}/logs`,
      redaction: { applied_at_read_time: true, rules: 6 },
      streams: [
        {
          stream: 'stdout',
          source: 'live',
          status: 'ok',
          detail: null,
          key: `tenants/u-bogdan/tasks/${taskId}/logs/att_2.stdout`,
          uri: `gs://swarm-logs/tenants/u-bogdan/tasks/${taskId}/logs/att_2.stdout`,
          content: body,
          total_bytes: 4096,
          offset: 0,
          returned_bytes: body.length,
          next_offset: 4096,
          truncated: true,
          redacted: false,
          redaction_count: 0,
          tail_window: { object_offset: 0, stream_size: 4096 },
        },
        {
          stream: 'stderr',
          source: 'live',
          status: 'ok',
          detail: null,
          key: `tenants/u-bogdan/tasks/${taskId}/logs/att_2.stderr`,
          uri: `gs://swarm-logs/tenants/u-bogdan/tasks/${taskId}/logs/att_2.stderr`,
          // An object that exists and is empty. NOT the same as `null`.
          content: '',
          total_bytes: 0,
          offset: 0,
          returned_bytes: 0,
          next_offset: null,
          truncated: false,
          redacted: false,
          redaction_count: 0,
          tail_window: null,
        },
      ],
    },
  }
}

/**
 * ONE artifact's content, resolved by the SERVER from the task's manifest.
 *
 * `GET /v1/tasks/{id}/artifacts/content?name=`. The parameter is a NAME, not a
 * path: the server matches it for exact equality against the manifest the
 * worker wrote into this task's own `result_summary` and rebuilds the object
 * key from the task document. Nothing in this client may construct a key, a
 * prefix or a `gs://` uri and send it -- a route that accepted one would be a
 * path-traversal hole into another tenant's prefix, and the reason it does not
 * is that no caller can express one.
 *
 * `status` MUST be read before `content`. `null` is absent, unreadable or
 * binary; `''` is a real, empty artifact. And `truncated` must be surfaced: a
 * capped window rendered without it reads as the whole output, which is the
 * thing the server went out of its way to make visible.
 */
export async function loadArtifactContent(
  taskId: string,
  name: string,
  options: { offset?: number; limitBytes?: number } = {},
): Promise<Result<ArtifactContent>> {
  if (USE_FIXTURES) return fixtureArtifactContent(taskId, name)
  const query = new URLSearchParams({ name })
  if (options.offset !== undefined) query.set('offset', String(options.offset))
  if (options.limitBytes !== undefined) query.set('limit_bytes', String(options.limitBytes))
  // Path literal and query kept apart; see loadCheckpoints above for why.
  return read<ArtifactContent>(route('/v1/tasks/{id}/artifacts/content', { id: taskId }, query), () => false)
}

// ---------------------------------------------------------------------------
// The Artifacts tab (#184): what a task took in and what it produced
// ---------------------------------------------------------------------------
//
// EVERY READ AND EVERY DOWNLOAD GOES THROUGH THE API, never a signed GCS URL
// (the owner's decision, 2026-09-25), so read-time credential redaction and
// the tenant check apply to every byte. Nothing here builds an object key, a
// prefix or a `gs://` uri; a file is named by the NAME its manifest spells,
// on the task that owns it -- a staged input by the UPSTREAM task's id.

/**
 * One task document, on its own: `GET /v1/tasks/{id}`. The Artifacts pane
 * re-reads it every 5 s while the task runs, and the events, attempts and
 * catalogue `loadAgentRun` adds are nothing that pane draws.
 */
export async function loadTask(taskId: string): Promise<Result<Task>> {
  if (USE_FIXTURES) {
    const detail = await fixtureAgentDetail(taskId)
    return detail.status === 'ok' ? { status: 'ok', data: detail.data.task, fetchedAt: detail.fetchedAt } : (detail as Result<Task>)
  }
  const got = await read<{ task: Task } | Task>(route('/v1/tasks/{id}', { id: taskId }), () => false)
  if (got.status === 'loading' || got.status === 'error') return got
  if (got.status === 'empty') {
    return {
      status: 'error',
      error: {
        kind: 'not_found',
        httpStatus: 404,
        code: 'not_found',
        message: 'This task does not exist, or it belongs to another tenant.',
      },
    }
  }
  // The route wraps the document in `{task}`; see loadAgentDetail.
  const raw = got.data as { task?: Task } & Task
  const task: Task = raw.task ?? raw
  return got.status === 'stale'
    ? { status: 'stale', data: task, fetchedAt: got.fetchedAt, error: got.error }
    : { status: 'ok', data: task, fetchedAt: got.fetchedAt, serverAt: got.serverAt }
}

/**
 * HOW MANY ENTRIES THE ARTIFACTS PANE ASKS THE LISTING FOR: 200, which is
 * `max_page_size`, the server's own cap.
 *
 * WITHOUT IT THE LISTING WAS 50. The artifacts route goes through
 * `paged_limit` (routes/tasks.py) like every list route, so a request with no
 * `limit` got `default_page_size`. `store.list_artifacts` then returned
 * `manifest.artifacts[:50]` with `complete: true` and nothing saying it had
 * cut the list. A browser run that took 60 screenshots drew 50 rows and a
 * chip reading 50. The route returns no `next_page_token` and accepts none, so
 * one read goes no further than this. The pane lists anything past it from
 * the task's own manifest, which is the record the route serves, and it says
 * how many the route listed (Artifacts.tsx `Files`).
 *
 * This is its own constant, not `ATTEMPT_PAGE_LIMIT`, for the reason that one
 * gives: the two routes share a cap today, and nothing makes them share one.
 * A deployment that lowered `MAX_PAGE_SIZE` would clamp this read further.
 * The pane still catches that, because it counts against the manifest and
 * not against this number.
 */
export const ARTIFACT_PAGE_LIMIT = 200

/**
 * The artifact LISTING: `GET /v1/tasks/{id}/artifacts`. The manifest from the
 * task's own result summary -- no GCS call -- plus, since #184, each file's
 * `kind` and `role` from the server's one name table.
 *
 * No empty predicate: `artifacts: []` with `complete: true` is a real zero,
 * and with `complete: false` it is "uploaded when the attempt ends". The
 * screen tells the two apart by `complete`, which a collapse to `empty`
 * would throw away.
 *
 * `limit` IS SENT, AT `ARTIFACT_PAGE_LIMIT`. See that constant for what not
 * sending it cost.
 */
export async function loadArtifactListing(taskId: string): Promise<Result<ArtifactListing>> {
  if (USE_FIXTURES) return fixtureArtifactListing(taskId)
  return read<ArtifactListing>(
    route('/v1/tasks/{id}/artifacts', { id: taskId }, `limit=${ARTIFACT_PAGE_LIMIT}`),
    () => false,
  )
}

/**
 * THE URL OF ONE ARTIFACT'S BYTES, for an `<img src>`, an `open full` link or
 * a `download` link: `GET /v1/tasks/{id}/artifacts/raw?name=&disposition=`.
 *
 * A URL AND NOT A READ, on purpose. The response is a byte stream, and `read`
 * classifies any non-JSON 2xx as an expired session -- correctly, for a JSON
 * route. The browser fetches this itself, with the same-origin session cookie
 * every read already rides on, so the tenant check and read-time redaction
 * (for text) happen on the server exactly as for the content route. The name
 * is the manifest's, verbatim; the server resolves the object.
 */
export function artifactRawUrl(taskId: string, name: string, disposition: 'inline' | 'attachment'): string {
  return route('/v1/tasks/{id}/artifacts/raw', { id: taskId }, new URLSearchParams({ name, disposition })).url
}

/**
 * The agent's final answer: `GET /v1/tasks/{id}/answer`. See `TaskAnswer` for
 * the four statuses; a failed READ of this route is a `Result` error, which is
 * a fifth thing and is drawn as one.
 */
export async function loadAnswer(taskId: string, options: { attemptId?: string } = {}): Promise<Result<TaskAnswer>> {
  if (USE_FIXTURES) return fixtureAnswer(taskId)
  const query = new URLSearchParams()
  if (options.attemptId) query.set('attempt_id', options.attemptId)
  return read<TaskAnswer>(route('/v1/tasks/{id}/answer', { id: taskId }, query), () => false)
}

/**
 * The agent's transcript as steps, parsed and redacted by the server:
 * `GET /v1/tasks/{id}/transcript`.
 *
 * With no `offset` the server serves the window `source` chooses: from the
 * start of a final object, or the newest tail of a live one -- which is what
 * the 5 s live read asks for, replacing its list each time. A final
 * transcript is paged from `next_offset`. `includeRaw` adds each step's
 * redacted source record, for `show records`.
 */
export async function loadTranscript(
  taskId: string,
  options: {
    attemptId?: string
    source?: 'auto' | 'final' | 'live'
    offset?: number
    limitBytes?: number
    includeRaw?: boolean
  } = {},
): Promise<Result<TaskTranscript>> {
  if (USE_FIXTURES) return fixtureTranscript(taskId, options.includeRaw === true)
  const query = new URLSearchParams()
  if (options.attemptId) query.set('attempt_id', options.attemptId)
  if (options.source) query.set('source', options.source)
  if (options.offset !== undefined) query.set('offset', String(options.offset))
  if (options.limitBytes !== undefined) query.set('limit_bytes', String(options.limitBytes))
  if (options.includeRaw) query.set('include_raw', 'true')
  return read<TaskTranscript>(route('/v1/tasks/{id}/transcript', { id: taskId }, query), () => false)
}

/**
 * `GET /v1/workflows/{id}`: the workflow, its steps and every step's task.
 * The Artifacts pane reads it only for a task that IS a workflow step, to name
 * the step that produced each staged file and to see whether that upstream
 * run still lists it.
 */
export interface WorkflowRead {
  workflow: Workflow
  tasks: Task[]
}

export async function loadWorkflow(workflowId: string): Promise<Result<WorkflowRead>> {
  if (USE_FIXTURES) return fixtureWorkflowRead(workflowId)
  return read<WorkflowRead>(route('/v1/workflows/{id}', { id: workflowId }), () => false)
}

/**
 * THE ARTIFACTS FIXTURES, derived from the task fixture so the joins are real:
 * a finished task lists its files with the kinds and roles the server would
 * give them, and an unfinished one lists nothing, incomplete -- "uploaded when
 * the attempt ends", never "none".
 */
async function fixtureArtifactListing(taskId: string): Promise<Result<ArtifactListing>> {
  await new Promise((r) => setTimeout(r, 40))
  noteFixtureProbe(route('/v1/tasks/{id}/artifacts', { id: taskId }), 40, true)
  const detail = await fixtureAgentDetail(taskId)
  if (detail.status !== 'ok') return detail as Result<ArtifactListing>
  const finished = detail.data.task.state === 'SUCCEEDED'
  const uri = (name: string) => `gs://swarm-artifacts-dev/tenants/u-bogdan/tasks/${taskId}/attempts/att_fixture/artifacts/${name}`
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      task_id: taskId,
      complete: finished,
      attempt_id: finished ? 'att_fixture' : null,
      artifact_bytes: finished ? 118_823 : null,
      artifacts_skipped: [],
      artifacts: finished
        ? [
            { name: 'report.md', bytes: 8241, uri: uri('report.md'), attempt_id: 'att_fixture', kind: 'markdown', content_type: 'text/markdown', role: null },
            { name: 'diff.patch', bytes: 91233, uri: uri('diff.patch'), attempt_id: 'att_fixture', kind: 'text', content_type: 'text/plain', role: null },
            { name: 'claude-code.stdout.log', bytes: 9397, uri: uri('claude-code.stdout.log'), attempt_id: 'att_fixture', kind: 'log', content_type: 'text/plain', role: 'agent_stdout' },
            { name: 'claude-code.stderr.log', bytes: 0, uri: uri('claude-code.stderr.log'), attempt_id: 'att_fixture', kind: 'log', content_type: 'text/plain', role: 'agent_stderr' },
            { name: 'claude-transcript.json', bytes: 10181, uri: uri('claude-transcript.json'), attempt_id: 'att_fixture', kind: 'json', content_type: 'application/json', role: 'agent_transcript' },
          ]
        : [],
    },
  }
}

function fixtureAttemptBlock(finished: boolean): TaskAnswer['attempt'] {
  return {
    status: 'latest',
    known: true,
    generation: 1,
    created_at: new Date(Date.now() - 9 * 60_000).toISOString(),
    completed_at: finished ? new Date(Date.now() - 60_000).toISOString() : null,
    exit_code: finished ? 0 : null,
  }
}

async function fixtureAnswer(taskId: string): Promise<Result<TaskAnswer>> {
  await new Promise((r) => setTimeout(r, 50))
  noteFixtureProbe(route('/v1/tasks/{id}/answer', { id: taskId }), 50, true)
  const detail = await fixtureAgentDetail(taskId)
  if (detail.status !== 'ok') return detail as Result<TaskAnswer>
  const state = detail.data.task.state
  const finished = TERMINAL_STATES.has(state)
  const content =
    '## Summary\n\nThe capacity reservation is all-or-nothing across every pool.\n\n' +
    '- concurrency counts from `LEASED`\n- a stale worker exits without running the agent\n'
  const ok = state === 'SUCCEEDED'
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      task_id: taskId,
      tenant_id: 'u-bogdan',
      attempt_id: 'att_fixture',
      attempt: fixtureAttemptBlock(finished),
      read_at: new Date().toISOString(),
      status: ok ? 'ok' : finished ? 'absent' : 'not_yet',
      source: ok ? 'agent_result_event' : null,
      object: ok
        ? { stream: 'agent_stdout', source: 'final', uri: null, object_updated_at: new Date(Date.now() - 60_000).toISOString() }
        : null,
      format: ok ? 'markdown' : null,
      content: ok ? content : null,
      complete: ok ? true : null,
      is_error: ok ? false : null,
      subtype: ok ? 'success' : null,
      stop_reason: ok ? 'end_turn' : null,
      terminal_reason: ok ? 'completed' : null,
      num_turns: ok ? 7 : null,
      bytes: ok ? new TextEncoder().encode(content).length : null,
      redacted: false,
      redaction_count: 0,
      // What the server says of a capture that was not cut, as #188 sends it.
      capture_truncated: ok ? false : null,
      detail: null,
    },
  }
}

async function fixtureTranscript(taskId: string, includeRaw: boolean): Promise<Result<TaskTranscript>> {
  await new Promise((r) => setTimeout(r, 60))
  noteFixtureProbe(route('/v1/tasks/{id}/transcript', { id: taskId }), 60, true)
  const detail = await fixtureAgentDetail(taskId)
  if (detail.status !== 'ok') return detail as Result<TaskTranscript>
  const finished = TERMINAL_STATES.has(detail.data.task.state)
  const step = (n: number, over: Partial<TranscriptStep>): TranscriptStep => ({
    id: `L${n * 120}:0`,
    line_offset: n * 120,
    block: 0,
    kind: 'text',
    role: 'assistant',
    parent_tool_use_id: null,
    text: null,
    tool: null,
    tool_result: null,
    meta: null,
    truncated_fields: [],
    raw: includeRaw ? '{"type":"assistant"}' : null,
    ...over,
  })
  const steps: TranscriptStep[] = [
    step(0, { kind: 'init', role: 'system', meta: { model: 'claude-opus-5', permission_mode: 'bypassPermissions', tool_count: 14 } }),
    step(1, { text: 'Reading the repository before changing anything.' }),
    step(2, { kind: 'tool_call', tool: { id: 'toolu_01', name: 'Read', input: '{\n  "file_path": "CONTRACT.md"\n}' } }),
    step(3, { kind: 'tool_result', role: 'user', tool_result: { tool_use_id: 'toolu_01', is_error: false, content: '# The contract\n...', images: 0 } }),
    ...(finished
      ? [step(4, { kind: 'result', role: null, text: '## Summary\n\nDone.', meta: { subtype: 'success', is_error: false, num_turns: 7 } })]
      : []),
  ]
  const size = 4096
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      task_id: taskId,
      tenant_id: 'u-bogdan',
      attempt_id: 'att_fixture',
      attempt: fixtureAttemptBlock(finished),
      read_at: new Date().toISOString(),
      stream: {
        stream: 'agent_stdout',
        source: finished ? 'final' : 'live',
        status: 'ok',
        detail: null,
        uri: `gs://swarm-artifacts-dev/tenants/u-bogdan/tasks/${taskId}/attempts/att_fixture/logs/agent_stdout.log`,
        object_updated_at: new Date(Date.now() - 4_000).toISOString(),
        age_seconds: 4,
        total_bytes: size,
        offset: 0,
        returned_bytes: size,
        next_offset: null,
        truncated: false,
        tail_window: finished ? null : { object_offset: 0, stream_size: size, published_at: new Date(Date.now() - 4_000).toISOString() },
      },
      format: 'claude-stream-json',
      steps,
      complete: finished,
      window_starts_mid_stream: false,
      skipped_lines: 0,
      answer_in_window: finished,
      // Known whole only for a whole final read, as #188 decides it.
      capture_truncated: finished ? false : null,
      redaction: { applied_at_read_time: true, rules: 6 },
      redaction_count: 0,
    },
  }
}

async function fixtureWorkflowRead(workflowId: string): Promise<Result<WorkflowRead>> {
  await new Promise((r) => setTimeout(r, 80))
  noteFixtureProbe(route('/v1/workflows/{id}', { id: workflowId }), 80, true)
  const workflow = fixtureWorkflowRows().find((w) => w.workflow_id === workflowId)
  if (workflow === undefined) {
    return {
      status: 'error',
      error: { kind: 'not_found', httpStatus: 404, code: 'not_found', message: 'This workflow does not exist, or it belongs to another tenant.' },
    }
  }
  const page = await fixtureTasks()
  const tasks = page.status === 'ok' ? page.data.tasks.filter((t) => t.workflow_id === workflowId) : []
  return { status: 'ok', fetchedAt: Date.now(), data: { workflow, tasks } }
}

/**
 * STOP ONE AGENT. `POST /v1/tasks/{id}/cancel`.
 *
 * The only write in this product that ends work rather than shaping it, so the
 * caller confirms first -- see `StopRun.tsx`, which owns the confirmation and
 * is the only thing that calls this.
 *
 * `released_immediately` is the field that matters in the response and it is
 * NOT the HTTP status: a 200 means the request was recorded. False means the
 * task held capacity and keeps it until the worker acts on the flag, which is
 * a different thing to tell someone than "it stopped".
 */
export async function cancelTask(taskId: string): Promise<Result<CancelResult>> {
  if (USE_FIXTURES) return fixtureCancel(taskId)
  return write(route('/v1/tasks/{id}/cancel', { id: taskId }), 'POST') as Promise<
    Result<CancelResult>
  >
}

/**
 * ONLY WHAT THIS CLIENT READS.
 *
 * The response also carries the re-read `task`, and it is deliberately not
 * typed here: the stop control reloads the screen rather than trusting an echo,
 * so nothing consumes it -- and declaring a field nothing reads is what forced
 * the fixture below to fabricate a whole Task in order to satisfy a shape.
 * A fixture that invents data is the thing README.md says fixtures must not be.
 */
export interface CancelResult {
  /**
   * TRUE only when the task held no capacity and went straight to CANCELLED.
   * False means the flag is set and a live container is still running.
   */
  released_immediately: boolean
}

async function fixtureArtifactContent(
  taskId: string,
  name: string,
): Promise<Result<ArtifactContent>> {
  await new Promise((r) => setTimeout(r, 30))
  // The same `route()` call as the live path, so a fixture read lands in the
  // same registry record a live one would (CH-18).
  noteFixtureProbe(
    route('/v1/tasks/{id}/artifacts/content', { id: taskId }, new URLSearchParams({ name })),
    30,
    true,
  )
  const bodies: Record<string, string> = {
    'synthesis.md':
      '# Synthesis\n\nFive agents looked at the same question.\n\n' +
      '- cold start dominates the wall clock\n' +
      '- fencing held on every retry\n\n' +
      '```\nreserve(all_or_nothing)\n```\n\nSee `rollup.py` for the derivation.\n',
    'claude-transcript.json': JSON.stringify(
      [
        { type: 'system', subtype: 'init', model: 'claude-opus-4' },
        { type: 'assistant', message: { content: [{ type: 'text', text: 'Reading the repository.' }] } },
        { type: 'result', subtype: 'success', total_cost_usd: 0.0937 },
      ],
      null,
      2,
    ),
  }
  const content = bodies[name] ?? `fixture artifact ${name}\n`
  const bytes = new TextEncoder().encode(content).length
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      task_id: taskId,
      tenant_id: 'u-bogdan',
      attempt_id: 'att_fixture',
      artifact: { name, bytes, uri: `gs://swarm-artifacts/${name}` },
      status: 'ok',
      detail: null,
      key: `tenants/u-bogdan/tasks/${taskId}/attempts/att_fixture/artifacts/${name}`,
      uri: `gs://swarm-artifacts/${name}`,
      content,
      total_bytes: bytes,
      offset: 0,
      returned_bytes: bytes,
      next_offset: null,
      truncated: false,
      redacted: false,
      redaction_count: 0,
      redaction: { applied_at_read_time: true, rules: 11 },
    },
  }
}

async function fixtureCancel(taskId: string): Promise<Result<CancelResult>> {
  await new Promise((r) => setTimeout(r, 60))
  noteFixtureProbe(route('/v1/tasks/{id}/cancel', { id: taskId }), 60, true)
  // `released_immediately: false` is the fixture's answer on purpose: it is
  // the case the confirmation copy is written for, and a fixture that always
  // reported an instant stop would let that copy be developed against the
  // easier half.
  return { status: 'ok', fetchedAt: Date.now(), data: { released_immediately: false } }
}

async function fixtureResourceClasses(): Promise<Result<{ resource_classes: ResourceClasses }>> {
  await new Promise((r) => setTimeout(r, 40))
  noteFixtureProbe(route('/v1/resource-classes'), 40, true)
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    // The real three, so the requested-vs-utilised bars are exercised in
    // development rather than shipping having never been looked at.
    data: {
      resource_classes: {
        standard: { name: 'standard', cpu: 4, memory_gib: 8, disk_gib: 4, units: 1 },
        browser: { name: 'browser', cpu: 8, memory_gib: 16, disk_gib: 8, units: 2 },
        large: { name: 'large', cpu: 8, memory_gib: 32, disk_gib: 16, units: 4 },
      },
    },
  }
}

/**
 * THE RUNTIME CATALOGUE AND THE BACKENDS IT LANDS ON.
 *
 * THREE READS, AND THE FAILURES ARE NOT POOLED. Only `/v1/runtimes` can decide
 * there is no screen: it is the catalogue, and without it there is nothing to
 * draw. The other two each degrade to a `null` field with the reason beside it,
 * because a backend whose pool read failed and a backend with no ceiling
 * configured produce the same blank cell unless something keeps them apart --
 * which is the bug this whole UI exists to avoid.
 *
 * `/v1/capacity` supplies the `backend:*` pool counters, so the topology panel
 * can say how loaded each backend is rather than only that it exists.
 * `/v1/resource-classes` supplies the WHOLE sizing catalogue, including a class
 * no runtime references -- which `/v1/runtimes` cannot show, since it only ever
 * reports the class each runtime resolved to.
 */
export interface RuntimeTopology {
  /** Keyed by runner-profile name, exactly as a caller must spell it. */
  runtimes: Record<string, Runtime>
  /** null means the capacity read FAILED. An empty array means there are none. */
  pools: Pool[] | null
  poolsDetail: string | null
  /** null means the class-catalogue read failed or served nothing. */
  classes: ResourceClasses | null
  classesDetail: string | null
}

export async function loadRuntimeTopology(): Promise<Result<RuntimeTopology>> {
  if (USE_FIXTURES) return fixtureRuntimeTopology()

  const [runtimes, capacity, classes] = await Promise.all([
    read<{ runtimes: Record<string, Runtime> }>(
      route('/v1/runtimes'),
      (d) => Object.keys(d.runtimes ?? {}).length === 0,
    ),
    read<Capacity>(route('/v1/capacity'), () => false),
    loadResourceClasses(),
  ])

  if (runtimes.status !== 'ok' && runtimes.status !== 'stale') {
    return runtimes as Result<RuntimeTopology>
  }

  const poolsOk = capacity.status === 'ok' || capacity.status === 'stale'
  const classesOk = classes.status === 'ok' || classes.status === 'stale'

  return {
    status: runtimes.status,
    fetchedAt: runtimes.fetchedAt,
    data: {
      runtimes: runtimes.data.runtimes,
      pools: poolsOk ? capacity.data.pools : null,
      poolsDetail: poolsOk
        ? null
        : capacity.status === 'error'
          ? capacity.error.message
          : 'The capacity read did not complete.',
      classes: classesOk ? classes.data.resource_classes : null,
      classesDetail: classesOk
        ? null
        : classes.status === 'error'
          ? classes.error.message
          : classes.status === 'empty'
            ? 'The catalogue route answered with no classes at all, which a healthy API cannot do — every runtime resolves to one.'
            : 'The resource-class read did not complete.',
    },
    error: runtimes.status === 'stale' ? runtimes.error : undefined,
  } as Result<RuntimeTopology>
}

/**
 * THE ONE FIXTURE IN THIS FILE THAT DELIBERATELY IS NOT REAL, and the reason is
 * the whole point of the screen it feeds.
 *
 * README.md says "fixtures are real" and every other fixture here honours that.
 * This one must not. The shipped catalogue lives in `swarm_common.profiles`,
 * `check-contract-parity.sh` does not read TypeScript, and a fixture is a copy
 * nothing ever compares -- so pasting the real five profiles and their real
 * cpu/memory/disk/unit figures in here would plant exactly the drift the route
 * exists to remove, in the file that reads the route. `RESOURCE_UNITS` in
 * types.ts is that mistake already made once; it is recorded in
 * docs/contract-change-requests.md and is not being made twice.
 *
 * So these names are obviously invented, and they are shaped to exercise every
 * branch the screen has rather than to resemble production:
 *   * two distinct resolved backends, so the topology panel groups;
 *   * one runtime whose DECLARED backend is AUTO, so the declared/resolved
 *     split is drawn at all -- no shipped profile is AUTO, which means live
 *     data would never once exercise it;
 *   * one runtime needing no provider, one needing all of its credentials, one
 *     accepting any single one of several;
 *   * three sizes and a duplicated image, so "what sets this apart" has both
 *     unique and shared facts to report;
 *   * a class in `resource_classes` that no runtime uses, so the sizing panel's
 *     "nothing routes here" row is seen in development.
 */
const FIXTURE_RUNTIMES: Record<string, Runtime> = {
  'demo-scribe': {
    name: 'demo-scribe', image: 'demo-runtime-base',
    backend: 'CLOUD_RUN_JOB', resolved_backend: 'CLOUD_RUN_JOB',
    provider: 'demo-vendor', secrets: ['DEMO_API_KEY', 'DEMO_OAUTH_TOKEN'], secrets_any_of: true,
    timeout_seconds: 7200, resource_class: 'demo-small',
    resources: { name: 'demo-small', cpu: 3, memory_gib: 7, disk_gib: 3, units: 1 },
    available: true, disabled_reason: '',
  },
  'demo-probe': {
    name: 'demo-probe', image: 'demo-runtime-base',
    backend: 'CLOUD_RUN_JOB', resolved_backend: 'CLOUD_RUN_JOB',
    provider: null, secrets: [], secrets_any_of: false,
    timeout_seconds: 300, resource_class: 'demo-small',
    resources: { name: 'demo-small', cpu: 3, memory_gib: 7, disk_gib: 3, units: 1 },
    available: true, disabled_reason: '',
  },
  'demo-viewport': {
    name: 'demo-viewport', image: 'demo-runtime-viewport',
    backend: 'GKE_AUTOPILOT', resolved_backend: 'GKE_AUTOPILOT',
    provider: 'demo-vendor', secrets: ['DEMO_API_KEY'], secrets_any_of: false,
    timeout_seconds: 5400, resource_class: 'demo-medium',
    resources: { name: 'demo-medium', cpu: 6, memory_gib: 15, disk_gib: 7, units: 2 },
    // THE DISABLED EXEMPLAR. A fixture set where every profile is available
    // means the disabled branch of the catalogue screen ships unexercised --
    // the same argument `demo-wide` makes for declaring AUTO, since nothing in
    // the shipped catalogue is AUTO either.
    available: false,
    disabled_reason: 'demo-vendor refused the registered credential. Use demo-small.',
  },
  'demo-wide': {
    name: 'demo-wide', image: 'demo-runtime-wide',
    // Declared AUTO, resolved by the platform. Nothing in the shipped
    // catalogue is AUTO, so without this the split ships unexercised.
    backend: 'AUTO', resolved_backend: 'CLOUD_RUN_JOB',
    provider: 'other-vendor', secrets: ['OTHER_API_KEY', 'OTHER_REGION'], secrets_any_of: false,
    timeout_seconds: 10800, resource_class: 'demo-wide',
    resources: { name: 'demo-wide', cpu: 7, memory_gib: 30, disk_gib: 15, units: 4 },
    available: true, disabled_reason: '',
  },
}

async function fixtureRuntimeTopology(): Promise<Result<RuntimeTopology>> {
  await new Promise((r) => setTimeout(r, 60))
  noteFixtureProbe(route('/v1/runtimes'), 60, true)
  const capacity = await fixtureCapacity()
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      runtimes: FIXTURE_RUNTIMES,
      pools: capacity.status === 'ok' ? capacity.data.pools : null,
      poolsDetail:
        capacity.status === 'ok' ? null : 'The fixture capacity read did not complete.',
      classes: Object.fromEntries(
        // Every class the invented runtimes resolve to, plus one nothing routes
        // to -- the row the sizing panel has to be able to draw.
        [...Object.values(FIXTURE_RUNTIMES).map((r) => [r.resources.name, r.resources] as const),
         ['demo-idle', { name: 'demo-idle', cpu: 1, memory_gib: 2, disk_gib: 1, units: 1 }] as const],
      ),
      classesDetail: null,
    },
  }
}

/**
 * ONE AGENT RUN, COMPLETE: the task, every attempt, every event, and the
 * ceilings the attempts' measurements should be read against.
 *
 * Four reads, and THE FAILURES ARE NOT POOLED. Only the task read can decide
 * there is no screen; the other three each degrade to a `null` field with the
 * reason beside it, because a failed attempts query and a task that has never
 * been admitted produce the same empty panel unless something keeps them
 * apart -- which is the bug this whole UI exists to avoid.
 *
 * `attempts: []` therefore means the query SUCCEEDED and the task has no
 * attempt document (QUEUED, PARKED, or READY and waiting for capacity).
 * `attempts: null` means the query failed and nothing may be concluded.
 */
export interface AgentRun {
  task: Task
  /** null means the event read FAILED. An empty array means there are none. */
  events: TaskEvent[] | null
  eventsDetail: string | null
  /** null means the attempt read FAILED. An empty array means none exist. */
  attempts: AttemptRow[] | null
  attemptsDetail: string | null
  /** null means the catalogue read FAILED, so no "requested" side may be drawn. */
  classes: ResourceClasses | null
  /** Why the catalogue is missing, and whether the route exists at all. */
  classesDetail: string | null
  /** True when the API did not recognise /v1/resource-classes -- a deployment
   *  older than the route, which is a different fix from a failed read. */
  classesRouteMissing: boolean
}

export async function loadAgentRun(taskId: string): Promise<Result<AgentRun>> {
  if (USE_FIXTURES) return fixtureAgentRun(taskId)

  const id = { id: taskId }
  const [task, events, attempts, classes] = await Promise.all([
    read<{ task: Task } | Task>(route('/v1/tasks/{id}', id), () => false),
    read<{ events: TaskEvent[] }>(route(`/v1/tasks/{id}/events?limit=${EVENT_PAGE_LIMIT}`, id), () => false),
    // `include=usage` (#184): each attempt row gains its CPU reading, found by
    // the SERVER in one descending events read -- not in the page above, which
    // is oldest-first and capped at 200, so on a long run it misses exactly the
    // final reading. Opt-in, so the Overview's per-task attempts reads keep
    // their cost. An API older than the change ignores the parameter and sends
    // no `usage`, which the CPU rows draw as "not served", never as zero.
    read<{ attempts: AttemptRow[] }>(
      route(`/v1/tasks/{id}/attempts?limit=${ATTEMPT_PAGE_LIMIT}&include=usage`, id),
      (d) => d.attempts.length === 0,
    ),
    loadResourceClasses(),
  ])

  if (task.status === 'loading' || task.status === 'error') return task
  if (task.status === 'empty') {
    return {
      status: 'error',
      error: {
        kind: 'not_found',
        httpStatus: 404,
        code: 'not_found',
        message: 'This task does not exist, or it belongs to another tenant.',
      },
    }
  }

  // GET /v1/tasks/{id} returns {"task": {...}}, not the bare document. The
  // fallback stays because create and cancel return the same wrapper and a
  // caller could route through either.
  const raw = task.data as { task?: Task } & Task
  const unwrapped: Task = raw.task ?? raw

  const eventList = events.status === 'ok' ? events.data.events : events.status === 'empty' ? [] : null
  // `empty` is a real answer here and collapses to [], which is NOT the same
  // as the null a failure produces. The screen prints two different sentences.
  const attemptList =
    attempts.status === 'ok' ? attempts.data.attempts
    : attempts.status === 'stale' ? attempts.data.attempts
    : attempts.status === 'empty' ? []
    : null

  const classesOk = classes.status === 'ok' || classes.status === 'stale'

  return {
    status: task.status === 'stale' ? 'stale' : 'ok',
    data: {
      task: unwrapped,
      events: eventList,
      eventsDetail:
        eventList === null
          ? events.status === 'error' || events.status === 'stale'
            ? events.error.message
            : 'The event read did not complete.'
          : null,
      attempts: attemptList,
      attemptsDetail:
        attemptList === null
          ? attempts.status === 'error'
            ? attempts.error.message
            : 'The attempt read did not complete.'
          : null,
      classes: classesOk ? classes.data.resource_classes : null,
      classesDetail: classesOk
        ? null
        : classes.status === 'error'
          ? classes.error.message
          : classes.status === 'empty'
            ? 'The catalogue route returned no classes, which cannot happen for a healthy API — the frozen catalogue always has three.'
            : 'The resource-class read did not complete.',
      classesRouteMissing: classes.status === 'error' && classes.error.kind === 'not_found',
    },
    fetchedAt: task.fetchedAt,
    error: task.status === 'stale' ? task.error : undefined,
  } as Result<AgentRun>
}

async function fixtureAgentRun(taskId: string): Promise<Result<AgentRun>> {
  const [detail, attempts, classes] = await Promise.all([
    fixtureAgentDetail(taskId),
    fixtureAttempts(taskId),
    fixtureResourceClasses(),
  ])
  if (detail.status !== 'ok') return detail as Result<AgentRun>
  return {
    status: 'ok',
    fetchedAt: detail.fetchedAt,
    data: {
      task: detail.data.task,
      events: detail.data.events,
      eventsDetail: detail.data.eventsDetail,
      attempts: attempts.status === 'ok' ? attempts.data.attempts.map(fixtureUsage) : null,
      attemptsDetail: attempts.status === 'ok' ? null : 'The fixture attempt read did not complete.',
      classes: classes.status === 'ok' ? classes.data.resource_classes : null,
      classesDetail: null,
      classesRouteMissing: false,
    },
  }
}

/**
 * The CPU reading `attempts?include=usage` would add to one fixture attempt:
 * `never_ran` for one that never started, the reaped runner's `final` reading
 * for one that ended, and a `live` periodic reading for one still running --
 * so all three shapes of the Details CPU rows are looked at in development.
 */
function fixtureUsage(a: AttemptRow): AttemptRow {
  const base = {
    detail: null,
    cpu_source: 'cgroup',
    cpu_limit_cores: 2,
    cpu_limit_source: 'cgroup' as const,
    peak_rss_bytes: a.peak_rss_bytes,
  }
  if (a.started_at === null) {
    return {
      ...a,
      usage: {
        ...base,
        status: 'never_ran',
        event_id: null,
        measured_at: null,
        age_seconds: null,
        final: null,
        cpu_seconds: null,
        peak_cpu_cores: null,
        mean_cpu_cores: null,
        cpu_wall_seconds: null,
        cpu_limit_cores: null,
        cpu_limit_source: null,
      },
    }
  }
  const ended = a.completed_at !== null
  return {
    ...a,
    usage: {
      ...base,
      status: ended ? 'final' : 'live',
      event_id: `ev_usage_${a.attempt_id}`,
      measured_at: new Date(Date.now() - (ended ? 60_000 : 20_000)).toISOString(),
      age_seconds: ended ? 60 : 20,
      final: ended,
      cpu_seconds: 402.311,
      peak_cpu_cores: 1.62,
      mean_cpu_cores: 0.842,
      cpu_wall_seconds: 477.8,
    },
  }
}

export async function loadStats(): Promise<Result<Stats>> {
  if (USE_FIXTURES) return fixtureStats()
  // A successful read always yields twelve numbers, because count_tasks_by_state
  // iterates the whole enum and writes a key for each. So there is no empty
  // state here -- and an error must never render as "0 RUNNING", because
  // "0 RUNNING" and "stats failed" are opposite facts.
  return read<Stats>(route('/v1/stats'), () => false)
}

export async function loadDispatchControl(): Promise<Result<DispatchControl>> {
  if (USE_FIXTURES) return fixtureDispatch()
  // Admin-gated. A 403 here is information, not a failure.
  return read<DispatchControl>(route('/v1/admin/dispatch'), () => false)
}

export async function loadProviders(): Promise<Result<ProvidersPage>> {
  if (USE_FIXTURES) return fixtureProviders()
  return read<ProvidersPage>(route('/v1/providers'), (d) => d.providers.length === 0)
}

/** One page of tasks in one state. Server-side filter, backed by a real index. */
export async function loadTasksInState(state: TaskState): Promise<Result<TaskPage>> {
  if (USE_FIXTURES) {
    const page = await fixtureTasks()
    if (page.status !== 'ok') return page
    const tasks = page.data.tasks.filter((t) => t.state === state)
    return tasks.length === 0
      ? { status: 'empty', fetchedAt: Date.now() }
      : { status: 'ok', fetchedAt: Date.now(), data: { ...page.data, tasks } }
  }
  return read<TaskPage>(
    route('/v1/tasks', {}, new URLSearchParams({ state, limit: '200' })),
    (d) => d.tasks.length === 0,
  )
}

async function fixtureStats(): Promise<Result<Stats>> {
  await new Promise((r) => setTimeout(r, 220))
  noteFixtureProbe(route('/v1/stats'), 220, true)
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      tenant_id: 'u-bogdan',
      dispatch_paused: false,
      tasks_by_state: {
        SUBMITTED: 0, QUEUED: 0, PARKED: 1, READY: 1, LEASED: 1, DISPATCHED: 1,
        STARTING: 1, RUNNING: 2, SUCCEEDED: 7, FAILED: 1, CANCELLED: 1,
        DEAD_LETTERED: 0,
      },
      limits: { max_batch_size: 100, max_input_bytes: 65536, max_workflow_steps: 50 },
      generated_at: new Date().toISOString(),
    },
  }
}

async function fixtureDispatch(): Promise<Result<DispatchControl>> {
  await new Promise((r) => setTimeout(r, 120))
  // A non-admin genuinely cannot read this, and the board must render that as
  // information rather than breakage. The fixture exercises that path.
  noteFixtureProbe(route('/v1/admin/dispatch'), 120, false)
  return {
    status: 'error',
    error: {
      kind: 'admin_required',
      httpStatus: 403,
      code: 'forbidden',
      message: 'admin group membership is required',
    },
  }
}

async function fixtureProviders(): Promise<Result<ProvidersPage>> {
  await new Promise((r) => setTimeout(r, 200))
  noteFixtureProbe(route('/v1/providers'), 200, true)
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      tenant_id: 'u-bogdan',
      generated_at: new Date().toISOString(),
      providers: [
        {
          provider: 'anthropic',
          credential_registered: true,
          runner_profiles: ['browser', 'claude-code'],
          quota: {
            provider: 'anthropic', tenant_id: 'u-bogdan', state: 'THROTTLED',
            updated_at: new Date(Date.now() - 40_000).toISOString(),
            configured_hard_max: 50, adaptive_target: 6, quota_derived_limit: null,
            requests_remaining: 118, tokens_remaining: null,
            reset_at: new Date(Date.now() + 900_000).toISOString(),
            cooldown_until: null,
            last_429_at: new Date(Date.now() - 240_000).toISOString(),
            retry_after_seconds: null, success_count: 412, rate_limit_count: 9,
            effective_limit: 6,
          },
        },
        {
          provider: 'openai',
          credential_registered: false,
          runner_profiles: ['codex'],
          // No quota document for this tenant yet. UNKNOWN, not zeros.
          quota: null,
        },
      ],
    },
  }
}

/**
 * `GET /v1/tenants/me`. `frame` marks the product header's own read, which is
 * the FRAME's and not the screen's: it lands in the tab-wide registry like any
 * read, and the head's "newest read" -- the screen's own (CH-2) -- does not
 * count it. Platform counts reads the same route as a screen, without it.
 */
export async function loadMe(options: { frame?: boolean } = {}): Promise<Result<Me>> {
  if (USE_FIXTURES) return fixtureMe(options)
  return read<Me>(route('/v1/tenants/me'), () => false, { frame: options.frame === true })
}

/**
 * A row-bounded window, assembled by following page tokens.
 *
 * NEVER LET A CLAMP READ AS AN END OF DATA. `paged_limit` silently takes
 * min(requested, 200), so a client asking for limit=500 gets 200 rows and a
 * next_page_token with no error and no warning. The only honest end-of-data
 * signal is a null next_page_token, so that -- not a short page -- is what
 * stops this loop.
 */
export async function loadTaskWindow(budget: number): Promise<Result<TaskWindow>> {
  if (USE_FIXTURES) return fixtureWindow(budget)

  const tasks: Task[] = []
  let token: string | null = null
  let pages = 0
  let moreExist = false

  while (tasks.length < budget) {
    const qs = new URLSearchParams({ limit: '200' })
    if (token) qs.set('page_token', token)
    const page: Result<TaskPage> = await read<TaskPage>(route('/v1/tasks', {}, qs), () => false)
    if (page.status === 'error') {
      // Partial data plus a failure is still a failure to describe a window:
      // a span computed from half the rows would be wrong in a way nothing on
      // screen could reveal.
      if (tasks.length === 0) return page
      moreExist = true
      break
    }
    if (page.status === 'loading') break
    pages++
    if (page.status === 'empty') break
    if (page.status === 'ok' || page.status === 'stale') {
      tasks.push(...page.data.tasks)
      token = page.data.next_page_token ?? null
      if (!token) break
    }
  }
  if (token) moreExist = true

  const trimmed = tasks.slice(0, budget)
  const times = trimmed
    .map((t) => t.created_at)
    .filter(Boolean)
    .sort()
  return {
    status: trimmed.length === 0 ? 'empty' : 'ok',
    fetchedAt: Date.now(),
    data: {
      tasks: trimmed,
      moreExist: moreExist || trimmed.length < tasks.length,
      pages,
      from: times[0] ?? null,
      to: times[times.length - 1] ?? null,
    },
  } as Result<TaskWindow>
}

async function fixtureMe(options: { frame?: boolean } = {}): Promise<Result<Me>> {
  await new Promise((r) => setTimeout(r, 150))
  noteFixtureProbe(route('/v1/tenants/me'), 150, true, undefined, { frame: options.frame === true })
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      tenant: {
        tenant_id: 'u-bogdan', kind: 'user', principal: 'bogdan@saga.xyz',
        display_name: 'Bogdan', created_at: new Date(Date.now() - 86_400_000 * 9).toISOString(),
        max_active: 2, capacity_units: 40, monthly_budget_usd: null, enabled: true,
        credentials: ['anthropic'], service_account: 'swarm-agent-u-bogdan@saga-agents-staging.iam.gserviceaccount.com',
        gcs_prefix: 'gs://swarm-artifacts-dev/u-bogdan', namespace: 'swarm-u-bogdan',
      },
      principal: {
        email: 'bogdan@saga.xyz', domain: 'saga.xyz',
        groups: ['eng@saga.xyz'], is_admin: false,
      },
      // UNDECLARED, ON PURPOSE. There is no API behind the fixtures, so there
      // is no environment it runs as; this is the shape of an API whose
      // ENVIRONMENT is unset (the frozen default fills in "dev"). Brand.tsx
      // then falls back to the build and the host, and a laptop reads Local.
      environment: 'dev',
      environment_declared: false,
    },
  }
}

async function fixtureWindow(budget: number): Promise<Result<TaskWindow>> {
  const page = await fixtureTasks()
  if (page.status !== 'ok') return page as Result<TaskWindow>
  const tasks = page.data.tasks.slice(0, budget)
  const times = tasks.map((t) => t.created_at).sort()
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      tasks,
      moreExist: true,
      pages: 1,
      from: times[0] ?? null,
      to: times[times.length - 1] ?? null,
    },
  }
}

/** `GET /v1/admin/tenants`, admin-gated. A 403 here is information. */
export async function loadTenants(): Promise<Result<{ tenants: Tenant[] }>> {
  if (USE_FIXTURES) return fixtureTenants()
  return read<{ tenants: Tenant[] }>(route('/v1/admin/tenants'), (d) => d.tenants.length === 0)
}

async function fixtureTenants(): Promise<Result<{ tenants: Tenant[] }>> {
  await new Promise((r) => setTimeout(r, 140))
  noteFixtureProbe(route('/v1/admin/tenants'), 140, false)
  return {
    status: 'error',
    error: {
      kind: 'admin_required',
      httpStatus: 403,
      code: 'forbidden',
      message: 'admin group membership is required',
    },
  }
}

/**
 * Unreleased leases. The freshest failure signal on the platform: it sees a
 * silent worker up to five minutes before the reconciler acts on it.
 *
 * Admin-gated, so a 403 here renders as information.
 */
export async function loadLeases(): Promise<Result<LeasePage>> {
  if (USE_FIXTURES) return fixtureLeases()
  return read<LeasePage>(route('/v1/admin/leases?active_only=true&limit=200'), (d) => d.leases.length === 0)
}

/** Every attempt of one task, newest first. Tenant-scoped. */
export async function loadAttempts(taskId: string): Promise<Result<{ attempts: AttemptRow[] }>> {
  if (USE_FIXTURES) return fixtureAttempts(taskId)
  return read<{ attempts: AttemptRow[] }>(
    route(`/v1/tasks/{id}/attempts?limit=${ATTEMPT_PAGE_LIMIT}`, { id: taskId }),
    (d) => d.attempts.length === 0,
  )
}

async function fixtureLeases(): Promise<Result<LeasePage>> {
  await new Promise((r) => setTimeout(r, 180))
  noteFixtureProbe(route('/v1/admin/leases'), 180, true)
  const iso = (secAgo: number) => new Date(Date.now() - secAgo * 1000).toISOString()
  const row = (
    id: string,
    silent: number,
    extra: Partial<LeaseRow> = {},
  ): LeaseRow => ({
    lease_id: id,
    task_id: `task_${id}`,
    attempt_id: `att_${id}`,
    tenant_id: 'u-bogdan',
    generation: 1,
    pools: ['global', 'tenant:u-bogdan'],
    units: 1,
    dispatch_state: 'DISPATCHED',
    created_at: iso(600),
    dispatch_deadline: iso(300),
    expires_at: iso(-120),
    heartbeat_at: iso(silent),
    released_at: null,
    release_reason: null,
    released: false,
    expired: false,
    dispatch_overdue: false,
    silent_seconds: silent,
    heartbeat_ever: true,
    last_error: null,
    ...extra,
  })
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      // One of each classification, so all three treatments are visible while
      // working on this screen rather than only the happy one.
      leases: [
        row('d1', 400, { expired: true, expires_at: iso(60) }),
        row('c1', 140),
        row('b1', 20),
        row('a1', 0, {
          dispatch_state: 'LEASED',
          dispatch_overdue: true,
          heartbeat_ever: false,
          heartbeat_at: null,
          silent_seconds: 480,
          last_error: 'BACKEND_REJECTED (attempt att_a1)',
        }),
      ],
      thresholds: { heartbeat_grace_seconds: 90, lease_timeout_seconds: 120 },
      evaluated_at: new Date().toISOString(),
      active_only: true,
      tenant_id: null,
      units_held: 4,
      // Every live lease is in the four rows above, so the drift check on the
      // Holders screen is evidence -- the shape `/v1/admin/leases` serves for
      // a fleet smaller than its limit.
      active_beyond_window: 0,
      truncated: false,
      examined: 4,
    },
  }
}

async function fixtureAttempts(taskId: string): Promise<Result<{ attempts: AttemptRow[] }>> {
  await new Promise((r) => setTimeout(r, 160))
  noteFixtureProbe(route('/v1/tasks/{id}/attempts', { id: taskId }), 160, true)
  const iso = (m: number) => new Date(Date.now() - m * 60_000).toISOString()
  // THE NEWEST ATTEMPT FOLLOWS THE TASK'S STATE. A fixture that always returned
  // a finished attempt meant the live-agent path -- no exit code, no
  // `peak_rss_bytes` yet, the resource figure coming off the newest heartbeat
  // event instead -- was never once looked at in development, and that is the
  // path someone opens this screen for when they are worried.
  const page = await fixtureTasks()
  const task = page.status === 'ok' ? page.data.tasks.find((t) => t.id === taskId) : undefined
  const live = task !== undefined && !TERMINAL_STATES.has(task.state)
  const mk = (n: number, exit: number | null, extra: Partial<AttemptRow> = {}): AttemptRow => ({
    attempt_id: `att_${taskId.slice(-4)}_${n}`,
    task_id: taskId,
    tenant_id: 'u-bogdan',
    generation: n,
    lease_id: `lease_${n}`,
    backend: 'CLOUD_RUN_JOB',
    execution_name: `swarm-job-u-bogdan-claude-code-${n}`,
    // PRODUCTION SHAPE: a started attempt's `created_at` IS its start. The
    // worker's `record_attempt_start` rewrites the scheduler's document with a
    // non-merge `.set()` (control.py), so admission survives only as the
    // `lease_acquired` event. The never-started attempt below overrides this
    // with its admission, because its document was never rewritten.
    created_at: iso(39 - n * 10),
    started_at: iso(39 - n * 10),
    completed_at: exit === null ? null : iso(35 - n * 10),
    exit_code: exit,
    error: exit ? 'exit status 1' : null,
    peak_rss_bytes: 1_842_000_000,
    peak_disk_bytes: null,
    oom_near_miss: n === 2,
    checkpoints: [],
    input_tokens: null,
    output_tokens: null,
    cache_read_input_tokens: null,
    cache_creation_input_tokens: null,
    cost_usd: null,
    ...extra,
  })
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      attempts: [
        // The newest attempt carries the measurements a current worker image
        // records, so the requested-vs-utilised bars and the spend columns are
        // exercised in development. `ckpt-00002` deliberately has no
        // `checkpoint_completed` event in fixtureAgentDetail: an id with no
        // size and no uri is the ordinary paging case, not a defect.
        mk(3, live ? null : 0, {
          checkpoints: ['ckpt-00001', 'ckpt-00002'],
          // THE NEWEST ATTEMPT HOLDS THE TASK'S LEASE when the task holds one.
          // `attemptEnd` reads an attempt as over once the task stops holding
          // its lease (a park, a failed dispatch, a reclaim), so a fixture
          // whose lease ids never matched would show every live agent in
          // development as ended.
          lease_id: task?.current_lease_id ?? 'lease_3',
          // A live attempt has written NO final measurement and no spend: both
          // are written when it ends. The screen falls back to the heartbeat
          // for memory and says five different things about the rest.
          ...(live
            ? { completed_at: null, peak_rss_bytes: null, peak_disk_bytes: null }
            : {
                peak_disk_bytes: 2_147_483_648,
                input_tokens: 18_422,
                output_tokens: 7_311,
                cache_read_input_tokens: 412_880,
                cache_creation_input_tokens: 21_004,
                cost_usd: 0.418_2,
              }),
        }),
        // The older two ran before usage capture landed: every spend figure is
        // null, which must render as em dashes and never as $0.00.
        mk(2, 1, { peak_rss_bytes: 7_730_941_132 }),
        // Never started: dispatch was rejected, so there is no exit code and no
        // measurement at all. It must not read as "exit 0" or as 0 bytes.
        mk(1, null, {
          peak_rss_bytes: null,
          created_at: iso(30),
          started_at: null,
          completed_at: null,
          execution_name: null,
          error: 'BACKEND_REJECTED',
        }),
      ],
    },
  }
}

/**
 * Leases plus the pool counters they should agree with.
 *
 * Two reads, because the drift check needs both sides. The LEASE read decides
 * whether there is a screen; a failed pool read leaves the holder table fully
 * trustworthy and only the comparison unavailable, so it degrades to `pools:
 * null` with the reason rather than failing the screen.
 */
export interface HoldersBoard {
  page: LeasePage
  /** null means the pool read FAILED. An empty array means there are none. */
  pools: Pool[] | null
  poolsDetail: string | null
}

export async function loadHolders(): Promise<Result<HoldersBoard>> {
  if (USE_FIXTURES) return fixtureHolders()

  const [leases, capacity] = await Promise.all([
    // NEVER `empty` FROM THE LEASE READ ALONE -- see `holdersAreEmpty`. The
    // page comes back as a page, and whether it is a real zero is decided
    // below with the counters in hand.
    read<LeasePage>(route('/v1/admin/leases?active_only=true&limit=200'), () => false),
    read<Capacity>(route('/v1/capacity'), () => false),
  ])

  if (leases.status === 'loading' || leases.status === 'error' || leases.status === 'empty') {
    return leases as Result<HoldersBoard>
  }

  const pools = capacity.status === 'ok' || capacity.status === 'stale' ? capacity.data.pools : null
  const detail =
    pools === null
      ? capacity.status === 'error' || capacity.status === 'stale'
        ? capacity.error.message
        : 'The pool read did not complete.'
      : null

  if (leases.status === 'ok' && holdersAreEmpty(leases.data, pools)) {
    return { status: 'empty', fetchedAt: leases.fetchedAt, serverAt: leases.serverAt }
  }

  return {
    status: leases.status,
    fetchedAt: leases.fetchedAt,
    data: { page: leases.data, pools, poolsDetail: detail },
  } as Result<HoldersBoard>
}

/**
 * WHETHER THE HOLDERS SCREEN MAY SAY "NOTHING HOLDS CAPACITY" (CP-7, visual QA
 * 2026-09-25).
 *
 * `empty` is drawn as a REAL ZERO, and it is two claims, not one: no live
 * lease exists, AND no pool counter is holding units. The second is the one
 * that matters most here -- a counter left non-zero with nothing behind it is
 * exactly the leak the drift card exists to find -- and it used to be dropped:
 * the capacity read was issued alongside the lease read and then discarded
 * whenever the lease page came back with no rows, so a leaked counter with
 * zero live leases rendered as the calmest thing on the screen.
 *
 * So `empty` needs BOTH, measured: a lease page that vouches it left no live
 * lease out (`active_beyond_window === 0`, not truncated -- an older API that
 * cannot say goes to the screen as a page, where the coverage is marked
 * unreported), and a counter read that succeeded with every `active` at 0.
 * A counter read that FAILED is not a zero either: the page goes to the screen,
 * where the drift card says the comparison was not made.
 */
function holdersAreEmpty(page: LeasePage, pools: Pool[] | null): boolean {
  const noLiveLease =
    page.leases.length === 0 && page.active_beyond_window === 0 && page.truncated !== true
  return noLiveLease && pools !== null && pools.every((p) => p.active === 0)
}

async function fixtureHolders(): Promise<Result<HoldersBoard>> {
  const [l, c] = await Promise.all([fixtureLeases(), fixtureCapacity()])
  if (l.status !== 'ok' || c.status !== 'ok') return l as Result<HoldersBoard>
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: { page: l.data, pools: c.data.pools, poolsDetail: null },
  }
}

/** `GET /v1/admin/quota`. Admin-gated, so a 403 renders as information. */
export async function loadAdminQuota(): Promise<Result<{ quota: QuotaState[] }>> {
  if (USE_FIXTURES) return fixtureAdminQuota()
  return read<{ quota: QuotaState[] }>(route('/v1/admin/quota'), (d) => d.quota.length === 0)
}

async function fixtureAdminQuota(): Promise<Result<{ quota: QuotaState[] }>> {
  await new Promise((r) => setTimeout(r, 170))
  noteFixtureProbe(route('/v1/admin/quota'), 170, true)
  const q = (
    provider: string,
    tenant: string,
    state: string,
    extra: Partial<QuotaState> = {},
  ): QuotaState => ({
    provider, tenant_id: tenant, state,
    updated_at: new Date(Date.now() - 60_000).toISOString(),
    configured_hard_max: 50, adaptive_target: null, quota_derived_limit: null,
    requests_remaining: null, tokens_remaining: null, reset_at: null,
    cooldown_until: null, last_429_at: null, retry_after_seconds: null,
    success_count: 120, rate_limit_count: 0, effective_limit: 50,
    ...extra,
  })
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      // One of every treatment, so none of them ships unlooked-at: a healthy
      // row, THROTTLED (the one a fallthrough would mislabel), an effective
      // limit of 0 that is a FACT, and UNKNOWN which is not healthy.
      quota: [
        q('anthropic', 'eng', 'AVAILABLE', { requests_remaining: 4210 }),
        q('anthropic', 'u-bogdan', 'THROTTLED', {
          adaptive_target: 6, effective_limit: 6, rate_limit_count: 9,
          last_429_at: new Date(Date.now() - 240_000).toISOString(),
        }),
        q('openai', 'eng', 'EXHAUSTED', { effective_limit: 0, rate_limit_count: 41 }),
        q('openai', 'u-bogdan', 'UNKNOWN', { effective_limit: 0, success_count: 0 }),
      ],
    },
  }
}

/**
 * Set a pool's hard limit, routing by the pool's own name.
 *
 * The API has one route per pool KIND rather than one generic route, so the
 * name has to be decomposed. Two of these did not exist until 2026-09-20 --
 * backend, and the per-tenant slice of a provider -- which is precisely why
 * backend:CLOUD_RUN_JOB and provider:anthropic:tenant:u-bogdan became the
 * binding constraints the moment every other pool was raised.
 */
export async function setPoolLimit(poolName: string, limit: number): Promise<Result<unknown>> {
  const parts = poolName.split(':')
  let path: ApiRoute | null = null

  // One route per pool KIND, each keyed by its template (CH-18): a limit set on
  // two runners is one `/v1/admin/limits/runner/{name}` in the registry, not two.
  if (poolName === 'global') path = route('/v1/admin/limits/global')
  else if (parts[0] === 'tenant' && parts[1]) path = route('/v1/admin/limits/tenant/{name}', { name: parts[1] })
  else if (parts[0] === 'resource' && parts[1]) path = route('/v1/admin/limits/resource/{name}', { name: parts[1] })
  else if (parts[0] === 'runner' && parts[1]) path = route('/v1/admin/limits/runner/{name}', { name: parts[1] })
  else if (parts[0] === 'backend' && parts[1]) path = route('/v1/admin/limits/backend/{name}', { name: parts[1] })
  else if (parts[0] === 'provider' && parts[1] && parts[2] === 'tenant' && parts[3])
    path = route('/v1/admin/limits/provider/{name}/tenant/{tenant}', { name: parts[1], tenant: parts[3] })
  else if (parts[0] === 'provider' && parts[1]) path = route('/v1/admin/limits/provider/{name}', { name: parts[1] })

  if (path === null) {
    // Fail loudly rather than POSTing somewhere plausible. A limit that
    // silently went nowhere is worse than one that refused.
    return {
      status: 'error',
      error: {
        kind: 'invalid',
        httpStatus: null,
        code: null,
        message: `No admin route covers a pool named "${poolName}".`,
      },
    }
  }

  if (USE_FIXTURES) {
    await new Promise((r) => setTimeout(r, 200))
    return { status: 'ok', fetchedAt: Date.now(), data: { pool: poolName, limit } }
  }

  return write(path, 'PUT', { limit })
}

// --------------------------------------------------------------------------
// Fixtures
// --------------------------------------------------------------------------
// These are the REAL pool names and the REAL live values read from the dev
// platform on 2026-09-19, not invented ones -- including tenant:u-bogdan at 40
// against a configured 2, which is the out-of-band drift `make pool-check`
// found. A fixture that shows a tidy platform teaches the wrong thing about
// what this screen is for.

/**
 * The `admission` block /v1/capacity serves for the pool rows below.
 *
 * NOT hand-written and NOT recomputed here: the literal output of
 * `swarm_api.headroom.analyse_profile` over exactly those rows. That is why a
 * fixture can carry it without becoming a second implementation of the rule --
 * and `tests/unit/control_plane/test_blocker_ui_surface.py` re-runs the
 * analyser over the same rows and asserts this is still what it produces, so a
 * fixture that drifts from the server fails a test rather than teaching a shape
 * the API never sends.
 *
 * STRICT JSON ON PURPOSE, quoted keys and all: that test parses this literal,
 * and `make test` runs with no node, so it has to be readable as text.
 *
 * Two entries teach the thing the panel exists for. `mock` is capped by THREE
 * pools tied at 15 and `codex` by two tied at 10, so every single-ceiling
 * counterfactual on them reads zero. Those zeros are the shape an operator has
 * to recognise, and they are real numbers off the dev platform, not staged ones.
 */
const FIXTURE_ADMISSION: Record<string, ProfileAdmission> = {
  "mock": {
    "units": 1,
    "headroom": 15,
    "basis": "measured",
    "blockers": [],
    "binding": [
      "global",
      "resource:standard",
      "backend:CLOUD_RUN_JOB"
    ],
    "counterfactual": [
      {
        "pool": "global",
        "action": "raise",
        "headroom_after": 15,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "resource:standard",
          "backend:CLOUD_RUN_JOB"
        ]
      },
      {
        "pool": "tenant:u-bogdan",
        "action": "raise",
        "headroom_after": 15,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "global",
          "resource:standard",
          "backend:CLOUD_RUN_JOB"
        ]
      },
      {
        "pool": "resource:standard",
        "action": "raise",
        "headroom_after": 15,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "global",
          "backend:CLOUD_RUN_JOB"
        ]
      },
      {
        "pool": "runner:mock",
        "action": "raise",
        "headroom_after": 15,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "global",
          "resource:standard",
          "backend:CLOUD_RUN_JOB"
        ]
      },
      {
        "pool": "backend:CLOUD_RUN_JOB",
        "action": "raise",
        "headroom_after": 15,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "global",
          "resource:standard"
        ]
      }
    ],
    "complete": true,
    "unread": [],
    "uncapped": []
  },
  "generic": {
    "units": 1,
    "headroom": 10,
    "basis": "measured",
    "blockers": [],
    "binding": [
      "runner:generic"
    ],
    "counterfactual": [
      {
        "pool": "global",
        "action": "raise",
        "headroom_after": 10,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "runner:generic"
        ]
      },
      {
        "pool": "tenant:u-bogdan",
        "action": "raise",
        "headroom_after": 10,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "runner:generic"
        ]
      },
      {
        "pool": "resource:standard",
        "action": "raise",
        "headroom_after": 10,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "runner:generic"
        ]
      },
      {
        "pool": "runner:generic",
        "action": "raise",
        "headroom_after": 15,
        "basis_after": "measured",
        "delta": 5,
        "next_binding": [
          "global",
          "resource:standard",
          "backend:CLOUD_RUN_JOB"
        ]
      },
      {
        "pool": "backend:CLOUD_RUN_JOB",
        "action": "raise",
        "headroom_after": 10,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "runner:generic"
        ]
      }
    ],
    "complete": true,
    "unread": [],
    "uncapped": []
  },
  "claude-code": {
    "units": 1,
    "headroom": 0,
    "basis": "measured",
    "blockers": [
      {
        "pool": "provider:anthropic:tenant:u-bogdan",
        "reason": "PROVIDER_CONCURRENCY_LIMIT",
        "limit": 5,
        "active": 5,
        "group": "no_room"
      }
    ],
    "binding": [
      "provider:anthropic:tenant:u-bogdan"
    ],
    "counterfactual": [
      {
        "pool": "global",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "tenant:u-bogdan",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "resource:standard",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "runner:claude-code",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "backend:CLOUD_RUN_JOB",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "provider:anthropic",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "provider:anthropic:tenant:u-bogdan",
        "action": "raise",
        "headroom_after": 5,
        "basis_after": "measured",
        "delta": 5,
        "next_binding": [
          "runner:claude-code",
          "provider:anthropic"
        ]
      }
    ],
    "complete": true,
    "unread": [],
    "uncapped": []
  },
  "codex": {
    "units": 1,
    "headroom": 10,
    "basis": "measured",
    "blockers": [],
    "binding": [
      "runner:codex",
      "provider:openai"
    ],
    "counterfactual": [
      {
        "pool": "global",
        "action": "raise",
        "headroom_after": 10,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "runner:codex",
          "provider:openai"
        ]
      },
      {
        "pool": "tenant:u-bogdan",
        "action": "raise",
        "headroom_after": 10,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "runner:codex",
          "provider:openai"
        ]
      },
      {
        "pool": "resource:standard",
        "action": "raise",
        "headroom_after": 10,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "runner:codex",
          "provider:openai"
        ]
      },
      {
        "pool": "runner:codex",
        "action": "raise",
        "headroom_after": 10,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:openai"
        ]
      },
      {
        "pool": "backend:CLOUD_RUN_JOB",
        "action": "raise",
        "headroom_after": 10,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "runner:codex",
          "provider:openai"
        ]
      },
      {
        "pool": "provider:openai",
        "action": "raise",
        "headroom_after": 10,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "runner:codex"
        ]
      }
    ],
    "complete": true,
    "unread": [],
    "uncapped": []
  },
  "browser": {
    "units": 2,
    "headroom": 0,
    "basis": "measured",
    "blockers": [
      {
        "pool": "provider:anthropic:tenant:u-bogdan",
        "reason": "PROVIDER_CONCURRENCY_LIMIT",
        "limit": 5,
        "active": 5,
        "group": "no_room"
      }
    ],
    "binding": [
      "provider:anthropic:tenant:u-bogdan"
    ],
    "counterfactual": [
      {
        "pool": "global",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "tenant:u-bogdan",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "resource:browser",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "runner:browser",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "backend:GKE_AUTOPILOT",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "provider:anthropic",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "provider:anthropic:tenant:u-bogdan",
        "action": "raise",
        "headroom_after": 2,
        "basis_after": "measured",
        "delta": 2,
        "next_binding": [
          "resource:browser",
          "runner:browser",
          "backend:GKE_AUTOPILOT",
          "provider:anthropic"
        ]
      }
    ],
    "complete": true,
    "unread": [],
    "uncapped": []
  }
}

/** `headroom.blocked_reason_groups()`, verbatim. Same test pins it. */
const FIXTURE_REASON_GROUPS: Record<string, string[]> = {
  "needs_action": [
    "BUDGET_LIMIT",
    "DEPENDENCY",
    "MANUAL_PAUSE",
    "QUOTA_EXHAUSTED"
  ],
  "no_room": [
    "BACKEND_LIMIT",
    "COOLDOWN",
    "GLOBAL_CONCURRENCY_LIMIT",
    "PROVIDER_CONCURRENCY_LIMIT",
    "RESOURCE_CLASS_LIMIT",
    "RUNNER_LIMIT",
    "SCHEDULED_RETRY",
    "TENANT_LIMIT"
  ]
}

/**
 * `swarm_api.runnerinputs.input_contract()` over the frozen catalogue, verbatim.
 *
 * Served on every fixture profile so the submit screens' REQUIRED-KEYS path can
 * be reached without a deployment. Without it `requiredInputKeys` read null for
 * every profile, and a fixture session could only ever show the unread mark --
 * never the form refusing `claude-code` a blank prompt, which is the branch the
 * rebuilt flow exists for (docs/web-ui/ux-plan.md §1.2).
 *
 * A block for EVERY profile, empty lists included, for the reason
 * `input_contract` gives: an empty list is a measured "nothing required" and
 * must be reachable too. Strict JSON because
 * tests/unit/control_plane/test_ui_fixture_input_contract.py parses this literal
 * and compares it with the server's function -- so a runner that starts
 * demanding a key fails that test here rather than teaching the screen a rule
 * production does not apply.
 */
const FIXTURE_INPUT_CONTRACTS: Record<string, RunnerInputContract> = {
  "mock": {"required_keys": []},
  "generic": {"required_keys": []},
  "claude-code": {"required_keys": ["prompt"]},
  "codex": {"required_keys": ["prompt"]},
  "browser": {"required_keys": []}
}

/**
 * Whether each of the five may be dispatched, as `/v1/capacity` now serves it
 * (CP-3, visual QA 2026-09-25). A fixture whose `codex` is on offer develops
 * Pools, Profile headroom and Submit against a platform that does not exist --
 * which is how the disabled branch of all three shipped unexercised.
 *
 * A COPY OF THE FROZEN CATALOGUE'S TWO FIELDS, and therefore a strict-JSON
 * literal: `test_capacity_serves_availability.py` reads it with the same
 * `json_literal` the input-contract table above is held by and compares it to
 * `RUNNER_PROFILES`, so re-enabling a profile there fails here until this
 * follows.
 *
 * The value type is NAMED rather than written inline: the reader takes the
 * first `{` after the declaration as the literal, and an inline object type
 * would be that brace.
 */
interface FixtureAvailability {
  available: boolean
  disabled_reason: string
}
const FIXTURE_AVAILABILITY: Record<string, FixtureAvailability> = {
  "mock": {"available": true, "disabled_reason": ""},
  "generic": {"available": true, "disabled_reason": ""},
  "claude-code": {"available": true, "disabled_reason": ""},
  "codex": {"available": false, "disabled_reason": "codex is disabled on this platform. The provider refused the registered credential and the platform is focused on Claude. Use claude-code."},
  "browser": {"available": true, "disabled_reason": ""}
}

async function fixtureCapacity(): Promise<Result<Capacity>> {
  await new Promise((r) => setTimeout(r, 400))
  noteFixtureProbe(route('/v1/capacity'), 400, true)
  const pool = (
    name: string,
    hard: number,
    active: number,
    extra: Partial<Capacity['pools'][number]> = {},
  ) => ({
    name,
    hard_limit: hard,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: hard,
    active,
    available: Math.max(0, hard - active),
    enabled: true,
    updated_at: new Date().toISOString(),
    ...extra,
  })

  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      generated_at: new Date().toISOString(),
      tenant_id: 'u-bogdan',
      // The listing was not truncated, so a pool absent from it is
      // unconfigured rather than unread.
      pools_complete: true,
      blocked_reason_groups: FIXTURE_REASON_GROUPS,
      // The real five from the frozen catalogue, with the pool lists
      // pool_names_for builds for one tenant. Not invented: an empty map here
      // meant the headroom rows never rendered in development, which is how a
      // panel ships untested.
      runner_profiles: {
        mock: {
          resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: null, units: 1,
          ...FIXTURE_AVAILABILITY.mock,
          pools: ['global', 'tenant:u-bogdan', 'resource:standard', 'runner:mock', 'backend:CLOUD_RUN_JOB'],
          admission: FIXTURE_ADMISSION.mock,
          input_contract: FIXTURE_INPUT_CONTRACTS.mock,
        },
        generic: {
          resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: null, units: 1,
          ...FIXTURE_AVAILABILITY.generic,
          pools: ['global', 'tenant:u-bogdan', 'resource:standard', 'runner:generic', 'backend:CLOUD_RUN_JOB'],
          admission: FIXTURE_ADMISSION.generic,
          input_contract: FIXTURE_INPUT_CONTRACTS.generic,
        },
        'claude-code': {
          resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: 'anthropic', units: 1,
          ...FIXTURE_AVAILABILITY['claude-code'],
          pools: [
            'global', 'tenant:u-bogdan', 'resource:standard', 'runner:claude-code',
            'backend:CLOUD_RUN_JOB', 'provider:anthropic', 'provider:anthropic:tenant:u-bogdan',
          ],
          admission: FIXTURE_ADMISSION['claude-code'],
          input_contract: FIXTURE_INPUT_CONTRACTS['claude-code'],
        },
        codex: {
          resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: 'openai', units: 1,
          ...FIXTURE_AVAILABILITY.codex,
          pools: [
            'global', 'tenant:u-bogdan', 'resource:standard', 'runner:codex',
            'backend:CLOUD_RUN_JOB', 'provider:openai',
          ],
          admission: FIXTURE_ADMISSION.codex,
          input_contract: FIXTURE_INPUT_CONTRACTS.codex,
        },
        browser: {
          resource_class: 'browser', backend: 'GKE_AUTOPILOT', provider: 'anthropic', units: 2,
          ...FIXTURE_AVAILABILITY.browser,
          pools: [
            'global', 'tenant:u-bogdan', 'resource:browser', 'runner:browser',
            'backend:GKE_AUTOPILOT', 'provider:anthropic', 'provider:anthropic:tenant:u-bogdan',
          ],
          admission: FIXTURE_ADMISSION.browser,
          input_contract: FIXTURE_INPUT_CONTRACTS.browser,
        },
      },
      pools: [
        pool('global', 20, 5),
        pool('tenant:u-bogdan', 40, 5),
        pool('provider:anthropic', 10, 5),
        pool('provider:anthropic:tenant:u-bogdan', 5, 5),
        pool('provider:anthropic:tenant:eng', 5, 0),
        pool('provider:openai', 10, 0),
        pool('provider:openai:tenant:eng', 5, 0),
        pool('runner:claude-code', 10, 5),
        pool('runner:mock', 20, 0),
        pool('runner:browser', 4, 0),
        pool('runner:codex', 10, 0),
        pool('runner:generic', 10, 0),
        pool('resource:standard', 20, 5),
        pool('resource:browser', 4, 0),
        pool('resource:large', 2, 0),
        pool('backend:CLOUD_RUN_JOB', 20, 5),
        pool('backend:GKE_AUTOPILOT', 4, 0),
        pool('tenant:smoke', 2, 0),
        // Paused, and at a reduced ceiling: the two states this screen must
        // make obvious at a glance.
        pool('tenant:eng', 10, 0, { enabled: false, adaptive_target: 6, effective_limit: 6 }),
      ],
    },
  }
}

async function fixtureTasks(): Promise<Result<TaskPage>> {
  await new Promise((r) => setTimeout(r, 350))
  noteFixtureProbe(route('/v1/tasks'), 350, true)
  const now = Date.now()
  const at = (minsAgo: number) => new Date(now - minsAgo * 60_000).toISOString()

  // The fan-out that actually ran on 2026-09-19: fourteen script fixes, one
  // failure reclaimed three times, plus a workflow. Real shapes, real states.
  const mk = (
    id: string,
    state: TaskState,
    profile: string,
    minsAgo: number,
    extra: Partial<Task> = {},
  ): Task => ({
    id,
    tenant_id: 'u-bogdan',
    state,
    runner_profile: profile,
    resource_class: profile === 'browser' ? 'browser' : 'standard',
    provider: profile === 'mock' ? null : 'anthropic',
    priority: 0,
    created_at: at(minsAgo),
    updated_at: at(Math.max(0, minsAgo - 6)),
    started_at: state === 'QUEUED' || state === 'READY' ? null : at(minsAgo - 1),
    completed_at: ['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(state) ? at(minsAgo - 6) : null,
    submitted_by: 'bogdan@saga.xyz',
    attempt_count: state === 'FAILED' ? 3 : 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: null,
    step_id: null,
    depends_on: null,
    cancel_requested: false,
    repository_url: 'https://github.com/bogdan-alexandrescu/SwarmCloud',
    model: profile === 'mock' ? null : 'claude-opus-5',
    timeout_seconds: 3600,
    // Only a PARKED task has one. Left null elsewhere on purpose: a date here
    // on a RUNNING task would imply a retry that is not scheduled.
    next_eligible_at: state === 'PARKED' ? at(minsAgo - 15) : null,
    metadata: null,
    repository_ref: 'main',
    // THE FENCING PAIR, as `task_to_api` now serves it. Two different rules,
    // both taken from the contract rather than drawn by eye here:
    //
    // A LEASE IS HELD ONLY IN THE FOUR CONCURRENCY STATES (invariant 1).
    // QUEUED, PARKED, READY and every terminal state cost nothing, and
    // `control.park` and `control.finish` both write `current_lease_id: None`
    // on the way out. A fixture that gave every row a lease id would show a
    // released slot as held, on the screens built to find held slots.
    //
    // THE GENERATION IS NEVER RESET, so a finished task keeps the one it ran
    // at, and only a never-admitted task is at 0. It equals `attempt_count`
    // unless a reconciler fenced a stale worker; `task_19d91b82` overrides it
    // in `extra` below to the fenced shape, which is the live twenty-minute
    // stall and the one row that exercises the glyph.
    current_generation:
      state === 'QUEUED' || state === 'READY' ? 0 : state === 'FAILED' ? 3 : 1,
    current_lease_id: CONCURRENCY_STATES.has(state)
      ? `lease_${id.replace('task_', '')}`
      : null,
    // `task_to_api` sends this on every task, filling the API's defaults for a
    // task that predates the feature -- so the fixture default is the same pair
    // and the rows that differ say so in `extra`. A fixture that omitted it
    // would exercise only the "this API did not report a dispatch" path, which
    // is the one production should never take.
    dispatch: { strategy: 'collect', carrier: 'checkpoints', role: null, integrates: [] },
    input: null,
    last_error: state === 'FAILED' ? 'exit status 1: pytest collected 2 failures' : null,
    result_summary: state === 'SUCCEEDED' ? { artifacts: 1, checkpoints: 2 } : null,
    latest_checkpoint:
      state === 'SUCCEEDED' || state === 'PARKED'
        ? 'gs://swarm-artifacts-dev/u-bogdan/checkpoints/ckpt-3.tar.zst'
        : null,
    ...extra,
  })

  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      tasks: [
        mk('task_a073aff5', 'RUNNING', 'claude-code', 4),
        mk('task_8ea5c26d', 'RUNNING', 'claude-code', 5),
        // THE FENCED SHAPE, which is the twenty-minute DISPATCHED stall that
        // exposed the missing fields: generation 2 against attempt_count 1,
        // still naming the generation-1 lease that was never released. This is
        // the row that tells a developer whether an indicator reads correctly;
        // with every fixture row at generation == attempt_count, a glyph that
        // never fires and a glyph that is not wired look identical.
        mk('task_19d91b82', 'DISPATCHED', 'claude-code', 3, {
          current_generation: 2,
          current_lease_id: 'lease_723362a25968460aae35',
        }),
        mk('task_92eb4807', 'STARTING', 'claude-code', 2),
        mk('task_460409cd', 'LEASED', 'claude-code', 1),
        mk('task_950b8e4a', 'READY', 'claude-code', 1),
        mk('task_e7d210b6', 'QUEUED', 'claude-code', 0),
        mk('task_8e33a3de', 'FAILED', 'claude-code', 42),
        mk('task_a3881bec', 'SUCCEEDED', 'claude-code', 58),
        mk('task_99284d38', 'SUCCEEDED', 'claude-code', 50),
        mk('task_f278adec', 'SUCCEEDED', 'claude-code', 48),
        mk('task_f523c160', 'SUCCEEDED', 'claude-code', 47),
        mk('task_812d51aa', 'SUCCEEDED', 'claude-code', 46),
        mk('task_108ef29c', 'SUCCEEDED', 'mock', 90),
        mk('task_1beb89a5', 'CANCELLED', 'claude-code', 62),
        // A standalone `direct-pr` task: the one shape that opens a pull
        // request on its own, so the row chip and the Code panel's published
        // path are both reachable in development.
        mk('task_c41d90b7', 'SUCCEEDED', 'claude-code', 20, {
          dispatch: { strategy: 'direct-pr', carrier: 'checkpoints', role: null, integrates: [] },
        }),
        // Part of a workflow, so the graph view has something with edges.
        //
        // THE WORKFLOW IS AN `integrate` ONE, and the roles are the real ones
        // `_step_dispatch` assigns: every step a contributor except the single
        // sink, whose `integrates` is the topological prefix in apply order.
        // A `collect` fixture would have left the integrator badge, the
        // contributor publish panel and the one-pull-request rollup unrendered
        // in development -- which is how a panel ships untested.
        mk('task_wf_plan', 'SUCCEEDED', 'claude-code', 30, {
          workflow_id: 'wf_audit_01', step_id: 'plan', depends_on: [],
          dispatch: { strategy: 'integrate', carrier: 'checkpoints', role: 'contributor', integrates: [] },
        }),
        mk('task_wf_scan_a', 'SUCCEEDED', 'claude-code', 24, {
          workflow_id: 'wf_audit_01', step_id: 'scan-scripts', depends_on: ['plan'],
          dispatch: { strategy: 'integrate', carrier: 'checkpoints', role: 'contributor', integrates: [] },
        }),
        mk('task_wf_scan_b', 'RUNNING', 'claude-code', 24, {
          workflow_id: 'wf_audit_01', step_id: 'scan-terraform', depends_on: ['plan'],
          dispatch: { strategy: 'integrate', carrier: 'checkpoints', role: 'contributor', integrates: [] },
        }),
        // PARKED, not "BLOCKED". There is no BLOCKED task state -- a step
        // waiting on a dependency is PARKED with DEPENDENCY_INCOMPLETE.
        mk('task_wf_report', 'PARKED', 'claude-code', 24, {
          workflow_id: 'wf_audit_01', step_id: 'report',
          depends_on: ['scan-scripts', 'scan-terraform'],
          park_reason: 'DEPENDENCY_INCOMPLETE',
          dispatch: {
            strategy: 'integrate', carrier: 'checkpoints', role: 'integrator',
            integrates: ['task_wf_plan', 'task_wf_scan_a', 'task_wf_scan_b'],
          },
        }),
        // THE FAN-IN, reproduced from the live workflow that proved the graph
        // could not express one: `wf_5e5ad3b6f7da4299a839`, five independent
        // steps and a sixth depending on ALL five. Rendered by the old view it
        // was indistinguishable from a chain of five, so a fixture that only
        // ever held the two-fork shape would have let that ship again.
        //
        // The step ids are the real ones, `allornothing` included, because the
        // join's dependency line is the string that used to be ellipsed and its
        // length is the reason.
        ...(['cold-start', 'fencing', 'allornothing', 'checkpoints', 'absentzero'].map(
          (stepId, i) =>
            mk(`task_fan_${i}`, i === 3 ? 'FAILED' : 'SUCCEEDED', 'claude-code', 40 - i, {
              workflow_id: 'wf_5e5ad3b6f7da4299a839', step_id: stepId, depends_on: [],
              dispatch: { strategy: 'integrate', carrier: 'checkpoints', role: 'contributor', integrates: [] },
            }),
        )),
        mk('task_fan_join', 'CANCELLED', 'claude-code', 35, {
          workflow_id: 'wf_5e5ad3b6f7da4299a839', step_id: 'synthesis',
          depends_on: ['cold-start', 'fencing', 'allornothing', 'checkpoints', 'absentzero'],
          dispatch: {
            strategy: 'integrate', carrier: 'checkpoints', role: 'integrator',
            integrates: ['task_fan_0', 'task_fan_1', 'task_fan_2', 'task_fan_3', 'task_fan_4'],
          },
        }),
      ],
      next_page_token: null,
      tenant_id: 'u-bogdan',
    },
  }
}

async function fixtureWorkflowBoard(): Promise<Result<WorkflowBoard>> {
  await new Promise((r) => setTimeout(r, 300))
  noteFixtureProbe(route('/v1/workflows'), 300, true)
  // A route a non-admin genuinely cannot read, so the strip's 403 cell -- the
  // one that must read as information rather than breakage -- is visible in
  // development instead of only in production.
  noteFixtureProbe(route('/v1/admin/dispatch'), 120, false)

  // Reuse the task fixture so the join is a REAL join: if a step_id or task_id
  // stops matching, the fixture shows "state unknown" exactly as production
  // would, instead of quietly carrying a state of its own.
  const tasks = await fixtureTasks()
  const taskById = new Map<string, Task>()
  if (tasks.status === 'ok') for (const t of tasks.data.tasks) taskById.set(t.id, t)

  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: { taskById, statesDetail: null, workflows: fixtureWorkflowRows() },
  }
}

/**
 * The workflow rows, ONCE, because two screens read them.
 *
 * `loadWorkflowBoard` (the graph) and `loadWorkflows` (Overview's progress
 * check) hit the same route in production, so a fixture that gave them
 * different rows would let the two screens disagree in development about what
 * exists -- which is precisely the class of defect the live UI audit kept
 * finding.
 */
function fixtureWorkflowRows(): Workflow[] {
  const step = (
    step_id: string,
    depends_on: string[],
    task_id: string | null,
    // A MAP, as the API serves it -- upstream step_id -> artifact filename.
    // This fixture built a bare string, which is the shape nothing ever sends;
    // a fixture that disagrees with the response is a development screen that
    // renders correctly and a production one that does not.
    input_from: Record<string, string> = {},
  ) => ({
    step_id,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    depends_on,
    input_from,
    timeout_seconds: 3600,
    task_id,
  })

  return [
    {
      workflow_id: 'wf_audit_01',
      tenant_id: 'u-bogdan',
      // The fixture mirrors what the API now sends: a DERIVED state, the
      // stored copy beside it, and the drift between them. `wf_audit_01`
      // carries a deliberate disagreement so the drift line has something
      // to render in development.
      state: 'RUNNING',
      stored_state: 'QUEUED',
      state_source: 'derived',
      rollup: {
        state: 'RUNNING',
        complete: true,
        reason: 'steps_hold_capacity',
        counts: { SUCCEEDED: 1, RUNNING: 2, READY: 1, unstarted: 1 },
        unreadable_steps: [],
        unstarted_steps: ['publish'],
        steps_read: 4,
      },
      drift: {
        stored: 'QUEUED',
        derived: 'RUNNING',
        agrees: false,
        reason: 'steps_hold_capacity',
        steps_read: 4,
        unreadable_steps: [],
        repaired: true,
      },
      created_at: new Date(Date.now() - 30 * 60_000).toISOString(),
      updated_at: new Date().toISOString(),
      submitted_by: 'bogdan@saga.xyz',
      priority: 0,
      on_step_failure: 'FAIL_WORKFLOW',
      cancel_requested: false,
      steps: [
        step('plan', [], 'task_wf_plan'),
        step('scan-scripts', ['plan'], 'task_wf_scan_a', { plan: 'plan.md' }),
        step('scan-terraform', ['plan'], 'task_wf_scan_b', { plan: 'plan.md' }),
        step('report', ['scan-scripts', 'scan-terraform'], 'task_wf_report'),
        // No task_id: the workflow has not reached it. Renders as
        // "not started", which is NOT the same as "state unknown".
        step('publish', ['report'], null),
      ],
    },
      // THE SHAPE THE GRAPH EXISTS TO DISTINGUISH, reproduced from the live
      // workflow that proved the view could not express one:
      // `wf_5e5ad3b6f7da4299a839` on 2026-09-22, five independent steps and a
      // sixth depending on ALL five. Rendered by the old view it was
      // indistinguishable from a chain of five -- one `THEN` bar between the
      // five and the sixth, the identical mark a linear workflow draws -- and
      // the dependency line that would have disambiguated it was ellipsed at
      // `allorn...`. Five edges now arrive at `synthesis` and four would arrive
      // at the last step of a chain, so the two no longer render alike.
      //
      // JOINED to the six `task_fan_*` documents in the task fixture, so this
      // row exercises the derived rollup and the census as well as the shape. A
      // fixture whose only workflow is a two-way join never shows the case that
      // was got wrong, and one with no tasks never shows the heading deriving.
      //
      // The step ids are the real ones, `allornothing` included, because the
      // join's dependency line is the string that used to be ellipsed and its
      // length is the reason.
      {
        workflow_id: 'wf_5e5ad3b6f7da4299a839',
        tenant_id: 'u-bogdan',
        state: 'FAILED',
        stored_state: 'FAILED',
        state_source: 'derived',
        rollup: {
          state: 'FAILED',
          complete: true,
          reason: 'terminal_worst_first',
          counts: { SUCCEEDED: 4, FAILED: 1, CANCELLED: 1 },
          unreadable_steps: [],
          unstarted_steps: [],
          steps_read: 6,
        },
        created_at: new Date(Date.now() - 4 * 3600_000).toISOString(),
        updated_at: new Date(Date.now() - 2 * 3600_000).toISOString(),
        submitted_by: 'bogdan@saga.xyz',
        priority: 0,
        on_step_failure: 'FAIL_WORKFLOW',
        cancel_requested: false,
        steps: [
          step('cold-start', [], 'task_fan_0'),
          step('fencing', [], 'task_fan_1'),
          step('allornothing', [], 'task_fan_2'),
          step('checkpoints', [], 'task_fan_3'),
          step('absentzero', [], 'task_fan_4'),
          // `input_from` MIRRORS `depends_on` here, and it is a MAP -- upstream
          // step_id to artifact filename -- because that is what the API serves
          // and what `WorkflowStep.input_from` is typed as. This row was first
          // written with a bare string, which is the shape nothing ever sends.
          step(
            'synthesis',
            ['cold-start', 'fencing', 'allornothing', 'checkpoints', 'absentzero'],
            'task_fan_join',
            {
              'cold-start': 'cold-start.md',
              fencing: 'fencing.md',
              allornothing: 'allornothing.md',
              checkpoints: 'checkpoints.md',
              absentzero: 'absentzero.md',
            },
          ),
        ],
      },
      {
        // AN API OLDER THAN `rollup.py`, which is what production was still
        // running on 2026-09-22: `state` straight off the Firestore document,
        // no `state_source: 'derived'`, no `rollup`, no `drift`. This payload
        // is the one that printed "QUEUED" over steps reading succeeded, failed
        // and cancelled, so it has to be renderable in development or the fix
        // for it cannot be looked at. The heading must claim NO state here.
        workflow_id: 'wf_bcdc9180e4fb4a209f31',
        tenant_id: 'u-bogdan',
        state: 'QUEUED',
        stored_state: 'QUEUED',
        state_source: 'stored',
        created_at: new Date(Date.now() - 6 * 3600_000).toISOString(),
        updated_at: new Date(Date.now() - 2 * 3600_000).toISOString(),
        submitted_by: 'bogdan@saga.xyz',
        priority: 0,
        on_step_failure: 'FAIL_WORKFLOW',
        cancel_requested: false,
        steps: [
          step('research', [], 'task_a3881bec'),
          step('draft', ['research'], 'task_8e33a3de'),
          step('review', ['draft'], 'task_1beb89a5'),
        ],
      },
  ]
}

/**
 * The same rows as a PAGE, for Overview's workflow check.
 *
 * `rollup_report` is filled in as the route would fill it for this page --
 * one workflow examined, one written because the fixture's stored state
 * deliberately disagrees, nothing truncated and no read budget spent. A
 * fixture that omitted it would exercise the `undefined` branch of every
 * provenance test on this screen and never the populated one.
 */
async function fixtureWorkflows(): Promise<Result<WorkflowPage>> {
  await new Promise((r) => setTimeout(r, 300))
  noteFixtureProbe(route('/v1/workflows'), 300, true)
  const workflows = fixtureWorkflowRows()
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      workflows,
      next_page_token: null,
      tenant_id: 'u-bogdan',
      rollup_report: {
        examined: workflows.length,
        written: 1,
        agreed: 0,
        disagreed: 1,
        unknown: 0,
        truncated: false,
        step_reads: 4,
        step_read_budget_exhausted: false,
      },
    },
  }
}

// --------------------------------------------------------------------------
// The account pool
// --------------------------------------------------------------------------
// These are quota-broker's routes, reached through swarm-api. That is not a
// layering preference: refreshing an OAuth credential REVOKES the token it
// replaces, so the broker is the platform's single writer for subscription
// credentials and two writers racing on one account brick it. Nothing in this
// file may reach Secret Manager, mint a token, or "helpfully" retry a refresh.
//
// WHICH OF THEM SWARM-API PASSES THROUGH, because this file has been wrong
// about that in both directions. `routes/accounts.py` registers GET "",
// POST "", POST /authorize, POST /exchange, POST /{id}/refresh,
// PUT /{id}/lending, PUT /{id}/state and DELETE /{id}; the `AccountPool`
// protocol in `brokerclient.py` -- which is "deliberately not a generic HTTP
// client", so there is no fallthrough -- has a method for each of those and
// for nothing else.
//
// THE TWO SIGN-IN ROUTES ARE AMONG THEM NOW. For one deploy they were not,
// and this comment carried a long apology for it in place of a proxy; the
// proxy is built (`begin_sign_in` and `finish_sign_in` in routes/accounts.py),
// and forwards to quota-broker's own `POST /v1/accounts/authorize` and
// `POST /v1/accounts/exchange` (`begin_account_authorization` and
// `finish_account_authorization` in quota_broker/main.py).
//
// THE PROXY'S BODIES ARE NARROWER THAN THE BROKER'S, and that is not cosmetic.
// `AccountSignInStart` is `{label, lend_to}` and `AccountSignInFinish` is
// `{state, code}` (swarm_api/schemas.py), both on `StrictModel`, whose
// `model_config` is `extra="forbid"`. A body carrying `owner_tenant` or
// `provider`, which this file used to send because the BROKER takes them, is
// refused 422 by FastAPI before any handler runs: not a message about the
// sign-in, and the first thing an operator adding a real account would have
// met. The tenant comes from the verified token and the provider is fixed to
// SUBSCRIPTION_PROVIDER inside the proxy, so neither is this page's to send.
//
// A 404 or a 405 from either path therefore no longer means "this gap is
// known": it means the API answering is not the one in this repository, and
// `SignInFailure` in Accounts.tsx says that rather than blaming the request.
// Adding a paste box back here to route around one would reinstate the flow
// this screen exists to remove.

/**
 * Everything the Accounts screen needs, in one load.
 *
 * Two reads, and only the first decides whether there is a screen:
 *
 *  - `/v1/accounts` IS the screen. A failure here is a failure. It also
 *    supplies the owner: `routes/accounts.py` echoes `tenant_id` from the
 *    VERIFIED TOKEN rather than from the broker's answer, precisely so a page
 *    can never be told it is looking at a tenant it is not. That makes it the
 *    right value to register under and to display, and strictly better than
 *    `/v1/tenants/me` -- it is the same value the register route will file the
 *    account under, not a second read that could disagree with it.
 *  - `/v1/admin/tenants` is only for offering lending suggestions. A non-admin
 *    genuinely cannot read it, so its absence is information and lending falls
 *    back to typed tenant ids with that said on screen.
 */
export interface AccountsBoard {
  page: AccountsPage
  /** null means the tenant list was not readable. An EMPTY array means there are none. */
  tenants: Tenant[] | null
  tenantsDetail: string | null
  /**
   * WHEN THIS BOARD WAS READ, as a browser instant.
   *
   * Carried on the data because `Screen` hands `children` the data alone, and
   * anything below that compares a server-supplied instant against
   * `Date.now()` needs it. This board only reloads on a mutation, so a row
   * whose window resets while the page sits open produces a reset instant in
   * the past with nothing wrong anywhere -- and a screen that cannot say how
   * old the figure is has to guess at a cause for that. It is not a
   * measurement of the platform's clock and must not be used as one: it is the
   * age of what is on screen, which is the part that IS measurable here.
   */
  readAt: number
}

export async function loadAccountsBoard(): Promise<Result<AccountsBoard>> {
  if (USE_FIXTURES) return fixtureAccountsBoard()

  const [accounts, tenants] = await Promise.all([
    // `() => false`, NOT `accounts.length === 0`, and the reason is the whole
    // point of the screen: Screen renders `empty` INSTEAD of its children, so
    // an empty pool would hide the register form -- the one control that fixes
    // an empty pool. Zero accounts is handled inside the body as a state panel
    // sitting above a form that is still there.
    read<AccountsPage>(route('/v1/accounts'), () => false),
    read<{ tenants: Tenant[] }>(route('/v1/admin/tenants'), () => false),
  ])

  if (accounts.status === 'loading' || accounts.status === 'error' || accounts.status === 'empty') {
    return accounts as Result<AccountsBoard>
  }

  const tenantList =
    tenants.status === 'ok' || tenants.status === 'stale' ? tenants.data.tenants
    : tenants.status === 'empty' ? []
    : null
  const tenantsDetail =
    tenantList === null
      ? tenants.status === 'error' || tenants.status === 'stale'
        ? tenants.error.message
        : 'The tenant list read did not complete.'
      : null

  return {
    status: accounts.status,
    fetchedAt: accounts.fetchedAt,
    // Only an `ok` read carries the server's own `generated_at`; a `stale` one
    // is by definition not reporting a fresh server time, and Screen reads the
    // age off `fetchedAt` in that case.
    ...(accounts.status === 'ok' ? { serverAt: accounts.serverAt } : {}),
    // `accounts.fetchedAt` rather than `Date.now()`: on a `stale` read this is
    // the instant of the read that produced these rows, which is older, and it
    // is the rows' age that a reader needs.
    data: { page: accounts.data, tenants: tenantList, tenantsDetail, readAt: accounts.fetchedAt },
  } as Result<AccountsBoard>
}

/**
 * ADDING AN ACCOUNT IS A SIGN-IN. There is no keychain paste on this surface
 * and there must not be one again.
 *
 * The old flow asked a person to run `security find-generic-password`, copy a
 * JSON blob and paste it without reformatting it. That was a bad experience
 * and it was also fragile: the commonest failure was a hand-edited value that
 * parsed on the operator's machine and not here, reported as a 422 about a
 * missing refresh token, which named the symptom rather than the cause.
 *
 * It is now two calls. `authorize` returns a URL the person opens; they sign
 * in to Claude as they normally would; the callback page DISPLAYS a code; they
 * paste it and `exchange` redeems it. The PKCE verifier stays server-side,
 * keyed by `state`, so nothing secret passes through this file in either
 * direction -- the request carries a code, and the response carries an
 * account and an expiry.
 *
 * `POST /v1/accounts`, the keychain route, still exists in the API. It is
 * deliberately not called from anywhere in this app: keeping one paste box
 * "just in case" is how the flow that was removed comes back.
 */
export async function beginAccountSignIn(body: {
  /**
   * The label, and nothing the proxy does not have a field for.
   *
   * NO `owner_tenant` AND NO `provider`, both of which this call used to send
   * because quota-broker's own route takes them. `AccountSignInStart` is a
   * `StrictModel`, so an extra key is a 422 raised by validation -- a refusal
   * about the shape of the request, not about the sign-in, on the one control
   * this screen exists for. The tenant is resolved from the verified token by
   * the proxy and the provider is fixed there; a page cannot influence either,
   * which is the rule every other account route already follows.
   */
  label: string
  lend_to: string[]
}): Promise<Result<AccountAuthorization>> {
  const res = USE_FIXTURES
    ? await fixtureBeginSignIn(body)
    : await write(route('/v1/accounts/authorize'), 'POST', body)
  if (res.status !== 'ok') return res as Result<AccountAuthorization>

  const b = res.data
  // A 200 WITHOUT A URL IS NOT A SUCCESS. Rendering a "sign in" button that
  // opens `undefined` would put the failure two clicks away from its cause,
  // in a new tab, where the message explaining it is not.
  if (!isRecord(b) || typeof b.authorize_url !== 'string' || typeof b.state !== 'string') {
    return {
      status: 'error',
      error: {
        kind: 'server_error',
        httpStatus: 200,
        code: null,
        message:
          'The platform answered the sign-in request without a URL to open, so there is nothing to sign in to. Nothing was registered and nothing was changed.',
      },
    }
  }
  // Checked because this value is handed to `window.open`. It comes from our
  // own API and should always be the provider's https URL; a scheme check
  // costs nothing and is the difference between a bad deployment and a bad
  // deployment that opens a `javascript:` URL in the operator's browser.
  if (!/^https:\/\//i.test(b.authorize_url)) {
    return {
      status: 'error',
      error: {
        kind: 'server_error',
        httpStatus: 200,
        code: null,
        message:
          'The platform returned a sign-in URL this page will not open, because it is not an https address. Nothing was registered and nothing was changed. This is a deployment fault rather than anything you did.',
      },
    }
  }
  const ttl = b.expires_in_seconds
  return {
    status: 'ok',
    fetchedAt: res.fetchedAt,
    data: {
      authorize_url: b.authorize_url,
      state: b.state,
      // Absent stays absent. See `AccountAuthorization.expires_in_seconds`:
      // a countdown to a deadline nobody reported is invented arithmetic.
      expires_in_seconds: typeof ttl === 'number' && Number.isFinite(ttl) ? ttl : null,
    },
  }
}

/**
 * `POST /v1/accounts/exchange`. The paste, redeemed.
 *
 * SENT VERBATIM. The callback page renders `<code>#<state>` and people paste
 * what is on screen; `split_pasted_code` in the broker is the one reader of
 * that shape, and a second implementation here would disagree with it the
 * first time the page changed -- by refusing a paste the platform would have
 * accepted. So nothing is split, trimmed into fields or normalised here.
 *
 * A FAILED EXCHANGE DOES NOT ALWAYS LEAVE THE SIGN-IN OPEN, and this comment
 * used to say that it did. Four refusals, reading
 * `finish_account_authorization` in quota_broker/main.py:
 *
 *  - a paste whose state belongs to ANOTHER sign-in is refused before the
 *    pending record is even read, so this one is untouched and still open;
 *  - a token-endpoint refusal -- a mistyped, spent or expired code -- happens
 *    after the record is read and before `ref.delete()`, which runs only once
 *    an account exists, so that one is still open too;
 *  - "this sign-in took too long" deletes the pending record for age. It is
 *    raised BEFORE the token endpoint is called, so no credential was
 *    redeemed and NOTHING WAS CREATED;
 *  - "this sign-in has expired or was already completed" is raised because the
 *    record is ABSENT, and the one place that deletes a record without also
 *    raising is the line after `_provision_and_register` returns. So the
 *    commonest way to reach this message is that the sign-in SUCCEEDED -- the
 *    account exists, and on a re-auth its credential has just been replaced.
 *
 * THE FIRST TWO LEAVE THE SIGN-IN REDEEMABLE. The last two end it, and they
 * end it in OPPOSITE DIRECTIONS: one means nothing happened, the other means
 * it probably all happened. A caller that merges them writes "nothing was
 * created" over an account that now exists. `exchangeRefusal` in
 * Accounts.tsx is where they are told apart, and it matches the broker's own
 * wording positively so that an unrecognised refusal promises nothing rather
 * than promising the commonest thing.
 */
export async function finishAccountSignIn(body: {
  state: string
  code: string
}): Promise<Result<AccountExchangeResponse>> {
  const res = USE_FIXTURES
    ? await fixtureFinishSignIn(body)
    : await write(route('/v1/accounts/exchange'), 'POST', body)
  if (res.status !== 'ok') return res as Result<AccountExchangeResponse>

  const b = res.data
  if (!isRecord(b) || !isRecord(b.account) || typeof b.account.account_id !== 'string') {
    // A 201 whose body does not name the account is not "nothing happened".
    // The code may well have been redeemed and the account registered, and
    // telling somebody to try again would spend a single-use code on a
    // sign-in that had already succeeded.
    //
    // `httpStatus: 201` IS LOAD-BEARING, not decoration. `classify` only ever
    // builds an ApiError for a response that was not ok, so 201 cannot arrive
    // from a real refusal -- which makes it an exact marker, and
    // `exchangeRefusal` in Accounts.tsx keys its `accepted_unnamed` treatment
    // off it rather than off this sentence. The advice rendered under this
    // message has to agree with it; it used to say "pasting again costs
    // nothing" directly below "do not sign in again yet".
    return {
      status: 'error',
      error: {
        kind: 'server_error',
        httpStatus: 201,
        code: null,
        message:
          'The platform accepted the sign-in but did not say which account it created. The code may already have been redeemed, so do not sign in again yet — refresh the pool and look for the label first.',
      },
    }
  }
  const expires = b.expires_at
  return {
    status: 'ok',
    fetchedAt: res.fetchedAt,
    data: {
      account: b.account as unknown as Account,
      expires_at: typeof expires === 'string' ? expires : null,
      note: typeof b.note === 'string' ? b.note : undefined,
    },
  }
}

/** `PUT /v1/accounts/{id}/lending`. The whole list, not a delta. */
export async function setAccountLending(
  accountId: string,
  lendTo: string[],
): Promise<Result<unknown>> {
  if (USE_FIXTURES) return fixtureLending(accountId, lendTo)
  return write(route('/v1/accounts/{account_id}/lending', { account_id: accountId }), 'PUT', { lend_to: lendTo })
}

/** `PUT /v1/accounts/{id}/state`. `reason` is for the next person, so it is required here. */
export async function setAccountState(
  accountId: string,
  state: AccountStateName,
  reason: string,
): Promise<Result<unknown>> {
  if (USE_FIXTURES) return fixtureState(accountId, state, reason)
  return write(route('/v1/accounts/{account_id}/state', { account_id: accountId }), 'PUT', { state, reason })
}

/**
 * `POST /v1/accounts/{id}/refresh`.
 *
 * A 200 HERE DOES NOT MEAN THE REFRESH WORKED. The route reports the outcome
 * in the body -- `refresh.refreshed` and `refresh.reason` -- and answers 200
 * for `reauth_required` exactly as it does for `refreshed`. Treating the status
 * as the answer would put "succeeded" on screen for the one case that needs a
 * human, which is this repository's defining bug wearing a different hat.
 */
export async function refreshAccount(accountId: string): Promise<Result<RefreshResponse>> {
  const res = USE_FIXTURES
    ? await fixtureRefresh(accountId)
    : await write(route('/v1/accounts/{account_id}/refresh', { account_id: accountId }), 'POST')
  if (res.status !== 'ok') return res as Result<RefreshResponse>

  const body = res.data
  if (!isRecord(body) || !isRecord(body.refresh) || typeof body.refresh.refreshed !== 'boolean') {
    // A 200 whose body does not say what happened is not a success. Something
    // MAY have been rotated, and the previous access token may already be
    // revoked, so this must not read as "nothing happened" either.
    return {
      status: 'error',
      error: {
        kind: 'server_error',
        httpStatus: 200,
        code: null,
        message:
          'The refresh route answered without saying whether the credential was exchanged. ' +
          'Refreshing revokes the previous access token, so the account may have been rotated anyway — reload before trying again.',
      },
    }
  }
  return { status: 'ok', data: body as unknown as RefreshResponse, fetchedAt: res.fetchedAt }
}

/**
 * `DELETE /v1/accounts/{id}`.
 *
 * Removes the Firestore document. The SECRET IS RETAINED, deliberately:
 * deleting a Secret Manager secret is irreversible and takes its version
 * history with it, so an account removed by mistake stays re-registerable.
 * The screen has to say that, because "remove" otherwise reads as "destroy".
 */
export async function removeAccount(accountId: string): Promise<Result<unknown>> {
  if (USE_FIXTURES) return fixtureRemove(accountId)
  return write(route('/v1/accounts/{account_id}', { account_id: accountId }), 'DELETE')
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

// --------------------------------------------------------------------------
// Account fixtures
// --------------------------------------------------------------------------
// Mutable on purpose: registering, refreshing, lending and removing all take
// effect here, so the screen can be worked on -- and its failure treatments
// actually looked at -- with no deployed API at all. A fixture that only
// serves reads means every write path ships having never been seen.
//
// The set below carries ONE OF EVERY TREATMENT, because a tidy fixture teaches
// the wrong thing about what this screen is for: a live account, a stale one
// (`~`), one whose five-hour window has already reset, one that has never been
// observed at all, one in REAUTH_REQUIRED, and one the provider reports only a
// five-hour window for.

const ISO = (msFromNow: number): string => new Date(Date.now() + msFromNow).toISOString()

function fixtureAccount(
  owner: string,
  label: string,
  extra: Partial<Account> = {},
): Account {
  return {
    account_id: `${owner}:${label}`,
    owner_tenant: owner,
    label,
    provider: 'anthropic',
    state: 'AVAILABLE',
    reason: '',
    lend_to: [],
    assigned: 0,
    windows: {},
    observed_at: ISO(-4 * 60_000),
    stale: false,
    unreadable_by: [],
    unreadable_now: [],
    // A fixture default of `null` would make EVERY development row say "never
    // assigned", which is the one warning that means "no worker can reach the
    // broker" -- the fixture must not teach that shape as normal. The rows
    // that carry it do so deliberately, below.
    last_assigned_at: ISO(-26 * 60_000),
    ...extra,
  }
}

let fixtureAccounts: Account[] = [
  fixtureAccount('u-bogdan', 'primary', {
    assigned: 2,
    windows: {
      five_hour: { utilization: 0.41, resets_at: ISO(2 * 3600_000 + 51 * 60_000), reset: false },
      seven_day: { utilization: 0.63, resets_at: ISO(70 * 3600_000), reset: false },
    },
  }),
  fixtureAccount('u-bogdan', 'overflow', {
    // Observed two hours ago: past DEFAULT_STALE_AFTER, so every figure on this
    // row is marked `~` and nothing on it may be read as current.
    observed_at: ISO(-2 * 3600_000),
    stale: true,
    windows: {
      five_hour: { utilization: 0.88, resets_at: ISO(19 * 3600_000 + 43 * 60_000), reset: false },
      seven_day: { utilization: 0.12, resets_at: ISO(94 * 3600_000), reset: false },
    },
  }),
  fixtureAccount('u-bogdan', 'weekly-burn', {
    state: 'PAUSED',
    reason: 'held back for the release run on Monday',
    windows: {
      // Already past its reset: the figure describes a window that has refilled.
      five_hour: { utilization: 0.97, resets_at: ISO(-6 * 60_000), reset: true },
      seven_day: { utilization: 0.94, resets_at: ISO(70 * 3600_000), reset: false },
    },
  }),
  fixtureAccount('u-bogdan', 'never-polled', {
    // Registered, never observed. Utilisation is UNKNOWN, emphatically not 0%.
    observed_at: null,
    stale: true,
    windows: {},
    // And never handed to an agent either. On one row that is just a new
    // account; the panel counts it because on EVERY row it is the shape of
    // workers that cannot reach the broker at all.
    last_assigned_at: null,
  }),
  fixtureAccount('u-bogdan', 'retired-laptop', {
    state: 'REAUTH_REQUIRED',
    reason: 'refresh failed: reauth_required',
    lend_to: ['eng'],
    observed_at: ISO(-3 * 86_400_000),
    stale: true,
    windows: {
      five_hour: { utilization: 0.2, resets_at: ISO(-2 * 86_400_000), reset: true },
      seven_day: { utilization: 0.55, resets_at: ISO(-86_400_000), reset: true },
    },
  }),
  fixtureAccount('eng', 'shared', {
    assigned: 3,
    lend_to: ['u-bogdan'],
    // THE ROW THAT LOOKS HEALTHIEST AND SERVES NOBODY HERE. A borrower that was
    // never granted secretAccessor on a lent secret reports the account
    // unreadable; `choose()` then skips it for that tenant and for nobody else,
    // so it keeps its window, its headroom and three agents of the OWNER's --
    // and without these two fields it renders as the best account on the
    // screen. This is the fixture for the defect, and it is in development so
    // the treatment cannot ship unlooked-at.
    unreadable_by: ['u-bogdan'],
    unreadable_now: ['u-bogdan'],
    windows: {
      // Only one window came back. The 7D column says so rather than showing 0%.
      five_hour: { utilization: 0.56, resets_at: ISO(3600_000 + 12 * 60_000), reset: false },
    },
  }),
]

/** The tenant the fixture's verified token would carry. */
const FIXTURE_TENANT = 'u-bogdan'

async function fixtureAccountsBoard(): Promise<Result<AccountsBoard>> {
  await new Promise((r) => setTimeout(r, 210))
  noteFixtureProbe(route('/v1/accounts'), 210, true)
  // The admin tenant list is the one read a non-admin genuinely cannot do, and
  // the fixture exercises that branch rather than the happy one -- otherwise
  // the "no picker, and here is why" copy ships unlooked-at.
  noteFixtureProbe(route('/v1/admin/tenants'), 90, false, 'admin_required')
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      // `eng:shared` is in the list because it is LENT to this tenant, which is
      // what `for_tenant` returns. It is deliberately not removable, pausable
      // or refreshable from here, and the fixture is what makes that path
      // visible in development.
      page: {
        accounts: fixtureAccounts.map((a) => ({ ...a })),
        tenant_id: FIXTURE_TENANT,
        // A document this tenant owns and the store could not parse. The rows
        // above are six; the pool is seven. Development has to be able to see
        // the case where the list is SHORT, because that is the one where
        // every figure on the screen is quietly computed over the wrong set.
        unreadable_documents: ['u-bogdan:half-written'],
        unreadable_document_count: 1,
      },
      tenants: null,
      tenantsDetail: 'Admin group membership is required for this endpoint.',
      readAt: Date.now(),
    },
  }
}

function fixtureRefused(message: string): Result<unknown> {
  return {
    status: 'error',
    error: { kind: 'invalid', httpStatus: 422, code: 'validation_failed', message },
  }
}

/**
 * The pending sign-ins this fixture is holding, keyed by state.
 *
 * A MAP RATHER THAN A SINGLE SLOT, because the two refusals worth exercising
 * both need more than one: a code reused after it succeeded, and a paste whose
 * state belongs to a different sign-in than the one this page started. Both
 * are copy on the screen, and copy that no fixture can reach is copy that
 * ships having never been read.
 */
const fixturePending = new Map<string, { label: string; lend_to: string[] }>()

async function fixtureBeginSignIn(body: {
  label: string
  lend_to: string[]
}): Promise<Result<unknown>> {
  await new Promise((r) => setTimeout(r, 260))
  // The label rule is the BROKER's -- `validate_label` runs inside
  // /v1/accounts/authorize, before a URL is returned, so an unusable label is
  // caught before anybody signs in. The fixture applies the same refusal so
  // that the message an operator gets in development is the one production
  // gives, rather than a happy path only production disagrees with.
  if (!/^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$/.test(body.label)) {
    return fixtureRefused(
      `account label '${body.label}' must be lowercase letters, digits and dashes, ` +
        'starting and ending alphanumeric, at most 40 characters -- it becomes part ' +
        'of a Secret Manager name and a Kubernetes annotation',
    )
  }
  // 43 characters, which is what 32 random bytes base64url-encode to. Anything
  // shorter is the length the real authorize endpoint refuses, so a fixture
  // that produced a short state would be modelling a broken server.
  const state = `fixture-${Math.random().toString(36).slice(2)}${Math.random()
    .toString(36)
    .slice(2)}`.padEnd(43, 'x')
  fixturePending.set(state, { label: body.label, lend_to: body.lend_to })
  // NOTED ONLY ON THE PATH THAT SUCCEEDS. `noteFixtureProbe` renders a non-ok
  // probe as a 403 or a 503, and this route's refusal is a 422 -- so noting
  // the refusal above would put a wrong status on the Reference page, which
  // exists to say what these routes actually answered. A route with no row is
  // a route nothing has called yet: true, and not a claim about the platform.
  noteFixtureProbe(route('/v1/accounts/authorize'), 260, true)
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      // The provider's real authorize host and real redirect, so the screen's
      // copy -- which reads the callback host out of this URL rather than
      // restating it -- is exercised rather than stubbed.
      authorize_url:
        'https://claude.com/cai/oauth/authorize?code=true&response_type=code' +
        '&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback' +
        `&code_challenge_method=S256&state=${encodeURIComponent(state)}`,
      state,
      expires_in_seconds: 900,
    },
  }
}

async function fixtureFinishSignIn(body: {
  state: string
  code: string
}): Promise<Result<unknown>> {
  await new Promise((r) => setTimeout(r, 480))

  const hash = body.code.indexOf('#')
  const pastedState = hash === -1 ? null : body.code.slice(hash + 1).trim()
  if (pastedState && pastedState !== body.state) {
    return fixtureRefused(
      'the pasted code belongs to a different sign-in than the one this page ' +
        'started. Press Add account and sign in again.',
    )
  }
  const pending = fixturePending.get(body.state)
  if (!pending) {
    // The record is ABSENT. In the broker this is overwhelmingly a sign-in
    // that already SUCCEEDED, because the only delete that does not also
    // raise is the one after the account is written -- which is why the
    // screen's treatment for this may not say "nothing was created", and why
    // the fixture reaches it by pasting twice rather than by timing out.
    return fixtureRefused(
      'this sign-in has expired or was already completed. Codes are ' +
        'single-use; press Add account to start again.',
    )
  }
  const code = (hash === -1 ? body.code : body.code.slice(0, hash)).trim()
  // TWO SENTINEL CODES, because the screen has two treatments that no ordinary
  // paste can reach and unreadable copy is copy that ships unread. They stand
  // where a 15-minute wait and a broker bug would be, and nothing but a
  // deliberate paste produces either.
  //
  // `expired` -> the age refusal. Deletes the record BEFORE any redemption,
  // so nothing was created: the opposite consequence to the branch above,
  // which is the whole reason they are two treatments and not one.
  if (code.toLowerCase() === 'expired') {
    fixturePending.delete(body.state)
    return fixtureRefused(
      'this sign-in took too long and the code will have expired. ' +
        'Press Add account to start again.',
    )
  }
  // A short code stands in for one the endpoint rejects -- the commonest real
  // cause being that the person took too long. The pending record SURVIVES it,
  // exactly as the broker's does, so the screen's "your sign-in is still open,
  // paste again" treatment is reachable in development.
  if (code.length < 6) {
    return fixtureRefused(
      'the sign-in code was not accepted. Codes are single-use and expire ' +
        'quickly, so this usually means it was already redeemed or that too ' +
        'much time passed. Sign in again to get a new one.',
    )
  }

  const existing = fixtureAccounts.find(
    (a) => a.account_id === `${FIXTURE_TENANT}:${pending.label}`,
  )
  const account: Account = existing
    ? // Re-registering preserves state and readings, exactly as accountstore.py
      // does -- REAUTH_REQUIRED included, which is why the screen puts the
      // state back as a visible second call rather than assuming it moved.
      { ...existing, lend_to: pending.lend_to }
    : fixtureAccount(FIXTURE_TENANT, pending.label, {
        lend_to: pending.lend_to,
        observed_at: null,
        stale: true,
        windows: {},
      })
  fixtureAccounts = existing
    ? fixtureAccounts.map((a) => (a.account_id === account.account_id ? account : a))
    : [...fixtureAccounts, account]
  // Single-use, and deleted only once the account exists.
  fixturePending.delete(body.state)
  noteFixtureProbe(route('/v1/accounts/exchange'), 480, true)
  // `unnamed` -> the second sentinel: a 201 whose body names no account. The
  // account IS registered above and the record IS gone, which is exactly the
  // situation the treatment describes -- "look for the label in the pool" is
  // advice a fixture can now be checked against, and it finds it.
  if (code.toLowerCase() === 'unnamed') {
    return { status: 'ok', fetchedAt: Date.now(), data: {} }
  }
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      account,
      expires_at: ISO(8 * 3600_000),
      note: 'stored write-only; no route in this service returns key material',
    },
  }
}

async function fixtureLending(accountId: string, lendTo: string[]): Promise<Result<unknown>> {
  await new Promise((r) => setTimeout(r, 180))
  fixtureAccounts = fixtureAccounts.map((a) =>
    a.account_id === accountId ? { ...a, lend_to: lendTo } : a,
  )
  return { status: 'ok', fetchedAt: Date.now(), data: { ok: true } }
}

async function fixtureState(
  accountId: string,
  state: AccountStateName,
  reason: string,
): Promise<Result<unknown>> {
  await new Promise((r) => setTimeout(r, 180))
  fixtureAccounts = fixtureAccounts.map((a) =>
    a.account_id === accountId ? { ...a, state, reason } : a,
  )
  return { status: 'ok', fetchedAt: Date.now(), data: { ok: true } }
}

async function fixtureRefresh(accountId: string): Promise<Result<unknown>> {
  await new Promise((r) => setTimeout(r, 620))
  const account = fixtureAccounts.find((a) => a.account_id === accountId)
  if (!account) return fixtureRefused(`no account '${accountId}'`)

  // REAUTH_REQUIRED keeps failing on purpose: it is the one outcome an operator
  // has to act on, and the only way to see that copy is for a fixture to
  // produce it.
  if (account.state === 'REAUTH_REQUIRED') {
    return {
      status: 'ok',
      fetchedAt: Date.now(),
      data: {
        refresh: {
          tenant_id: account.label, provider: 'account',
          refreshed: false, reason: 'reauth_required', expires_at: null,
        },
        account,
      } satisfies RefreshResponse,
    }
  }
  const refreshed: Account = { ...account, observed_at: new Date().toISOString(), stale: false }
  fixtureAccounts = fixtureAccounts.map((a) => (a.account_id === accountId ? refreshed : a))
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      refresh: {
        tenant_id: account.label, provider: 'account',
        refreshed: true, reason: 'refreshed', expires_at: ISO(8 * 3600_000),
      },
      account: refreshed,
    } satisfies RefreshResponse,
  }
}

async function fixtureRemove(accountId: string): Promise<Result<unknown>> {
  await new Promise((r) => setTimeout(r, 200))
  fixtureAccounts = fixtureAccounts.filter((a) => a.account_id !== accountId)
  return { status: 'ok', fetchedAt: Date.now(), data: { removed: accountId, secret: 'retained' } }
}

// ===========================================================================
// Overview (the landing screen) -- two reads nothing else needed
// ===========================================================================
//
// Everything else the Overview draws comes from loaders that already existed
// above: loadCapacity, loadTasks, loadStats, loadProviders and loadLeases.
// Only these two are new, and both exist because the shape the Overview needs
// is genuinely different from what an existing loader returns.

/**
 * The subscription pool ALONE.
 *
 * Deliberately not `loadAccountsBoard`: that one also reads
 * `/v1/admin/tenants`, which exists to populate the lending picker on the
 * Accounts screen. The Overview offers no lending control, so paying for an
 * admin read -- and, for a non-admin, taking a guaranteed 403 -- to render a
 * headroom figure would be a request spent on nothing.
 *
 * `accounts.length === 0` IS empty here, and the panel says what that means:
 * no account is registered, so the subscription pool supplies nothing. That is
 * a real answer, not a failure, and it is the difference between "the pool is
 * empty" and "we could not read the pool".
 */
export async function loadAccountPool(): Promise<Result<AccountsPage>> {
  if (USE_FIXTURES) return fixtureAccountPool()
  return read<AccountsPage>(route('/v1/accounts'), (d) => d.accounts.length === 0)
}

async function fixtureAccountPool(): Promise<Result<AccountsPage>> {
  await new Promise((r) => setTimeout(r, 190))
  noteFixtureProbe(route('/v1/accounts'), 190, true)
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      accounts: fixtureAccounts.map((a) => ({ ...a })),
      tenant_id: FIXTURE_TENANT,
      // The same shortfall the Accounts board fixture carries. Both fixtures
      // read the same route, so a difference between them would be a shape the
      // API never sends.
      unreadable_documents: ['u-bogdan:half-written'],
      unreadable_document_count: 1,
    },
  }
}

/**
 * HOW MANY TASKS THE SPEND FIGURE IS SUMMED OVER, and why it is a small number.
 *
 * `cost_usd` and the four token counts live on the ATTEMPT (codec.py:285), and
 * there is no route that aggregates them -- no sum, no group-by, no date
 * range. The only way to total them from a browser is one
 * `GET /v1/tasks/{id}/attempts` per task, so the sample size is a request
 * count in disguise.
 *
 * Twelve, at three in flight, because the API's token bucket is 20 rps per
 * principal per instance and EVERY OPEN TAB counts against it. The Overview
 * already spends five requests on its other panels; a fan-out of thirty would
 * make the landing screen the thing that 429s the operator who opened it.
 * Three in flight, started only after the task page has landed, keeps the peak
 * under half the bucket with room for a second tab.
 *
 * The consequence is stated on the panel rather than hidden: this is a sample
 * of the most recent work, not the tenant's bill. An aggregate route is the
 * request filed in the report.
 */
const SPEND_SAMPLE_TASKS = 12
const SPEND_CONCURRENCY = 3

/**
 * What the Overview's spend panel is allowed to claim.
 *
 * Every field that could be zero is `number | null` on purpose. An attempt
 * that reports no cost is NOT an attempt that cost nothing -- `record_usage`
 * in agent_worker/control.py omits a key the runner did not report, and says
 * so in its own docstring: "None means not reported and zero means cost
 * nothing, and a mock task is genuinely the second while a result that failed
 * to parse is the first". So a total is null until at least one attempt
 * actually carried the figure, and `$0.00` is never printed over an unmeasured
 * sample.
 */
export interface SpendRollup {
  /** The tenant these tasks belong to, as the task page reported it. */
  tenantId: string | null
  /** Tasks on the page the sample was drawn from. */
  tasksOnPage: number
  /** Tasks on that page that have ever run. The population, not the sample. */
  tasksWithAttempts: number
  /** Tasks whose attempts were actually read. At most SPEND_SAMPLE_TASKS. */
  tasksSampled: number
  /** Tasks whose attempt read FAILED. Their spend is in none of the figures. */
  failedReads: number
  /** Why, for the panel. Null when every read in the sample succeeded. */
  failedDetail: string | null
  /** Attempts returned across the sample. */
  attempts: number
  /** Of those, how many carried a cost figure. The rest are not zero. */
  attemptsWithCost: number
  /** Of those, how many carried any token figure. */
  attemptsWithTokens: number
  costUsd: number | null
  inputTokens: number | null
  outputTokens: number | null
  cacheReadTokens: number | null
  cacheCreationTokens: number | null
  /** Oldest and newest `created_at` among the SAMPLED tasks. The real span. */
  from: string | null
  to: string | null
}

/**
 * Sum spend over the most recent tasks of an already-loaded page.
 *
 * Takes the page rather than re-reading `/v1/tasks`: the Overview has it in
 * hand, and a second copy could disagree with the one the rest of the screen
 * is drawn from -- two panels describing different sets of tasks with no way
 * to tell.
 *
 * `page.tasks` arrives newest-first (store.py:408 orders by created_at
 * DESCENDING), so `slice` takes the most recent, not an arbitrary twelve.
 */
export async function loadSpend(page: TaskPage): Promise<Result<SpendRollup>> {
  const withAttempts = page.tasks.filter((t) => t.attempt_count > 0)
  const sample = withAttempts.slice(0, SPEND_SAMPLE_TASKS)
  const base = {
    tenantId: page.tenant_id ?? null,
    tasksOnPage: page.tasks.length,
    tasksWithAttempts: withAttempts.length,
    tasksSampled: sample.length,
  }

  // No task has ever run. A real zero, and the panel says exactly that --
  // distinct from "we could not read the attempts".
  if (sample.length === 0) return { status: 'empty', fetchedAt: Date.now() }

  const results = await mapWithLimit(sample, SPEND_CONCURRENCY, (t) =>
    USE_FIXTURES ? fixtureSpendAttempts(t) : loadAttempts(t.id),
  )

  let attempts = 0
  let attemptsWithCost = 0
  let attemptsWithTokens = 0
  let failedReads = 0
  let failedDetail: string | null = null
  // `null` until a figure is actually seen. Seeding these at 0 is how an
  // unmeasured sample becomes a confident "$0.00".
  let cost: number | null = null
  let input: number | null = null
  let output: number | null = null
  let cacheRead: number | null = null
  let cacheCreate: number | null = null

  const add = (acc: number | null, v: number | null): number | null =>
    typeof v === 'number' && Number.isFinite(v) ? (acc ?? 0) + v : acc

  for (const r of results) {
    if (r.status === 'error') {
      failedReads++
      // The FIRST failure's message, kept verbatim. Collapsing several causes
      // into one sentence is the mistake this codebase keeps paying for.
      failedDetail ??= r.error.message
      continue
    }
    // `empty` means the task reports attempts but the subcollection returned
    // none. Nothing to add, and nothing failed.
    if (r.status !== 'ok' && r.status !== 'stale') continue
    for (const a of r.data.attempts) {
      attempts++
      if (typeof a.cost_usd === 'number' && Number.isFinite(a.cost_usd)) attemptsWithCost++
      const anyToken =
        [a.input_tokens, a.output_tokens, a.cache_read_input_tokens, a.cache_creation_input_tokens]
          .some((v) => typeof v === 'number' && Number.isFinite(v))
      if (anyToken) attemptsWithTokens++
      cost = add(cost, a.cost_usd)
      input = add(input, a.input_tokens)
      output = add(output, a.output_tokens)
      cacheRead = add(cacheRead, a.cache_read_input_tokens)
      cacheCreate = add(cacheCreate, a.cache_creation_input_tokens)
    }
  }

  // EVERY read failed. There is no partial figure to show and showing the
  // zeros would be a number nobody measured, so this is an error, not a
  // rollup with empty columns.
  if (failedReads === sample.length) {
    const first = results.find((r) => r.status === 'error')
    return {
      status: 'error',
      error:
        first && first.status === 'error'
          ? first.error
          : {
              kind: 'server_error',
              httpStatus: null,
              code: null,
              message: 'No attempt read completed.',
            },
    }
  }

  const times = sample
    .map((t) => t.created_at)
    .filter((v): v is string => typeof v === 'string')
    .sort()

  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      ...base,
      failedReads,
      failedDetail,
      attempts,
      attemptsWithCost,
      attemptsWithTokens,
      costUsd: cost,
      inputTokens: input,
      outputTokens: output,
      cacheReadTokens: cacheRead,
      cacheCreationTokens: cacheCreate,
      from: times[0] ?? null,
      to: times[times.length - 1] ?? null,
    },
  }
}

/**
 * HOW MANY STEPS THE WORKFLOW BOARD READS ATTEMPTS FOR, and why it is bounded.
 *
 * Same wall as `loadSpend` above and the same arithmetic: `cost_usd` and the
 * four token counts live on the ATTEMPT (codec.py:285) and no route aggregates
 * them, so a per-step figure costs one `GET /v1/tasks/{id}/attempts` per step.
 * A board showing ten workflows of five steps would be fifty requests against a
 * 20 rps bucket shared with every other tab the operator has open.
 *
 * Twelve tasks at three in flight, started only after the board itself has
 * landed. A step outside the sample renders `not sampled` -- which is a THIRD
 * absence, distinct from "no attempt reported a cost" and from "the read
 * failed", and the three must not render alike. An aggregation route is the
 * standing request (audit S3); until it exists, a bounded sample that says so
 * is the honest shape.
 */
const STEP_USAGE_SAMPLE_TASKS = 12
const STEP_USAGE_CONCURRENCY = 3

/** What one step's attempts add up to. Every summable field is nullable. */
export interface StepUsage {
  /** Attempts returned for this task. A real count -- 0 is possible and means the read succeeded with none. */
  attempts: number
  /** Of those, how many carried a cost. The rest are not zero. */
  attemptsWithCost: number
  /** Of those, how many carried any token figure. */
  attemptsWithTokens: number
  /** Checkpoint ids listed across the attempts. COUNTED, so 0 is a digit. */
  checkpoints: number
  costUsd: number | null
  inputTokens: number | null
  outputTokens: number | null
  cacheReadTokens: number | null
  cacheCreationTokens: number | null
}

export interface WorkflowUsage {
  /** Usage for every task whose attempts were read. */
  byTaskId: Map<string, StepUsage>
  /** Task ids left out because the sample ceiling was reached. Not zero-cost. */
  notSampled: Set<string>
  /** Task id -> why its attempt read failed. Not zero-cost either. */
  failed: Map<string, string>
  /** Published so the screen can say what the ceiling was, rather than imply none. */
  sampleLimit: number
  /** Distinct task ids the board asked about. */
  tasksRequested: number
}

/**
 * Attempt usage for the tasks a workflow board has on screen.
 *
 * Takes the ids rather than re-reading the board: the screen has them in hand,
 * and a second read could disagree with the graph that is already drawn.
 */
export async function loadWorkflowUsage(
  taskIds: readonly string[],
): Promise<Result<WorkflowUsage>> {
  const wanted = Array.from(new Set(taskIds))
  const sample = wanted.slice(0, STEP_USAGE_SAMPLE_TASKS)
  const notSampled = new Set(wanted.slice(STEP_USAGE_SAMPLE_TASKS))

  // No step has a task yet. A real zero -- the workflow has not reached any of
  // them -- and distinct from "we could not read the attempts".
  if (sample.length === 0) return { status: 'empty', fetchedAt: Date.now() }

  const results = await mapWithLimit(sample, STEP_USAGE_CONCURRENCY, (id) =>
    USE_FIXTURES ? fixtureStepAttempts(id) : loadAttempts(id),
  )

  const byTaskId = new Map<string, StepUsage>()
  const failed = new Map<string, string>()
  results.forEach((r, i) => {
    const id = sample[i]
    if (id === undefined) return
    if (r.status === 'error') {
      failed.set(id, r.error.message)
      return
    }
    // `loading` cannot be awaited into existence here, but the union permits
    // it and a silent `continue` would record the task as measured-with-zero.
    if (r.status === 'loading') {
      failed.set(id, 'The attempt read did not complete.')
      return
    }
    byTaskId.set(id, rollUpAttempts(r.status === 'empty' ? [] : r.data.attempts))
  })

  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      byTaskId,
      notSampled,
      failed,
      sampleLimit: STEP_USAGE_SAMPLE_TASKS,
      tasksRequested: wanted.length,
    },
  }
}

/**
 * Sum one task's attempts.
 *
 * Each field is summed SEPARATELY and stays null until some attempt actually
 * carried it -- `record_spend` writes only the keys the runner reported, so an
 * attempt can have input tokens and no output, and `(input ?? 0) + (output ??
 * 0)` counts the missing half as a zero. That is the same defect
 * `AgentDetail.tsx:408` records having already been fixed once.
 */
function rollUpAttempts(attempts: readonly AttemptRow[]): StepUsage {
  return {
    attempts: attempts.length,
    attemptsWithCost: attempts.filter((a) => typeof a.cost_usd === 'number' && Number.isFinite(a.cost_usd)).length,
    attemptsWithTokens: attempts.filter((a) =>
      [a.input_tokens, a.output_tokens, a.cache_read_input_tokens, a.cache_creation_input_tokens].some(
        (v) => typeof v === 'number' && Number.isFinite(v),
      ),
    ).length,
    checkpoints: attempts.reduce((t, a) => t + a.checkpoints.length, 0),
    costUsd: sumReported(attempts, (a) => a.cost_usd),
    inputTokens: sumReported(attempts, (a) => a.input_tokens),
    outputTokens: sumReported(attempts, (a) => a.output_tokens),
    cacheReadTokens: sumReported(attempts, (a) => a.cache_read_input_tokens),
    cacheCreationTokens: sumReported(attempts, (a) => a.cache_creation_input_tokens),
  }
}

/**
 * Attempts for the workflow board's step figures.
 *
 * `fixtureAttempts` reports null for every usage field, which is faithful to an
 * attempt that ran before usage capture and would mean every node on the board
 * read "not reported" in development -- the one state that ships unlooked-at
 * would be the ordinary one, which is the failure `fixtureSpendAttempts`
 * already exists to avoid.
 *
 * The mix here is the one the node has to survive, and it is chosen per task id
 * so it does not move between refreshes:
 *
 *   * `task_wf_plan`    figures present on both attempts;
 *   * `task_wf_scan_a`  a MEASURED ZERO cost -- the digit case. A mock runner
 *                       really does cost nothing, and `$0.00` here is a
 *                       reading, not an absence;
 *   * `task_wf_scan_b`  attempts that carry nothing. "not reported";
 *   * anything else     one attempt with figures.
 */
async function fixtureStepAttempts(taskId: string): Promise<Result<{ attempts: AttemptRow[] }>> {
  await new Promise((r) => setTimeout(r, 70))
  noteFixtureProbe(route('/v1/tasks/{id}/attempts', { id: taskId }), 70, true)
  const mk = (n: number, usage: Partial<AttemptRow>, checkpoints: string[] = []): AttemptRow => ({
    attempt_id: `${taskId}-a${n}`,
    task_id: taskId,
    tenant_id: FIXTURE_TENANT,
    generation: n,
    lease_id: `lease-${taskId}-${n}`,
    backend: 'CLOUD_RUN_JOB',
    execution_name: null,
    created_at: new Date(Date.now() - 20 * 60_000).toISOString(),
    started_at: new Date(Date.now() - 19 * 60_000).toISOString(),
    completed_at: null,
    exit_code: null,
    error: null,
    peak_rss_bytes: null,
    peak_disk_bytes: null,
    oom_near_miss: false,
    checkpoints,
    input_tokens: null,
    output_tokens: null,
    cache_read_input_tokens: null,
    cache_creation_input_tokens: null,
    cost_usd: null,
    ...usage,
  })

  if (taskId === 'task_wf_scan_a') {
    return {
      status: 'ok',
      fetchedAt: Date.now(),
      // cost_usd: 0 EXACTLY. A mock runner costs nothing and reported so; the
      // node must print "$0.00" as a digit, never the absent sentence.
      data: { attempts: [mk(1, { cost_usd: 0, input_tokens: 0, output_tokens: 0 })] },
    }
  }
  if (taskId === 'task_wf_scan_b') {
    return { status: 'ok', fetchedAt: Date.now(), data: { attempts: [mk(1, {})] } }
  }
  if (taskId === 'task_wf_plan') {
    return {
      status: 'ok',
      fetchedAt: Date.now(),
      data: {
        attempts: [
          mk(2, { input_tokens: 21_400, output_tokens: 3_180, cache_read_input_tokens: 104_000, cache_creation_input_tokens: 9_900, cost_usd: 0.42 }, ['ckpt_plan_2']),
          // A retry that predates usage capture: real attempt, no figures.
          mk(1, {}),
        ],
      },
    }
  }
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      attempts: [
        mk(1, { input_tokens: 8_200, output_tokens: 1_050, cache_read_input_tokens: 44_000, cache_creation_input_tokens: 5_100, cost_usd: 0.0061 }, ['ckpt_a', 'ckpt_b']),
      ],
    },
  }
}

/**
 * `Promise.all` with a ceiling on how many are in flight.
 *
 * Not a utility for its own sake: `Promise.all` over the sample would put
 * twelve requests on the wire at once, and the API's bucket is 20 rps per
 * principal shared with every other tab the operator has open. This is the
 * one place in this file that fans out, so the limiter lives here rather than
 * in a lib nothing else imports.
 */
async function mapWithLimit<T, R>(
  items: readonly T[],
  limit: number,
  fn: (item: T) => Promise<R>,
): Promise<R[]> {
  const out = new Array<R>(items.length)
  let next = 0
  const worker = async (): Promise<void> => {
    for (;;) {
      const i = next++
      const item = items[i]
      if (item === undefined) return
      out[i] = await fn(item)
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker))
  return out
}

/**
 * Attempts for the spend fixture.
 *
 * Separate from `fixtureAttempts` above, which reports null for every usage
 * field -- a faithful picture of an attempt that ran before usage capture, and
 * the right default for the attempt timeline. Summing those would show the
 * spend panel only in its "nothing measured" state, so the one state that
 * ships unlooked-at would be the ordinary one.
 *
 * The mix here is the one the panel has to survive: figures present on the
 * newest attempt, absent on a retry, and a task whose attempts carry tokens
 * but no cost at all.
 */
async function fixtureSpendAttempts(task: Task): Promise<Result<{ attempts: AttemptRow[] }>> {
  await new Promise((r) => setTimeout(r, 90))
  noteFixtureProbe(route('/v1/tasks/{id}/attempts', { id: task.id }), 90, true)
  const seed = task.id.length + task.attempt_count
  const mk = (n: number, usage: Partial<AttemptRow>): AttemptRow => ({
    attempt_id: `${task.id}-a${n}`,
    task_id: task.id,
    tenant_id: task.tenant_id,
    generation: n,
    lease_id: `lease-${task.id}-${n}`,
    backend: 'CLOUD_RUN_JOB',
    execution_name: null,
    created_at: task.created_at,
    started_at: task.started_at,
    completed_at: task.completed_at,
    exit_code: null,
    error: null,
    peak_rss_bytes: null,
    peak_disk_bytes: null,
    oom_near_miss: false,
    checkpoints: [],
    input_tokens: null,
    output_tokens: null,
    cache_read_input_tokens: null,
    cache_creation_input_tokens: null,
    cost_usd: null,
    ...usage,
  })

  // `mock` and `generic` runners report nothing at all -- the frozen catalogue
  // says only claude-code and codex produce usage -- so their attempts stay
  // all-null and the panel has to count them as unmeasured rather than free.
  if (task.runner_profile === 'mock' || task.runner_profile === 'generic') {
    return { status: 'ok', fetchedAt: Date.now(), data: { attempts: [mk(1, {})] } }
  }

  const rows = [
    mk(task.attempt_count, {
      input_tokens: 14_000 + seed * 137,
      output_tokens: 2_100 + seed * 31,
      cache_read_input_tokens: 96_000 + seed * 811,
      cache_creation_input_tokens: 11_500 + seed * 57,
      cost_usd: 0.18 + (seed % 7) * 0.043,
    }),
  ]
  // A retry that predates usage capture: real attempt, no figures on it.
  if (task.attempt_count > 1) rows.push(mk(task.attempt_count - 1, {}))
  return { status: 'ok', fetchedAt: Date.now(), data: { attempts: rows } }
}
