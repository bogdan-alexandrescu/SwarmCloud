// The agent section's click-only states the fixture build never draws
// (issue 439; the registry is prosestates.agent.tsx): each screen mounted on
// hand-built props, with what it needs stubbed -- fetch for an action's
// error, the phone's media query for the phone-only tool menu, and the
// widths jsdom does not lay out for the tab bar's More. `restoreAgentStubs`
// undoes all of it after each state (prose.states.agent.test.tsx).

import { useState, type ReactElement } from 'react'
import { vi } from 'vitest'

import { AgTabsEdge } from '../agent-split/tabs'
import { AgentDetailScreen } from '../AgentDetail'
import { LogBody, PromptText, type LogFeed } from '../Artifacts'
import { FIXTURE_CLOCK, type AgentDetail, type CancelResult } from '../api'
import { AttemptTimelineScreen } from '../AttemptTimeline'
import type { ApiError, Result } from '../fetch'
import { CheckpointBrowser, type CheckpointLoaders } from '../CheckpointBrowser'
import { DiffView } from '../diff/DiffView'
import { LiveToolMenu } from '../LiveToolMenu'
import { LogMarksContext, type LogMarks } from '../logMarks'
import { StopRun } from '../StopRun'
import type { AttemptRow, Task, TaskTranscript, TranscriptStep, Waiting, WaitingHistoryEntry } from '../types'
import { WaitPanel } from '../WaitPanel'
import { at, attempt, task } from './runfixture'

// ------------------------------------------------------------------ stubs

const restores: (() => void)[] = []

/** Undo every stub a mount set: called after each state. */
export function restoreAgentStubs(): void {
  for (const undo of restores.splice(0).reverse()) undo()
}

/**
 * What `cancelTask` answers instead of the build's own (fixture) answer, while
 * set: the slice mocks api.ts's `cancelTask` to read it (the fixture build
 * never refuses a stop). Cleared by `restoreAgentStubs`.
 */
export const refuse: { cancel: Result<CancelResult> | null; transcript: Result<TaskTranscript> | null } = { cancel: null, transcript: null }

/** The next stop is released at once (it held no capacity), until restored. */
export function releaseStop(): void {
  refuse.cancel = { status: 'ok', fetchedAt: Date.now(), data: { released_immediately: true } }
  restores.push(() => {
    refuse.cancel = null
  })
}

/** The next stop is refused by the API with `message`, until restored. */
export function refuseStop(message: string): void {
  const error: ApiError = { kind: 'server_error', httpStatus: 500, code: 'internal', message }
  refuse.cancel = { status: 'error', error }
  restores.push(() => {
    refuse.cancel = null
  })
}

/**
 * What the attempts and agent reads answer instead of the build's own
 * (fixture) answers, while set: the slice mocks api.ts's `loadAttempts` and
 * `loadAgentDetail` to read them. Cleared by `restoreAgentStubs`.
 */
export const served: { attempts: Result<{ attempts: AttemptRow[] }> | null; detail: Result<AgentDetail> | null } = { attempts: null, detail: null }

/** The next transcript read (the transcript's `next window`) is refused by the API with `message`, until restored. */
export function refuseTranscript(message: string): void {
  const error: ApiError = { kind: 'server_error', httpStatus: 500, code: 'internal', message }
  refuse.transcript = { status: 'error', error }
  restores.push(() => {
    refuse.transcript = null
  })
}

/** The phone's media query matches, until restored (jsdom has no matchMedia). */
export function stubPhone(): void {
  const had = Object.getOwnPropertyDescriptor(window, 'matchMedia')
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: (query: string) => ({
      matches: true,
      media: query,
      onchange: null,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      addListener: () => undefined,
      removeListener: () => undefined,
      dispatchEvent: () => false,
    }),
  })
  restores.push(() => {
    if (had === undefined) delete (window as { matchMedia?: unknown }).matchMedia
    else Object.defineProperty(window, 'matchMedia', had)
  })
}

/** Inside `.ps-narrow`, every element is `each` px wide and its box `room` px: the layout jsdom does not do. */
export function stubNarrow(room: number, each: number): void {
  const proto = HTMLElement.prototype
  const keep = (['clientWidth', 'offsetWidth', 'scrollWidth'] as const).map((k) => [k, Object.getOwnPropertyDescriptor(proto, k)] as const)
  for (const [k, d] of keep) {
    Object.defineProperty(proto, k, {
      configurable: true,
      get(this: HTMLElement) {
        if (this.closest('.ps-narrow') === null) return (d?.get?.call(this) as number | undefined) ?? 0
        return k === 'clientWidth' ? room : each
      },
    })
  }
  restores.push(() => {
    for (const [k, d] of keep) {
      if (d === undefined) delete (proto as unknown as Record<string, unknown>)[k]
      else Object.defineProperty(proto, k, d)
    }
  })
}

