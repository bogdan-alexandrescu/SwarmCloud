// SPLIT (part of #2688, 2026-11-07): this file and `artifacts.pane.test.tsx` were one file,
// 182 s alone with three workers, over the ~150 s line under a CI shard. Split
// along its describe blocks; each file keeps its own fixtures and shares no state.

// #184: THE AGENT DRAWER'S ARTIFACTS PANE, AS BEHAVIOUR, THROUGH THE REAL APP.
//
// THE DEFECT, measured on `task_73b5f4d9ca3641fbb914` (claude-code, SUCCEEDED,
// 2026-09-25): the agent's 7,124-character answer sat in an artifact the drawer
// listed by name beside `copy gsutil`; the panel headed "Output, as the agent
// wrote it" showed the RUNNER's streams; nothing drew an image. The owner's
// decisions: tabs Details / Attempts / Artifacts; Inputs, Outputs (the answer
// as Markdown FIRST, then every file with an inline viewer by kind, a download
// and `copy gsutil`), Logs (stdout, stderr, the transcript as steps); live
// every 5 s with each read's age; every read and download through the API.
//
// WHY THROUGH <App /> AND A STUBBED fetch, not the component with its loaders
// mocked. The pane is a ROUTE (`#work/task/<id>/artifacts`), and the loaders are
// the seam the contract fixes: a test that mocked them would agree with any URL
// the screen built, including a signed GCS one. Here the fetch stub answers by
// PATH, so a request to a path the contract does not name gets no answer and
// the assertion that needs it fails by name.
//
// MUTATIONS, each a case below: drop the `artifacts` pane from `fromHash`; put
// `Detail` back; render the answer after the file list, or as raw text; point a
// download at the `uri`; pretty-print a partial JSON window; drop the image's
// `onError`; draw a pending listing as `none uploaded`; drop a tool result's
// pairing or its `error` word; stop polling while RUNNING, or keep polling a
// settled finish; read the transcript's age off the browser clock; ask the
// listing for no `limit` (the server's default page is 50); draw a listing the
// route cut short as the whole manifest; draw `task.input` instead of the
// API's masked copy (`/v1/tasks/{id}/input`, #184 follow-up), or fall back to
// it when the copy is not served.

import STYLES from '../styles.css?raw'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, waitFor } from '@testing-library/react'

import { Markdown } from '../ArtifactViewer'
import type { Task } from '../types'
import { cascade, type CascadeEnv } from './cssgate'
import { task as baseTask } from './runfixture'

// The page is stubbed for speed; it still draws the open agent App hosts in its
// inspector (#3146). Mocked again after each `resetModules`, so the stub reads
// the same context module as the App imported after it.
const agentsStub = vi.hoisted(() => async () => {
  const { useContext } = await import('react')
  const { HostedAgent } = await import('../work/hostedAgent')
  return { AgentsScreen: () => useContext(HostedAgent)?.view ?? null }
})
vi.mock('../Agents', agentsStub)

// THE CONSOLE'S MODULES ARE BUILT ONCE, HERE, not inside the first test: each
// test re-imports App after `resetModules`, and the first import also
// transforms App's whole graph -- the agent hosted in the Agents page's
// inspector included -- which under a loaded runner took the first test past
// its 30s. Later imports re-run built modules.
beforeAll(async () => {
  await import('../App')
}, 120_000)

const REF = 'task_73b5f4d9ca3641fbb914'
const iso = (msAgo: number) => new Date(Date.now() - msAgo).toISOString()

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

const uri = (id: string, name: string) => `gs://swarm-artifacts-example-project/tenants/eng/tasks/${id}/attempts/att_fe27/artifacts/${name}`

function attemptBlock(ended: boolean) {
  return {
    status: 'latest',
    known: true,
    generation: 1,
    created_at: iso(120_000),
    completed_at: ended ? iso(30_000) : null,
    exit_code: ended ? 0 : null,
  }
}

function task(over: Partial<Task> = {}) {
  return baseTask({
    id: REF,
    tenant_id: 'eng',
    state: 'SUCCEEDED',
    runner_profile: 'claude-code',
    started_at: iso(120_000),
    completed_at: iso(30_000),
    repository_url: 'https://github.com/example-org/example-repo',
    repository_ref: 'main',
    input: { prompt: 'Audit the capacity code.\n\nSay what reserve() guarantees.' },
    result_summary: {
      artifacts: [],
      logs: {},
      git: { base: '0a09be9f3c1d2e4b5a6978' },
      agent_streams: { stdout: 'claude-code.stdout.log', stderr: 'claude-code.stderr.log', transcript: 'claude-transcript.json', transcript_skipped: null },
    },
    ...over,
  })
}

const ANSWER_MD = '# Findings\n\n## Capacity\n\n- reserve is all-or-nothing\n- concurrency counts from `LEASED`\n'

function answer(over: Record<string, unknown> = {}) {
  return {
    task_id: REF,
    tenant_id: 'eng',
    attempt_id: 'att_fe27',
    attempt: attemptBlock(true),
    read_at: new Date().toISOString(),
    status: 'ok',
    source: 'agent_result_event',
    object: { stream: 'agent_stdout', source: 'artifact', uri: uri(REF, 'claude-code.stdout.log'), object_updated_at: iso(30_000) },
    format: 'markdown',
    content: ANSWER_MD,
    complete: true,
    is_error: false,
    subtype: 'success',
    stop_reason: 'end_turn',
    terminal_reason: 'completed',
    num_turns: 7,
    bytes: 90,
    redacted: false,
    redaction_count: 0,
    detail: null,
    ...over,
  }
}

function listing(over: Record<string, unknown> = {}) {
  const e = (name: string, bytes: number, kind: string, role: string | null = null) => ({
    name,
    bytes,
    uri: uri(REF, name),
    attempt_id: 'att_fe27',
    kind,
    content_type: kind === 'image' ? 'image/png' : kind === 'binary' ? null : 'text/plain',
    role,
  })
  return {
    task_id: REF,
    complete: true,
    attempt_id: 'att_fe27',
    artifact_bytes: 30_000,
    artifacts_skipped: [],
    artifacts: [
      e('report.md', 120, 'markdown'),
      e('claude-code.stdout.log', 9397, 'log', 'agent_stdout'),
      e('claude-code.stderr.log', 0, 'log', 'agent_stderr'),
      e('claude-transcript.json', 10181, 'json', 'agent_transcript'),
      e('big.json', 900_000, 'json'),
      e('screens/home.png', 2048, 'image'),
      e('bundle.tar', 4096, 'binary'),
    ],
    ...over,
  }
}

