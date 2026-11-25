// The agent section's click-only states the fixture build never draws
// (issue 439; the registry is prosestates.agent.tsx): each screen mounted on
// hand-built props, with what it needs stubbed -- fetch for an action's
// error, the phone's media query for the phone-only tool menu, and the
// widths jsdom does not lay out for the tab bar's More. `restoreAgentStubs`
// undoes all of it after each state (prose.states.agent.test.tsx).

import { useState, type ReactElement } from 'react'

import { AgTabsEdge } from '../agent-split/tabs'
import { PromptText } from '../Artifacts'
import type { CancelResult } from '../api'
import type { ApiError, Result } from '../fetch'
import { CheckpointBrowser, type CheckpointLoaders } from '../CheckpointBrowser'
import { DiffView } from '../diff/DiffView'
import { LiveToolMenu } from '../LiveToolMenu'
import { StopRun } from '../StopRun'
import type { Task, Waiting, WaitingHistoryEntry } from '../types'
import { WaitPanel } from '../WaitPanel'
import { at, task } from './runfixture'

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
export const refuse: { cancel: Result<CancelResult> | null } = { cancel: null }

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