// --------------------------------------------------------------- fixtures

/** A running agent a member may stop. */
export function runningTask(over: Partial<Task> = {}): Task {
  return task({
    id: 'task_5c0e2a7d91b34f6e8a10',
    state: 'RUNNING',
    started_at: at(1),
    current_generation: 2,
    ...over,
  })
}

function history(n: number): WaitingHistoryEntry[] {
  return Array.from({ length: n }, (_, i) => ({ at: at(60 + i), kind: 'parked', text: `entry ${i + 1}` }))
}

/** A task parked on CI with `n` entries of history. */
export function parkedTask(n: number): Task {
  const w: Waiting = {
    reason: 'CI_PENDING',
    summary: 'checks pending',
    since: at(60),
    next_check_at: at(130),
    resumes_when: 'its pull request checks settle',
    detail: {},
    history: history(n),
    history_read: true,
    read_at: at(120),
  }
  return task({
    id: 'task_31c301ecb33940eeb278',
    state: 'PARKED',
    park_reason: 'CI_PENDING',
    next_eligible_at: w.next_check_at,
    waiting: w,
  })
}

/** The instant the wait panel is read at: two hours after `at(0)`. */
export const WAIT_NOW = Date.parse(at(120))

function section(path: string, hunks: [number, string, string][]): string {
  const out = [`diff --git a/${path} b/${path}`, `--- a/${path}`, `+++ b/${path}`]
  for (const [line, del, add] of hunks) out.push(`@@ -${line},1 +${line},1 @@`, `-${del}`, `+${add}`)
  return out.join('\n') + '\n'
}

/** A two-file patch, so the viewer draws its file picker and its file rows. */
export const PATCH =
  section('src/app/one.ts', [
    [1, 'const pool = "a"', 'const pool = "b"'],
    [10, 'return 1', 'return 2'],
  ]) + section('src/app/two.ts', [[3, 'let x = 1', 'let x = 2']])

/** The split's tabs, more than a narrow bar holds. */
const TABS = [
  { key: 'detail', label: 'Details' },
  { key: 'logs', label: 'Logs' },
  { key: 'changes', label: 'Changes', count: 3 },
  { key: 'attempts', label: 'Attempts', count: 2 },
  { key: 'artifacts', label: 'Artifacts', count: 5 },
  { key: 'checkpoints', label: 'Checkpoints', count: 1 },
]

function Tabs(): ReactElement {
  const [current, setCurrent] = useState('detail')
  return (
    <div className="ps-narrow">
      <AgTabsEdge tabs={TABS} current={current} onSelect={setCurrent} />
    </div>
  )
}

// ----------------------------------------------------------------- mounts

export const mountStop = (): ReactElement => <StopRun task={runningTask()} what="agent" reload={() => undefined} overlay />

/** An agent leased and dispatched but no worker has started: the Stop dialog's third case. */
export const mountStopNotStarted = (): ReactElement => (
  <StopRun task={runningTask({ state: 'DISPATCHED', started_at: null })} what="agent" reload={() => undefined} overlay />
)

export const mountWait = (): ReactElement => <WaitPanel task={parkedTask(8)} now={WAIT_NOW} />

export const mountDiff = (): ReactElement => <DiffView patch={PATCH} name="swarm-work.patch" />

/** On a phone, where the viewer draws one file at a time and `All files` opens the sheet. */
export function mountDiffPhone(): ReactElement {
  stubPhone()
  return mountDiff()
}

export function mountToolMenu(): ReactElement {
  stubPhone()
  return (
    <LiveToolMenu>
      <button type="button">Fit to pane</button>
      <button type="button">Device: Desktop</button>
      <button type="button">Reload the page</button>
    </LiveToolMenu>
  )
}

export function mountTabs(): ReactElement {
  stubNarrow(260, 120)
  return <Tabs />
}

/** A prompt long enough to be clamped: the reader's own words, shown as content. */
export const LONG_PROMPT = Array.from({ length: 40 }, (_, i) => `Step ${i + 1}: read the pool table and note the queue depth of pool ${i + 1}.`).join('\n')

export const mountPrompt = (): ReactElement => <PromptText text={LONG_PROMPT} />

// ------------------------------------------------------- the checkpoint browser