function stream(over: Record<string, unknown> = {}) {
  return {
    stream: 'agent_stdout',
    source: 'artifact',
    status: 'ok',
    detail: null,
    uri: uri(REF, 'claude-code.stdout.log'),
    object_updated_at: iso(30_000),
    age_seconds: 30,
    total_bytes: 9397,
    offset: 0,
    returned_bytes: 9397,
    next_offset: null,
    truncated: false,
    tail_window: null,
    ...over,
  }
}

function step(n: number, over: Record<string, unknown> = {}) {
  return {
    id: `L${n}:0`,
    line_offset: n * 100,
    block: 0,
    kind: 'text',
    role: 'assistant',
    parent_tool_use_id: null,
    text: null,
    tool: null,
    tool_result: null,
    meta: null,
    truncated_fields: [],
    raw: null,
    ...over,
  }
}

/** The reference task as it is: one `--output-format json` result object, read from the artifact. */
function transcript(over: Record<string, unknown> = {}) {
  return {
    task_id: REF,
    tenant_id: 'eng',
    attempt_id: 'att_fe27',
    attempt: attemptBlock(true),
    read_at: new Date().toISOString(),
    stream: stream(),
    format: 'claude-json',
    steps: [step(0, { kind: 'result', role: null, text: ANSWER_MD, meta: { subtype: 'success', is_error: false, num_turns: 7 } })],
    complete: true,
    window_starts_mid_stream: false,
    skipped_lines: 0,
    answer_in_window: true,
    redaction: { applied_at_read_time: true, rules: 12 },
    redaction_count: 0,
    ...over,
  }
}

function logStream(name: string, over: Record<string, unknown> = {}) {
  return {
    stream: name,
    source: 'final',
    status: 'ok',
    detail: null,
    key: `tenants/eng/tasks/${REF}/attempts/att_fe27/logs/${name}.log`,
    uri: `gs://swarm-artifacts-example-project/tenants/eng/tasks/${REF}/attempts/att_fe27/logs/${name}.log`,
    content: '',
    total_bytes: 0,
    offset: 0,
    returned_bytes: 0,
    next_offset: null,
    truncated: false,
    redacted: false,
    redaction_count: 0,
    tail_window: null,
    object_updated_at: iso(30_000),
    age_seconds: 30,
    ...over,
  }
}

function logs(streams: unknown[], ended = true) {
  return {
    task_id: REF,
    tenant_id: 'eng',
    attempt_id: 'att_fe27',
    attempt: attemptBlock(ended),
    read_at: new Date().toISOString(),
    streams,
    prefix: `tenants/eng/tasks/${REF}/attempts/att_fe27/`,
    redaction: { applied_at_read_time: true, rules: 12 },
  }
}

/**
 * `GET /v1/tasks/{id}/input`: the task's input as the API masked it (#184
 * follow-up). The default is the reference task's clean prompt, so its count
 * is a measured zero.
 */
function inputCopy(over: Record<string, unknown> = {}) {
  const prompt = 'Audit the capacity code.\n\nSay what reserve() guarantees.'
  return {
    task_id: REF,
    tenant_id: 'eng',
    read_at: new Date().toISOString(),
    prompt_key: 'string',
    prompt: { text: prompt, redaction_count: 0 },
    rest: null,
    full: { text: JSON.stringify({ prompt }, null, 2), redaction_count: 0 },
    redacted: false,
    redaction_count: 0,
    redaction: { applied_at_read_time: true, rules: 12 },
    ...over,
  }
}

const FINAL_LOGS = logs([
  logStream('agent_stderr'),
  logStream('stdout'),
  logStream('stderr', {
    content: '{"severity":"INFO","component":"claude-code-runner","message":"child started"}',
    total_bytes: 80,
    returned_bytes: 80,
  }),
])

/** What each path answers. A function sees the query; anything not named here never answers. */
type Route = unknown | ((q: URLSearchParams) => unknown)

interface Api {
  calls: string[]
  count: (path: string) => number
}

async function openPane(routes: Record<string, Route>, hash = `#work/task/${REF}/artifacts`): Promise<Api> {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  vi.doMock('../Agents', agentsStub)
  const calls: string[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const raw = String(input)
    calls.push(raw)
    const u = new URL(raw, 'http://ui.test')
    const answer = routes[u.pathname]
    if (answer === undefined) return new Promise<Response>(() => {})
    const body = typeof answer === 'function' ? (answer as (q: URLSearchParams) => unknown)(u.searchParams) : answer
    return body instanceof Response ? body : json(body)
  }) as unknown as typeof fetch
  window.location.hash = hash
  const { App } = await import('../App')
  render(<App />)
  return { calls, count: (path) => calls.filter((c) => new URL(c, 'http://ui.test').pathname === path).length }
}

function finishedRoutes(over: Record<string, Route> = {}): Record<string, Route> {
  return {
    [`/v1/tasks/${REF}`]: { task: task() },
    [`/v1/tasks/${REF}/artifacts`]: listing(),
    [`/v1/tasks/${REF}/answer`]: answer(),
    [`/v1/tasks/${REF}/transcript`]: transcript(),
    [`/v1/tasks/${REF}/logs`]: FINAL_LOGS,
    [`/v1/tasks/${REF}/input`]: inputCopy(),
    ...over,
  }
}

function drawer(): HTMLElement {
  const el = document.querySelector<HTMLElement>('.ctl-drawer')
  expect(el, 'no agent drawer is open').not.toBeNull()
  return el!
}

/** Selects one of the agent's tabs, by its label, unless it is selected already. */
function choose(label: string): void {
  const t = [...drawer().querySelectorAll<HTMLButtonElement>('[role="tab"]')].find((x) => x.querySelector('.c-tab-label')?.textContent === label)
  expect(t, `no ${label} tab`).toBeTruthy()
  if (t!.getAttribute('aria-selected') !== 'true') fireEvent.click(t!)
}

function section(title: string): HTMLElement {
  // THE LOG IS A TAB (U11a, owner decision 2026-10-04): the reader opens it,
  // and the Artifacts pane's own sections are on the Artifacts tab.
  if (title === 'Log') {
    choose('Logs')
    const logs = drawer().querySelector<HTMLElement>('section[aria-label="Logs"]')
    expect(logs, 'no Logs tab content in the drawer').not.toBeNull()
    return logs!
  }
  // Back from the Logs tab to the pane these sections are on.
  if (drawer().querySelector('section[aria-label="Logs"]') !== null) choose('Artifacts')
  const s = [...drawer().querySelectorAll<HTMLElement>('section')].find((x) => x.querySelector('h2')?.textContent === title)
  expect(s, `no ${title} section in the drawer`).toBeTruthy()
  return s!
}