const PREFIX = 'eng/task_a/att_1/ckpt-00001'
const URI = ['gs:/', 'swarm-artifacts-example', PREFIX, 'archive.tar.gz'].join('/')
type Files = Awaited<ReturnType<CheckpointLoaders['files']>>
type FileRead = Awaited<ReturnType<CheckpointLoaders['file']>>
type Member = NonNullable<Extract<Files, { status: 'ok' }>['data']['files']>[number]

function member(path: string, over: Partial<Member> = {}): Member {
  return { path, size: 10, mode: 0o644, type: 'file', link: null, unsafe: false, undecodable: false, ...over }
}

const MEMBERS: Member[] = [
  member('progress', { type: 'dir', size: 0, mode: 0o755 }),
  member('progress/step-0001.md', { size: 32 }),
  member('state.json', { size: 40 }),
]

const STATE_JSON = JSON.stringify({ pools: { a: { depth: 3 } } })

const LOADERS: CheckpointLoaders = {
  files: () =>
    Promise.resolve<Files>({
      status: 'ok',
      fetchedAt: Date.now(),
      data: {
        task_id: 'task_a', tenant_id: 'eng', attempt_id: 'att_1', checkpoint_id: 'ckpt-00001', prefix: PREFIX,
        archive: { key: `${PREFIX}/archive.tar.gz`, uri: URI, bytes: 4096 },
        manifest: { status: 'present', detail: null, file_count: 2, archive_bytes: 4096, archive_sha256: 'abc', created_at: null, label: 'periodic', archive_key_agrees: true },
        status: 'ok', detail: null, files: MEMBERS, count: MEMBERS.length, truncated: false, truncated_reason: null,
        file_count_agrees: true, scanned_bytes: 4096, inflated_bytes: 20480,
        limits: { entries: 5000, scan_bytes: 268435456, inflate_bytes: 1073741824 },
      },
    }),
  file: (_t: string, _a: string, _c: string, path: string) => {
    const text = path === 'state.json' ? STATE_JSON : '# step 1\n'
    return Promise.resolve<FileRead>({
      status: 'ok',
      fetchedAt: Date.now(),
      data: {
        task_id: 'task_a', tenant_id: 'eng', attempt_id: 'att_1', checkpoint_id: 'ckpt-00001', path,
        content_type: path.endsWith('.json') ? 'application/json' : 'text/plain',
        member: { type: 'file', mode: 0o644, size: text.length },
        artifact: { name: path, bytes: text.length, uri: URI },
        status: 'ok', detail: null, key: `${PREFIX}/archive.tar.gz`, uri: URI, content: text,
        total_bytes: text.length, offset: 0, returned_bytes: text.length, next_offset: null, truncated: false,
        redacted: false, redaction_count: 0, redaction: { applied_at_read_time: true, rules: 11 },
      },
    })
  },
}

export const mountCheckpoint = (): ReactElement => (
  <CheckpointBrowser taskId="task_a" attemptId="att_1" checkpointId="ckpt-00001" onClose={() => undefined} loaders={LOADERS} />
)

// ------------------------------------------------------------ the transcript

const TRANSCRIPT_TASK = 'task_7e21c0d4a95b4f3e8c02'

function step(n: number, over: Partial<TranscriptStep>): TranscriptStep {
  return {
    id: `L${n}:0`, line_offset: n * 100, block: 0, kind: 'text', role: 'assistant', parent_tool_use_id: null,
    text: null, tool: null, tool_result: null, meta: null, truncated_fields: [], raw: null, ...over,
  }
}

/** One of each fold a transcript step draws: progress events, a record, thinking, a tool call and its result, a result whose call is in another window. */
const STEPS: TranscriptStep[] = [
  step(1, { kind: 'other', role: 'system', meta: { raw_type: 'tool_progress' } }),
  step(2, { kind: 'other', role: 'system', meta: { raw_type: 'tool_progress' } }),
  step(3, { kind: 'text', text: 'Reading the pool table.', raw: '{"type":"assistant","text":"Reading the pool table."}' }),
  step(4, { kind: 'thinking', text: 'Pool a first.' }),
  step(5, { kind: 'tool_call', tool: { id: 'tu_1', name: 'Bash', input: '{"command":"ls pools"}' } }),
  step(6, { kind: 'tool_result', role: 'user', tool_result: { tool_use_id: 'tu_1', is_error: false, content: 'pool-a\npool-b', images: 0 } }),
  step(7, { kind: 'tool_result', role: 'user', tool_result: { tool_use_id: 'tu_0', is_error: true, content: 'exit 1', images: 0 } }),
]

const LOG_ATTEMPT = { status: 'latest' as const, known: true, generation: 1, created_at: at(0), completed_at: at(30), exit_code: 0 }

/** A finished attempt's final transcript, one window of two: `next window` reads the second. */
function transcriptRead(): TaskTranscript {
  return {
    task_id: TRANSCRIPT_TASK, tenant_id: 'eng', attempt_id: 'att_1', attempt: LOG_ATTEMPT, read_at: at(31),
    stream: {
      stream: 'agent_stdout', source: 'final', status: 'ok', detail: null,
      uri: ['gs:/', 'swarm-artifacts-example', 'tenants/eng/tasks', TRANSCRIPT_TASK, 'attempts/att_1/agent_stdout.log'].join('/'),
      object_updated_at: at(30), age_seconds: 60, total_bytes: 2000, offset: 0, returned_bytes: 1000, next_offset: 1000,
      truncated: true, tail_window: null,
    },
    format: 'claude-stream-json', steps: STEPS, complete: false, window_starts_mid_stream: false, skipped_lines: 0,
    answer_in_window: false, redaction: { applied_at_read_time: true, rules: 11 }, redaction_count: 0,
  }
}

/** Inside the log dock: its marks provided, so the transcript's facts fold as the dock folds them. */
const DOCK: LogMarks = { needle: '', current: null, wrap: false, gaps: {} }

/** The log dock's transcript view on one finished attempt, drawn as the Logs tab draws it beside the live browser. */
export function mountTranscript(): ReactElement {
  const t = transcriptRead()
  const v: LogFeed = {
    task: task({ id: TRANSCRIPT_TASK, state: 'SUCCEEDED', runner_profile: 'claude-code', started_at: at(0), completed_at: at(30) }),
    attemptId: null,
    transcript: { status: 'ok', data: t, fetchedAt: Date.parse(at(31)) },
    logs: {
      status: 'ok',
      fetchedAt: Date.parse(at(31)),
      data: {
        task_id: TRANSCRIPT_TASK, tenant_id: 'eng', attempt_id: 'att_1', attempt: LOG_ATTEMPT, read_at: at(31), streams: [],
        prefix: `tenants/eng/tasks/${TRANSCRIPT_TASK}/attempts/att_1/`, redaction: { applied_at_read_time: true, rules: 11 },
      },
    },
  }
  return (
    <LogMarksContext.Provider value={DOCK}>
      <LogBody v={v} view="transcript" reading={{ fetchedAt: Date.parse(at(31)), pollMs: null }} now={Date.parse(at(32))} />
    </LogMarksContext.Provider>
  )
}

// ------------------------------------------------------- the standalone drawer

/** A button that opens the agent drawer on its own -- not inside the split -- as a page that hosts it alone would. */
function StandaloneDrawer({ taskId }: Readonly<{ taskId: string }>): ReactElement {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" className="ps-open-drawer" onClick={() => setOpen(true)}>
        Open agent
      </button>
      {open && <AgentDetailScreen taskId={taskId} onClose={() => setOpen(false)} />}
    </>
  )
}

/**
 * The fixture build's running agent, in the standalone drawer (its own
 * `.drawer` and close button), read at the fixture build's own clock as a
 * walk is: on today's clock its last event is weeks old and it reads silent.
 */
export function mountDrawer(): ReactElement {
  vi.setSystemTime(new Date(FIXTURE_CLOCK))
  return <StandaloneDrawer taskId="task_a073aff5" />
}

// -------------------------------------------------- a merge step's re-checks

/** A merge step handed to a person after three CI re-checks: the attempts page folds the two earlier ones. */
export function mountReChecks(): ReactElement {
  const t = task({
    id: 'task_9b40d6e2f1a84c37b5e1',
    state: 'PARKED',
    park_reason: 'HUMAN_REQUIRED',
    runner_profile: 'merge',
    waiting: {
      reason: 'HUMAN_REQUIRED',
      summary: 'CI on PR #3412 waited 48 m over 3 re-checks',
      since: null,
      resumes_when: 'a member of the workspace calls human-ready',
      detail: { park_phase: 'ci_wait_stuck', re_checks: 3 },
      history: [],
    },
  } as Partial<Task>)
  const rows = [1, 2, 3].map((n) => attempt(n, { task_id: t.id, exit_code: 75, error: 'CI_PENDING', completed_at: at(n * 16 + 1) }))
  served.attempts = { status: 'ok', fetchedAt: Date.now(), data: { attempts: rows } }
  served.detail = { status: 'ok', fetchedAt: Date.now(), data: { task: t, events: [], eventsDetail: null } as unknown as AgentDetail }
  restores.push(() => {
    served.attempts = null
    served.detail = null
  })
  return <AttemptTimelineScreen taskId={t.id} />
}