async function sectionReady(title: string, text: string | RegExp): Promise<HTMLElement> {
  return waitFor(() => {
    const s = section(title)
    expect(s.textContent ?? '').toMatch(text)
    return s
  })
}

function row(within: HTMLElement, name: string): HTMLTableRowElement {
  const r = [...within.querySelectorAll<HTMLTableRowElement>('tbody tr')].find((tr) =>
    (tr.querySelector('th')?.textContent ?? '').trim().startsWith(name),
  )
  expect(r, `no row for ${name}`).toBeTruthy()
  return r!
}

afterEach(() => {
  vi.useRealTimers()
  window.location.hash = ''
})

/**
 * A RUNNER WITH NO AGENT CLI OPENS THE LOG ON ITS RUNNER LOG (viewers.html A:
 * "opens on Transcript for claude-code and codex, and on Runner for runners
 * with no agent CLI"). The transcript view is one click away; this takes it.
 */
async function openTranscriptOfNoCliRunner(): Promise<void> {
  const dock = await sectionReady('Log', /Runner/)
  const pressed = () => [...dock.querySelectorAll<HTMLButtonElement>('[aria-label="Which log"] button')].find((b) => b.getAttribute('aria-pressed') === 'true')
  await waitFor(() => expect(pressed()?.textContent, 'a runner with no agent CLI did not open on Runner').toBe('Runner'))
  const t = [...dock.querySelectorAll<HTMLButtonElement>('[aria-label="Which log"] button')].find((b) => b.textContent === 'Transcript')
  fireEvent.click(t!)
}

// ---------------------------------------------------------------------------
// Logs
// ---------------------------------------------------------------------------

describe('Logs: the transcript as steps, the agent’s streams, the runner’s', () => {
  it('draws a tool call and its result as one collapsed row, marks a failed one, and nests a sub-agent', async () => {
    await openPane(
      finishedRoutes({
        [`/v1/tasks/${REF}/transcript`]: transcript({
          stream: stream({ source: 'final' }),
          format: 'claude-stream-json',
          window_starts_mid_stream: true,
          skipped_lines: 2,
          complete: false,
          steps: [
            step(1, { text: 'I will read the contract.' }),
            step(2, { kind: 'tool_call', tool: { id: 'toolu_1', name: 'Read', input: '{\n  "file_path": "CONTRACT.md"\n}' } }),
            step(3, { kind: 'tool_result', role: 'user', tool_result: { tool_use_id: 'toolu_1', is_error: true, content: 'ENOENT', images: 0 } }),
            step(4, { kind: 'tool_call', tool: { id: 'toolu_2', name: 'Task', input: '{}' } }),
            step(5, { text: 'The sub-agent reports back.', parent_tool_use_id: 'toolu_2' }),
            step(6, { kind: 'result', role: null, text: 'Done.', meta: { subtype: 'success', is_error: false, num_turns: 3 } }),
          ],
        }),
      }),
    )
    // THE TRANSCRIPT IS A TRACE (owner pick, 2026-11-05): one timeline row per
    // event, a tool call and its result one row, the picked one in full beside it.
    const logsSection = await sectionReady('Log', /Read/)
    expect(logsSection.textContent).toMatch(/earlier steps are not in this window/)
    const rows = [...logsSection.querySelectorAll<HTMLElement>('.trc-row')]
    const read = rows.find((r) => r.querySelector('.trc-label')?.textContent?.includes('Read'))
    expect(read, 'the Read tool call is not an event').toBeTruthy()
    expect(read!.classList.contains('is-error'), 'the failed result is not paired with its call, or not called an error').toBe(true)
    expect(logsSection.textContent, 'the paired result was drawn a second time').not.toMatch(/its call is not in this read/)
    expect(rows.some((r) => /The sub-agent reports back/.test(r.textContent ?? '')), 'the sub-agent’s message is not an event').toBe(true)
    expect(logsSection.textContent).toMatch(/2 lines skipped/)
  })

  it('labels a run that recorded only its final result, rather than drawing a short transcript', async () => {
    await openPane(finishedRoutes())
    const logsSection = await sectionReady('Log', /recorded only its final result/)
    expect(logsSection.textContent).toMatch(/turn-by-turn steps need stream-json/)
  })

  it('draws the agent’s empty stderr as a measured zero, and the runner’s own log behind its own choice', async () => {
    await openPane(finishedRoutes())
    await sectionReady('Log', /Transcript/)
    const choose = (label: string) => {
      const b = [...section('Log').querySelectorAll<HTMLButtonElement>('[aria-label="Which log"] button')].find((x) => (x.querySelector('.ag-logs-long')?.textContent ?? x.textContent) === label)
      expect(b, `no ${label} choice`).toBeTruthy()
      fireEvent.click(b!)
    }
    choose('Agent stderr')
    await waitFor(() => expect(row(section('Log'), 'agent stderr').querySelector('td[data-label="Size"] .ctl-mark.is-zero')).not.toBeNull(), { timeout: 5000 })
    choose('Runner')
    await waitFor(() => expect(section('Log').querySelector('pre.logwin-body')?.textContent).toMatch(/child started/))
    // The runner's own streams, named the runner's (G2-07).
    row(section('Log'), 'runner stdout')
    row(section('Log'), 'runner stderr')
  })

  it('says a runner with no agent CLI has no agent stream, which is not a missing one', async () => {
    await openPane(
      finishedRoutes({
        [`/v1/tasks/${REF}`]: { task: task({ runner_profile: 'mock' }) },
        [`/v1/tasks/${REF}/transcript`]: transcript({ stream: stream({ status: 'not_applicable', source: null, total_bytes: null, returned_bytes: 0 }), format: null, steps: null, complete: false }),
        [`/v1/tasks/${REF}/logs`]: logs([logStream('agent_stderr', { status: 'not_applicable', source: null, content: null, total_bytes: null }), logStream('stdout'), logStream('stderr')]),
      }),
    )
    await openTranscriptOfNoCliRunner()
    const logsSection = await sectionReady('Log', /no agent CLI/)
    expect(logsSection.querySelector('.ctl-mark.is-absent'), 'not applicable is drawn as not measured').toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Parity with #188: what the backend added after this pane was written
// ---------------------------------------------------------------------------
//
// #188's review fix-up added `capture_truncated` to `/transcript` and
// `/answer`, a `system` step with `meta.subtype: "capture_truncated"` where the
// worker's capture dropped the middle of a run, an `other` step with
// `meta.oversize` for a line longer than the window (its reason in
// `stream.detail`), and `invalid_utf8_bytes` on `/artifacts/content`. It also
// serves `tool.id` and `tool_result.tool_use_id` as null when an event carried
// no string there. Each case below is a shape #188's own tests pin, answered
// by the stub exactly as the route answers it.
//
// MUTATIONS: stop reading `capture_truncated`; draw an `ok` transcript's
// `stream.detail` nowhere; drop the notice step's text; draw an oversize line
// as a bare `other`; join a result to a call because both ids are null; drop
// the `not utf-8` fact or draw it at zero.

const CUT_DETAIL =
  "the agent's output passed its capture size cap: the capture kept the start and the end of the stream and dropped the middle, with a notice where, so this is not the whole run"
const OVERSIZE_DETAIL =
  "one line of the agent's output is longer than this window, so it is shown as a single step with no text; raise limit_bytes (up to 4 MiB) to read it"

describe('the pane reads what #188 serves about a cut capture, an over-long line and bytes that are not UTF-8', () => {
  it('says a transcript whose capture was cut is not the whole run, and draws the capture’s notice in its own words', async () => {
    await openPane(
      finishedRoutes({
        [`/v1/tasks/${REF}/transcript`]: transcript({
          stream: stream({ source: 'final', detail: CUT_DETAIL }),
          format: 'claude-stream-json',
          complete: false,
          capture_truncated: true,
          steps: [
            step(1, { text: 'Reading the repository.' }),
            step(2, {
              kind: 'system',
              role: 'system',
              text: '[swarm] output truncated: 110592 bytes dropped here',
              meta: { subtype: 'capture_truncated' },
            }),
            step(3, { kind: 'result', role: null, text: 'Done.', meta: { subtype: 'success', is_error: false, num_turns: 40 } }),
          ],
        }),
      }),
    )
    const logsSection = await sectionReady('Log', /cut at its cap/)
    const capture = [...logsSection.querySelectorAll('.trc-notes li')].find((li) => /cut at its cap/.test(li.textContent ?? ''))
    expect(capture?.querySelector('.ctl-mark.is-partial'), 'a cut capture is not marked partial').not.toBeNull()
    expect(logsSection.textContent, 'the server’s reason for an ok window is drawn nowhere').toContain(CUT_DETAIL)
    const notice = [...logsSection.querySelectorAll('.trc-row')].find((r) => /capture_truncated/.test(r.textContent ?? ''))
    expect(notice, 'the capture’s notice is not an event').toBeTruthy()
    expect(notice!.textContent, 'the notice’s own words are not drawn').toMatch(/110592 bytes dropped/)
  })

  it('draws a line longer than the window in words, with the server’s reason, never as a bare other', async () => {
    await openPane(
      finishedRoutes({
        [`/v1/tasks/${REF}/transcript`]: transcript({
          stream: stream({ source: 'final', detail: OVERSIZE_DETAIL, next_offset: 524_288, truncated: true, total_bytes: 9_000_000 }),
          format: null,
          complete: false,
          capture_truncated: null,
          steps: [step(0, { kind: 'other', role: null, meta: { oversize: true } })],
        }),
      }),
    )
    const logsSection = await sectionReady('Log', /line longer than this window/)
    expect(logsSection.textContent).toContain(OVERSIZE_DETAIL)
    const s = logsSection.querySelector('.trc-row')
    expect(s?.textContent, 'an over-long line is drawn as an event with nothing in it').toMatch(/line longer than this window/)
    expect(s?.querySelector('.trc-label')?.textContent, 'an over-long line is drawn as a bare other').not.toMatch(/^\s*other/)
    expect(logsSection.textContent, 'a capture nobody said was cut is drawn as cut').not.toMatch(/cut at its cap/)
  })

  it('never joins a tool result to a call because both ids are null', async () => {
    await openPane(
      finishedRoutes({
        [`/v1/tasks/${REF}/transcript`]: transcript({
          stream: stream({ source: 'final' }),
          format: 'claude-stream-json',
          steps: [
            step(1, { kind: 'tool_call', tool: { id: null, name: 'Read', input: '{}' } }),
            step(2, { kind: 'tool_result', role: 'user', tool_result: { tool_use_id: null, is_error: null, content: 'the file', images: 0 } }),
          ],
        }),
      }),
    )
    const logsSection = await sectionReady('Log', /Read/)
    const rows = [...logsSection.querySelectorAll<HTMLElement>('.trc-row')]
    expect(rows.length, 'two unknown ids were joined as a call and its result').toBe(2)
    expect(logsSection.textContent, 'the unjoined result was dropped').toMatch(/its call is not in this read/)
  })

  it('marks an answer whose capture was cut, and says why an ended run left none when the capture was cut', async () => {
    await openPane(finishedRoutes({ [`/v1/tasks/${REF}/answer`]: answer({ capture_truncated: true, detail: `${CUT_DETAIL}; the result event, its last line, was kept whole` }) }))
    const out = await sectionReady('Outputs', /capture cut/)
    const head = out.querySelector('.arts-answer .ctl-card-note')
    expect(head?.querySelector('.ctl-mark.is-partial'), 'a cut capture is not marked on the answer').not.toBeNull()
    expect(out.querySelector('.arts-answer .art-md, .arts-answer h4, .arts-answer li'), 'the answer itself was withheld').not.toBeNull()
  })

  it('says a finished run with no answer had its capture cut, in the server’s words', async () => {
    const why = `${CUT_DETAIL}, and no result event is in what was kept; the attempt ended and left neither a result event nor a runner summary`
    await openPane(
      finishedRoutes({
        [`/v1/tasks/${REF}/answer`]: answer({ status: 'absent', source: null, object: null, format: null, content: null, complete: null, is_error: null, subtype: null, num_turns: null, capture_truncated: true, detail: why }),
      }),
    )
    const out = await sectionReady('Outputs', /no answer recorded/)
    expect(out.textContent).toContain(why)
  })

  it('counts the bytes of a window that are not UTF-8, and draws nothing at zero', async () => {
    const content = (name: string) => ({
      task_id: REF,
      tenant_id: 'eng',
      attempt_id: 'att_fe27',
      artifact: { name, bytes: 40, uri: uri(REF, name) },
      status: 'ok',
      detail: name === 'report.md' ? '3 bytes of this window are not UTF-8 and are shown as U+FFFD; the raw route (/artifacts/raw) serves them exactly' : null,
      key: null,
      uri: uri(REF, name),
      content: name === 'report.md' ? 'caf\uFFFD \uFFFD\uFFFD\n' : '{}',
      total_bytes: 40,
      offset: 0,
      returned_bytes: 40,
      next_offset: null,
      truncated: false,
      redacted: false,
      redaction_count: 0,
      redaction: { applied_at_read_time: true, rules: 12 },
      kind: name === 'report.md' ? 'markdown' : 'json',
      content_type: 'text/plain',
      invalid_utf8_bytes: name === 'report.md' ? 3 : 0,
    })
    await openPane(finishedRoutes({ [`/v1/tasks/${REF}/artifacts/content`]: (q: URLSearchParams) => content(q.get('name') ?? '') }))
    const out = await sectionReady('Outputs', /report\.md/)
    const fact = () => [...out.querySelectorAll('.art-viewer li.ctl-fact')].find((li) => li.querySelector('b')?.textContent === 'not utf-8')
    fireEvent.click(row(out, 'report.md').querySelector('button.art-open')!)
    await waitFor(() => expect(fact(), 'bytes shown as U+FFFD are not counted').toBeTruthy())
    expect(fact()!.textContent).toMatch(/3/)
    expect(fact()!.querySelector('.ctl-mark.is-partial')).not.toBeNull()
    fireEvent.click(row(out, 'claude-transcript.json').querySelector('button.art-open')!)
    // A whole JSON file opens as its tree (viewers.html A).
    await waitFor(() => expect(out.querySelector('.art-viewer [role="tree"]')?.textContent).toBe('{ 0 keys }'))
    expect(fact(), 'a zero count is drawn as a finding').toBeUndefined()
  })
})

// ---------------------------------------------------------------------------
// Live
// ---------------------------------------------------------------------------

describe('live: every 5 s while the task runs, with each read’s age, and not after it has settled', () => {
  it('re-reads the transcript and logs on the 5 s clock, shows the server’s age, and stops once the finish is in', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
    let finished = false
    const running = () => task({ state: 'RUNNING', completed_at: null, result_summary: null })
    const api = await openPane({
      [`/v1/tasks/${REF}`]: () => ({ task: finished ? task({ completed_at: new Date().toISOString() }) : running() }),
      [`/v1/tasks/${REF}/artifacts`]: listing(),
      [`/v1/tasks/${REF}/answer`]: () =>
        finished ? answer() : answer({ status: 'not_yet', source: null, object: null, format: null, content: null, complete: null, is_error: null, subtype: null, num_turns: null, attempt: attemptBlock(false) }),
      [`/v1/tasks/${REF}/transcript`]: () =>
        finished
          ? transcript({ stream: stream({ source: 'final' }), format: 'claude-stream-json' })
          : transcript({ stream: stream({ source: 'live', age_seconds: 42 }), format: 'claude-stream-json', steps: [step(1, { text: 'Reading.' })], complete: false, attempt: attemptBlock(false) }),
      [`/v1/tasks/${REF}/logs`]: () =>
        finished
          ? FINAL_LOGS
          : logs([logStream('agent_stderr', { source: 'live' }), logStream('stdout', { source: 'live' }), logStream('stderr', { source: 'live' })], false),
    })
    const advance = async (ms: number) => {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ms)
      })
      for (let i = 0; i < 4; i++) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(0)
        })
      }
    }
    await advance(0)
    // The log is read on its own tab (U11a): open it, as a reader would.
    section('Log')
    await advance(0)
    const transcripts = `/v1/tasks/${REF}/transcript`
    const logReads = `/v1/tasks/${REF}/logs`
    expect(api.count(transcripts)).toBe(1)
    expect(section('Log').textContent, 'a live read does not say how old it is').toMatch(/published 42s ago/)

    await advance(5_000)
    expect(api.count(transcripts), 'the transcript was not re-read at 5 s').toBe(2)
    // The trace draws the transcript only: the streams are read when their view is picked.
    expect(api.count(logReads), 'the trace polled the streams it does not draw').toBe(0)
    expect(api.calls.some((c) => c.includes('stream=agent_stdout')), 'the raw agent stdout was polled with its view closed').toBe(false)

    finished = true
    await advance(5_000)
    expect(api.count(transcripts), 'the finish was not read').toBeGreaterThanOrEqual(3)
    // The trace reads the final record once more when it sees the task settled, then stops.
    await advance(30_000)
    const settled = api.count(transcripts)
    expect(settled).toBeLessThanOrEqual(4)
    await advance(30_000)
    expect(api.count(transcripts), 'the pane kept polling a settled finish').toBe(settled)
    expect(api.count(logReads)).toBe(0)
  })
})

// ---------------------------------------------------------------------------
// Phone width and the dark theme
// ---------------------------------------------------------------------------

/**
 * The pane's own block of the sheet, from its header to the next at-rule.
 * A SOURCE SCAN, for the reason typescale.test.ts gives: "no colour here is a
 * literal" is a property of the text, and jsdom resolves no `var()` and
 * evaluates no media query, so no computed style could answer it. The DOM
 * half is the case above it: the tables are the stacked records.
 */
function paneSheet(): string {
  const start = STYLES.indexOf('#184 -- the Artifacts pane')
  expect(start, 'the Artifacts pane has no block in styles.css').toBeGreaterThan(-1)
  const end = STYLES.indexOf('\n@media', start)
  const block = STYLES.slice(start, end === -1 ? undefined : end)
  // Comments removed: the block's header explains the rules in words.
  return block.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^[\s\S]*?\*\//, '')
}

/** Whether nothing from `el` up to the drawer is `display: none` at `env`, by the shipped sheet's cascade. */
function shownAt(el: Element, env: CascadeEnv): boolean {
  for (let node: Element | null = el; node !== null; node = node.parentElement) {
    if (cascade(STYLES, node, 'display', env).winner?.value === 'none') return false
    if (node.classList.contains('ctl-drawer')) return true
  }
  return true
}

describe('the pane holds at 390 wide and in the dark theme', () => {
  // WHY A CASCADE AND NOT ONLY THE BLOCK SCAN BELOW. The Details pane's CPU
  // rows passed a scan like it and still lost their age at 390, because the
  // rule that hid it (`.ctl-util-by { display: none }`) lives in another block
  // of the sheet. These ask the whole sheet what a 390px viewport displays.
  it('keeps every live read’s age on screen at 390', async () => {
    await openPane(
      finishedRoutes({
        [`/v1/tasks/${REF}`]: { task: task({ state: 'RUNNING', completed_at: null, result_summary: null }) },
        [`/v1/tasks/${REF}/answer`]: answer({ status: 'not_yet', source: null, object: null, format: null, content: null, complete: null, is_error: null, subtype: null, num_turns: null, attempt: attemptBlock(false) }),
        [`/v1/tasks/${REF}/transcript`]: transcript({ stream: stream({ source: 'live', age_seconds: 7 }), format: 'claude-stream-json', steps: [step(1, { text: 'Reading.' })], complete: false, attempt: attemptBlock(false) }),
        [`/v1/tasks/${REF}/logs`]: logs([logStream('agent_stderr', { source: 'live', content: 'warming up', total_bytes: 10, returned_bytes: 10, age_seconds: 4 }), logStream('stdout', { source: 'live' }), logStream('stderr', { source: 'live' })], false),
      }),
    )
    await sectionReady('Log', /published \d+s ago/)
    const ages = () => [...section('Log').querySelectorAll<HTMLElement>('.ctl-sub')].filter((s) => /published \d+s ago/.test(s.textContent ?? ''))
    expect(ages().length, 'the transcript shows no age').toBeGreaterThan(0)
    for (const a of ages()) expect(shownAt(a, { width: 390 }), `${a.textContent} is hidden at 390`).toBe(true)

    const stderr = [...section('Log').querySelectorAll<HTMLButtonElement>('[aria-label="Which log"] button')].find((b) => b.querySelector('.ag-logs-long')?.textContent === 'Agent stderr')
    fireEvent.click(stderr!)
    // #172: a live stream's Age cell leads with how long ago the tail object
    // changed, with `live` on the line under it.
    const cell = await waitFor(() => {
      const c = row(section('Log'), 'agent stderr').querySelector<HTMLElement>('td[data-label="Age"]')
      expect(c?.textContent).toMatch(/^\d+s ago/)
      expect(c?.querySelector('.ctl-sub')?.textContent).toBe('live')
      return c!
    })
    expect(shownAt(cell, { width: 390 }), 'a live stream’s age is hidden at 390').toBe(true)
  })

  it('keeps every file’s download and copy on screen at 390', async () => {
    await openPane(finishedRoutes())
    const out = await sectionReady('Outputs', /bundle\.tar/)
    for (const e of listing().artifacts) {
      const r = row(out, e.name)
      const get = [...r.querySelectorAll<HTMLElement>('a[download], button.copy')]
      expect(get.length, `${e.name} has no way to get it`).toBeGreaterThanOrEqual(2)
      for (const g of get) expect(shownAt(g, { width: 390 }), `${e.name}'s ${g.textContent} is hidden at 390`).toBe(true)
    }
  })

  it('draws every table as the stacked record the inspector uses below 900px', async () => {
    await openPane(finishedRoutes())
    await sectionReady('Outputs', /bundle\.tar/)
    // The Artifacts tab's tables, then the Logs tab's (U11a: a tab of its own).
    const tables = [...drawer().querySelectorAll('table')]
    await sectionReady('Log', /recorded only its final result/)
    tables.push(...drawer().querySelectorAll('table'))
    expect(tables.length).toBeGreaterThan(0)
    for (const t of tables) {
      expect(t.parentElement?.classList.contains('ctl-table') && t.parentElement.classList.contains('is-stacked'), 'a table would scroll sideways at 390').toBe(true)
    }
  })

  it('writes no colour literal, so both themes follow their tokens', () => {
    const sheet = paneSheet()
    expect(sheet).toMatch(/\.arts-/)
    expect(sheet.match(/#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(/g) ?? [], 'a colour that ignores the theme').toEqual([])
  })

  it('never lets a prompt, an answer or an image widen the drawer', () => {
    const sheet = paneSheet()
    const rule = (sel: string) => {
      const esc = sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
      const m = new RegExp(`(?:^|\\n)${esc}\\s*\\{([^}]*)\\}`).exec(sheet)
      expect(m, `no ${sel} rule`).not.toBeNull()
      return m![1]!
    }
    expect(rule('.arts-prompt')).toMatch(/white-space:\s*pre-wrap/)
    expect(rule('.arts-prompt')).toMatch(/overflow-wrap:\s*anywhere/)
    expect(rule('.arts-figure img')).toMatch(/max-width:\s*100%/)
    const wide = sheet.match(/(?:^|[;\s{])(?:min-)?width:\s*(\d+)px/g) ?? []
    expect(wide.filter((w) => Number(/(\d+)px/.exec(w)![1]) > 390), 'a fixed width wider than a phone').toEqual([])
  })
})

// ---------------------------------------------------------------------------
// The post-deploy QA's drawer findings (#222) and the log locations' move
// ---------------------------------------------------------------------------
//
// Epic #222 (post-deploy QA of dev, 2026-09-26), each finding red first:
//
//   (b) at 390 the Logs card note ran 74 px past the drawer: `.ctl-card-note`
//       is `flex: none`, and the <=560px override let it wrap but not shrink;
//   (c) the transcript's facts row printed `source live [not measured] ·
//       masked 0` above `nothing published yet`;
//   (d) a thinking step with `text: ""` drew an expandable that opened onto
//       nothing;
//   (e) Markdown numbered lists restarted at 1 (ArtifactViewer.tsx, below);
//   (f) the rate-limit step printed raw keys and an epoch.
//
// And the owner's decision of 2026-09-26: each log's object location and its
// `copy gsutil` sit beside the log in Artifacts › Logs, now that Details lists
// none.
//
// MUTATIONS: take `flex-shrink`/`min-width` out of the override; draw the facts
// for an absent stream; check `step.text === null` alone; drop `start` from
// the `<ol>`; print `Scalars` again; drop the transcript's object fact.

describe('the drawer findings of the post-deploy QA (#222)', () => {
  it('(b) says the attempt and the masking in the log dock’s facts, which wrap rather than overflow', async () => {
    // THE LOGS TOOLBAR'S CARD NOTE MOVED INTO THE DOCK (viewers.html A): the
    // attempt and "masking at read time" are facts of the dock's own facts
    // row now, a `.ctl-facts` strip, which wraps at any width.
    await openPane(finishedRoutes())
    // In the trace the read's facts fold behind its source, beside the summary.
    const logsSection = await sectionReady('Log', /masking/)
    const facts = logsSection.querySelector<HTMLElement>('.trc-readfacts ul.ctl-facts')
    expect(facts, 'no facts row in the log dock').not.toBeNull()
    expect(logsSection.querySelector('.ag-logs-attempt')?.textContent).toMatch(/Attempt/)
    expect(facts!.textContent).toMatch(/masking\s*applied at read time/)
    expect(cascade(STYLES, facts!, 'flex-wrap', { width: 390 }).winner?.value).toBe('wrap')
  })

  it('(c) draws no facts over a transcript that has published nothing yet', async () => {
    await openPane(
      finishedRoutes({
        [`/v1/tasks/${REF}`]: { task: task({ state: 'RUNNING', completed_at: null, result_summary: null }) },
        [`/v1/tasks/${REF}/answer`]: answer({ status: 'not_yet', source: null, object: null, format: null, content: null, complete: null, is_error: null, subtype: null, num_turns: null, attempt: attemptBlock(false) }),
        [`/v1/tasks/${REF}/transcript`]: transcript({
          stream: stream({ status: 'absent', source: 'live', uri: null, total_bytes: null, returned_bytes: 0, age_seconds: null, object_updated_at: null }),
          format: null,
          steps: null,
          complete: false,
          attempt: attemptBlock(false),
        }),
        [`/v1/tasks/${REF}/logs`]: logs([logStream('agent_stderr'), logStream('stdout'), logStream('stderr')], false),
      }),
    )
    const logsSection = await sectionReady('Log', /nothing published yet/)
    const view = logsSection
    expect(view.querySelector('.trc-readfacts'), 'facts were drawn for a stream that does not exist').toBeNull()
    expect(view.textContent, 'a masking count was drawn over bytes nobody read').not.toMatch(/masked\s*0/)
  })

  it('(c) and draws none for a runner that has no agent CLI', async () => {
    await openPane(
      finishedRoutes({
        [`/v1/tasks/${REF}`]: { task: task({ runner_profile: 'mock' }) },
        [`/v1/tasks/${REF}/transcript`]: transcript({ stream: stream({ status: 'not_applicable', source: null, uri: null, total_bytes: null, returned_bytes: 0 }), format: null, steps: null, complete: false }),
      }),
    )
    await openTranscriptOfNoCliRunner()
    const logsSection = await sectionReady('Log', /no agent CLI/)
    expect(logsSection.querySelector('.arts-transcript .ctl-facts')).toBeNull()
  })

  it('(d) draws a thinking step with an empty text as words, never an empty expandable', async () => {
    await openPane(
      finishedRoutes({
        [`/v1/tasks/${REF}/transcript`]: transcript({
          stream: stream({ source: 'final' }),
          format: 'claude-stream-json',
          steps: [step(1, { kind: 'thinking', text: '' }), step(2, { kind: 'thinking', text: 'plan the work' })],
        }),
      }),
    )
    const logsSection = await sectionReady('Log', /plan the work/)
    const [empty, full] = [...logsSection.querySelectorAll<HTMLElement>('.trc-row.is-thinking')]
    expect(empty, 'the empty thinking step is not drawn').toBeTruthy()
    expect(empty!.textContent).toMatch(/thinking/)
    // `thinking · text not recorded`, not `real zero` (QA G2-27): nothing was measured.
    expect(empty!.textContent).toMatch(/text not recorded/)
    expect(empty!.querySelector('.ctl-mark.is-zero'), 'an empty thinking text is marked a measured zero').toBeNull()
    expect(full!.textContent, 'a thinking step with text lost its words').toMatch(/plan the work/)
  })

  it('(f) words a rate-limit reading and gives its reset in local time, never raw keys and an epoch', async () => {
    const resetsAt = 1790401200
    await openPane(
      finishedRoutes({
        [`/v1/tasks/${REF}/transcript`]: transcript({
          stream: stream({ source: 'final' }),
          format: 'claude-stream-json',
          steps: [
            step(1, {
              kind: 'rate_limit',
              role: null,
              meta: {
                'rate_limit_info.status': 'allowed',
                'rate_limit_info.rateLimitType': 'five_hour',
                'rate_limit_info.resetsAt': resetsAt,
              },
            }),
          ],
        }),
      }),
    )
    const logsSection = await sectionReady('Log', /rate limit/)
    const li = [...logsSection.querySelectorAll<HTMLElement>('.trc-row')].find((x) => /rate limit/.test(x.textContent ?? ''))!
    const text = li.textContent ?? ''
    expect(text, 'a raw key reached the screen').not.toMatch(/rate_limit_info|resetsAt|rateLimitType/)
    expect(text, 'an epoch reached the screen').not.toContain(String(resetsAt))
    expect(text).toMatch(/allowed/)
    expect(text).toMatch(/5-hour window/)
    const local = new Date(resetsAt * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
    expect(text).toContain(`resets ${local} local`)
  })

  it('puts the transcript object’s location and its copy beside the transcript', async () => {
    const writeText = vi.fn(async (_: string) => {})
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    try {
      await openPane(finishedRoutes())
      const logsSection = await sectionReady('Log', /recorded only its final result/)
      const where = uri(REF, 'claude-code.stdout.log')
      const fact = [...logsSection.querySelectorAll<HTMLElement>('.trc-readfacts .ctl-fact')].find((f) => (f.textContent ?? '').includes(where))
      expect(fact, 'the transcript names no object location').toBeTruthy()
      expect(fact!.querySelector('.uri')?.getAttribute('title'), 'the location is not whole in its title').toBe(where)
      const copy = fact!.querySelector<HTMLButtonElement>('button.copy')
      expect(copy?.textContent).toBe('copy gsutil')
      fireEvent.click(copy!)
      expect(writeText).toHaveBeenCalledWith(`gsutil cat ${where}`)
    } finally {
      delete (navigator as unknown as { clipboard?: unknown }).clipboard
    }
  })

  it('keeps each stream’s location and its copy on its own row in the other views', async () => {
    await openPane(finishedRoutes())
    await sectionReady('Log', /Transcript/)
    const choose = (label: string) => {
      const b = [...section('Log').querySelectorAll<HTMLButtonElement>('[aria-label="Which log"] button')].find((x) => (x.querySelector('.ag-logs-long')?.textContent ?? x.textContent) === label)
      fireEvent.click(b!)
    }
    choose('Runner')
    for (const name of ['stdout', 'stderr']) {
      const head = await waitFor(() => row(section('Log'), `runner ${name}`).querySelector('th')!)
      expect(head.querySelector('.uri')?.textContent).toContain(`/logs/${name}.log`)
      expect([...head.querySelectorAll('button.copy')].map((b) => b.textContent)).toContain('copy gsutil')
    }
  })
})

describe('Markdown keeps a numbered list’s own numbers (#222 e)', () => {
  it('starts each list at its first item’s number when blank lines part the items', () => {
    const { container } = render(<Markdown source={'1. first\n\n2. second\n\n3. third'} />)
    const lists = [...container.querySelectorAll('ol')]
    expect(lists.map((l) => l.textContent)).toEqual(['first', 'second', 'third'])
    // Every list would read `1.` without a start: the answer said 1, 1, 1.
    expect(lists.map((l) => l.getAttribute('start'))).toEqual([null, '2', '3'])
  })

  it('keeps a list that does not begin at 1 where it began, and a tight list as one list', () => {
    const { container } = render(<Markdown source={'4. four\n5. five\n\ntext\n\n7) seven'} />)
    const lists = [...container.querySelectorAll('ol')]
    expect(lists).toHaveLength(2)
    expect(lists[0]!.getAttribute('start')).toBe('4')
    expect([...lists[0]!.querySelectorAll('li')].map((li) => li.textContent)).toEqual(['four', 'five'])
    expect(lists[1]!.getAttribute('start')).toBe('7')
  })
})

// ---------------------------------------------------------------------------
// The address names the open output, and only the output (owner decision
// 2026-10-01): `/agents/<tab>/<id>/artifacts/<name>`, never the file of a
// patch that is open. MUTATION: drop the `artifacts/<name>` branch from
// `fromAddress` or `canonical`, or keep `open` in Files' own state -- the
// address stays `/artifacts` and the first case goes red; write the diff
// viewer's open file anywhere in the address and the `n`/click checks do.
// ---------------------------------------------------------------------------

const PATCH = [
  'diff --git a/src/one.ts b/src/one.ts',
  '--- a/src/one.ts',
  '+++ b/src/one.ts',
  '@@ -1 +1 @@',
  '-a',
  '+b',
  'diff --git a/docs/two.md b/docs/two.md',
  '--- a/docs/two.md',
  '+++ b/docs/two.md',
  '@@ -1 +1,2 @@',
  ' a',
  '+c',
  '',
].join('\n')

function patchRoutes(): Record<string, Route> {
  const diff = { name: 'change.diff', bytes: PATCH.length, uri: uri(REF, 'change.diff'), attempt_id: 'att_fe27', kind: 'text', content_type: 'text/plain', role: null }
  return finishedRoutes({
    [`/v1/tasks/${REF}/artifacts`]: listing({ artifacts: [...listing().artifacts, diff] }),
    [`/v1/tasks/${REF}/artifacts/content`]: (q: URLSearchParams) => ({
      task_id: REF,
      tenant_id: 'eng',
      attempt_id: 'att_fe27',
      artifact: { name: q.get('name'), bytes: PATCH.length, uri: uri(REF, 'change.diff') },
      status: 'ok',
      detail: null,
      key: null,
      uri: uri(REF, 'change.diff'),
      content: PATCH,
      total_bytes: PATCH.length,
      offset: 0,
      returned_bytes: PATCH.length,
      next_offset: null,
      truncated: false,
      redacted: false,
      redaction_count: 0,
      redaction: { applied_at_read_time: true, rules: 12 },
    }),
  })
}

describe('the address names the open output and never the open file of a patch', () => {
  afterEach(() => {
    window.history.replaceState(null, '', '/')
  })

  it('opening change.diff gives exactly /agents/<tab>/<id>/artifacts/change.diff; n and a click leave it alone', async () => {
    // The task is SUCCEEDED, so it sits under Recent (QA12-26): the list, the
    // back link and the address follow the task's group, whatever list opened it.
    await openPane(patchRoutes())
    const out = await sectionReady('Outputs', /change\.diff/)
    await waitFor(() => expect(window.location.pathname).toBe(`/agents/recent/${REF}/artifacts`))
    fireEvent.click(row(out, 'change.diff').querySelector('button.art-open')!)
    const scroller = await waitFor(() => {
      const s = drawer().querySelector<HTMLElement>('section.diff .diff-scroll')
      expect(s, 'change.diff did not open in the diff viewer').not.toBeNull()
      return s!
    })
    const named = `/agents/recent/${REF}/artifacts/change.diff`
    expect(window.location.pathname).toBe(named)
    expect(scroller.getAttribute('data-open-file')).toBe('src/one.ts')
    fireEvent.keyDown(scroller, { key: 'n' })
    expect(scroller.getAttribute('data-open-file')).toBe('docs/two.md')
    fireEvent.click(drawer().querySelector<HTMLElement>('button.diff-file[data-path="src/one.ts"]')!)
    expect(scroller.getAttribute('data-open-file')).toBe('src/one.ts')
    expect(window.location.pathname).toBe(named)
    expect(window.location.search).toBe('')
    expect(window.location.hash).toBe('')
    // Back to the list takes the name out of the address again.
    fireEvent.click(drawer().querySelector<HTMLElement>('.diff-bar .diff-back')!)
    await waitFor(() => expect(window.location.pathname).toBe(`/agents/recent/${REF}/artifacts`))
    expect(drawer().querySelector('section.diff')).toBeNull()
  })

  it('opens the output the address names, at the first file of the patch', async () => {
    await openPane(patchRoutes(), `#work/task/${REF}/artifacts/change.diff`)
    const scroller = await waitFor(() => {
      const s = drawer().querySelector<HTMLElement>('section.diff .diff-scroll')
      expect(s, 'the named output was not opened').not.toBeNull()
      return s!
    })
    expect(scroller.getAttribute('data-open-file')).toBe('src/one.ts')
    expect(window.location.pathname).toBe(`/agents/recent/${REF}/artifacts/change.diff`)
  })

  it('round-trips a name, slashes and all, as one segment', async () => {
    const { fromHash, canonical } = await import('../App')
    window.location.hash = '#work/task/tsk_x/artifacts/out%2Fchange.diff'
    const r = fromHash()
    expect(r.taskId).toBe('tsk_x')
    expect(r.taskPane).toBe('artifacts')
    expect(r.artifact).toBe('out/change.diff')
    expect(canonical(r)).toBe('work/task/tsk_x/artifacts/out%2Fchange.diff')
    // The bare pane names no output, and writes none.
    window.location.hash = '#work/task/tsk_x/artifacts'
    expect(fromHash().artifact ?? null).toBeNull()
    expect(canonical(fromHash())).toBe('work/task/tsk_x/artifacts')
  })
})
