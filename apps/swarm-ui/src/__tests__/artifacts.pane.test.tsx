// SPLIT (part of #2688, 2026-11-07): this file and `artifacts.pane.logs.test.tsx` were one file,
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

import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, waitFor } from '@testing-library/react'

import type { Task } from '../types'
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

function rawHref(taskId: string, name: string, disposition: 'inline' | 'attachment'): string {
  return `/v1/tasks/${taskId}/artifacts/raw?${new URLSearchParams({ name, disposition }).toString()}`
}

afterEach(() => {
  vi.useRealTimers()
  window.location.hash = ''
})

// ---------------------------------------------------------------------------
// The tabs and the address
// ---------------------------------------------------------------------------

describe('the drawer has six panes: Details, Logs, Changes, Attempts, Artifacts, Checkpoints', () => {
  it('names them so, and #work/task/<id>/artifacts opens the fifth of six', async () => {
    await openPane(finishedRoutes())
    const tabs = await waitFor(() => {
      const list = drawer().querySelector('[role="tablist"][aria-label="Agent panes"]')
      expect(list).not.toBeNull()
      return [...list!.querySelectorAll<HTMLButtonElement>('[role="tab"]')]
    })
    expect(tabs.map((t) => t.querySelector('.c-tab-label')?.textContent?.trim())).toEqual(['Details', 'Logs', 'Changes', 'Attempts', 'Artifacts', 'Checkpoints'])
    expect(tabs[4]!.getAttribute('aria-selected')).toBe('true')
    for (const title of ['Inputs', 'Outputs', 'Log']) await sectionReady(title, /./)
  })

  it('round-trips the pane through the address, and leaves the other two as they were', async () => {
    const { fromHash, canonical } = await import('../App')
    for (const [hash, pane] of [
      ['#work/task/tsk_x/artifacts', 'artifacts'],
      ['#work/task/tsk_x/attempts', 'attempts'],
      ['#work/task/tsk_x', 'detail'],
    ] as const) {
      window.location.hash = hash
      const r = fromHash()
      expect(r.taskId, `${hash} named the wrong task`).toBe('tsk_x')
      expect(r.taskPane).toBe(pane)
      expect(canonical(r)).toBe(hash.slice(1))
    }
  })
})

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

describe('Outputs: the answer first, then every file', () => {
  it('renders the agent’s answer as Markdown, above the file list', async () => {
    await openPane(finishedRoutes())
    const out = await sectionReady('Outputs', /reserve is all-or-nothing/)
    const md = out.querySelector('.art-md')
    expect(md, 'the answer is not rendered as Markdown').not.toBeNull()
    expect(md!.querySelector('h4')?.textContent).toBe('Findings')
    expect([...md!.querySelectorAll('li')].map((li) => li.textContent)).toContain('reserve is all-or-nothing')
    expect(out.textContent, 'the Markdown source leaked through as text').not.toMatch(/# Findings/)
    const table = out.querySelector('table')
    expect(table, 'the file list is missing').not.toBeNull()
    expect(
      md!.compareDocumentPosition(table!) & Node.DOCUMENT_POSITION_FOLLOWING,
      'the answer is not before the file list',
    ).toBeTruthy()
  })

  it('downloads every file through the API and copies a gsutil line, and never links a bucket', async () => {
    await openPane(finishedRoutes())
    const out = await sectionReady('Outputs', /bundle\.tar/)
    for (const e of listing().artifacts) {
      const r = row(out, e.name)
      const a = r.querySelector<HTMLAnchorElement>('a[download]')
      expect(a, `${e.name} has no download`).not.toBeNull()
      expect(a!.getAttribute('href')).toBe(rawHref(REF, e.name, 'attachment'))
      expect(
        [...r.querySelectorAll('button')].some((b) => b.textContent === 'copy gsutil'),
        `${e.name} has no copy gsutil`,
      ).toBe(true)
    }
    for (const a of drawer().querySelectorAll<HTMLAnchorElement>('a[href]')) {
      const href = a.getAttribute('href') ?? ''
      expect(href.startsWith('/v1/') || href.startsWith('#'), `a link leaves the API: ${href}`).toBe(true)
    }
  })

  it('draws an image inline from the raw route, and says so when it cannot be decoded', async () => {
    await openPane(finishedRoutes())
    const out = await sectionReady('Outputs', /home\.png/)
    const img = out.querySelector<HTMLImageElement>('img')
    expect(img, 'no image is drawn inline').not.toBeNull()
    expect(img!.getAttribute('src')).toBe(rawHref(REF, 'screens/home.png', 'inline'))
    expect(
      [...out.querySelectorAll<HTMLAnchorElement>('a')].some(
        (a) => a.textContent === 'open full' && a.getAttribute('href') === rawHref(REF, 'screens/home.png', 'inline'),
      ),
      'no open full for the image',
    ).toBe(true)
    fireEvent.error(img!)
    await waitFor(() => expect(out.textContent).toMatch(/image could not be loaded/))
    expect(out.querySelector('.ctl-mark.is-unread'), 'a broken image is not marked as a failed read').not.toBeNull()
    expect(out.querySelector('img'), 'the broken image is still drawn as a blank box').toBeNull()
  })

  it('opens each file in the viewer its kind names: Markdown rendered, JSON pretty only when whole', async () => {
    const api = await openPane(
      finishedRoutes({
        [`/v1/tasks/${REF}/artifacts/content`]: (q: URLSearchParams) => {
          const name = q.get('name') ?? ''
          const whole = name !== 'big.json'
          const content =
            name === 'report.md' ? '# Report\n\nThe body.\n' : name === 'big.json' ? '{"steps":[{"a":1},{"a"' : '{"type":"result","num_turns":7}'
          return {
            task_id: REF,
            tenant_id: 'eng',
            attempt_id: 'att_fe27',
            artifact: { name, bytes: content.length, uri: uri(REF, name) },
            status: 'ok',
            detail: null,
            key: null,
            uri: uri(REF, name),
            content,
            total_bytes: whole ? content.length : 900_000,
            offset: 0,
            returned_bytes: content.length,
            next_offset: whole ? null : 4096,
            truncated: !whole,
            redacted: false,
            redaction_count: 0,
            redaction: { applied_at_read_time: true, rules: 12 },
          }
        },
      }),
    )
    const out = await sectionReady('Outputs', /report\.md/)
    const open = (name: string) => {
      const b = row(out, name).querySelector('button.art-open')
      expect(b, `${name} cannot be opened`).not.toBeNull()
      fireEvent.click(b!)
    }

    open('report.md')
    await waitFor(() => expect(out.querySelector('.art-viewer .art-md h4')?.textContent).toBe('Report'))

    open('claude-transcript.json')
    // A WHOLE JSON FILE OPENS AS A TREE, with Raw beside it (viewers.html A).
    await waitFor(() => expect(out.querySelector('.art-viewer [role="tree"]')?.textContent).toContain('"num_turns": 7'))
    fireEvent.click([...out.querySelectorAll<HTMLButtonElement>('.art-viewer [aria-label="JSON view"] button')].find((b) => b.textContent === 'Raw')!)
    await waitFor(() => expect(out.querySelector('.art-viewer pre.art-text')?.textContent).toBe('{\n  "type": "result",\n  "num_turns": 7\n}'))

    open('big.json')
    await waitFor(() => expect(out.querySelector('.art-viewer')?.textContent).toMatch(/partial, not pretty-printed/))
    expect(out.querySelector('.art-viewer pre.art-text')?.textContent, 'a partial JSON window was reformatted').toBe('{"steps":[{"a":1},{"a"')

    const before = api.count(`/v1/tasks/${REF}/artifacts/content`)
    fireEvent.click(row(out, 'bundle.tar').querySelector('button.art-open')!)
    await waitFor(() => expect(out.textContent).toMatch(/binary · 4 KiB/))
    expect(api.count(`/v1/tasks/${REF}/artifacts/content`), 'a binary file was read as text').toBe(before)
  })

  it('says a running task’s files are uploaded when the attempt ends, and its answer is not yet -- never none', async () => {
    const api = await openPane(
      finishedRoutes({
        [`/v1/tasks/${REF}`]: { task: task({ state: 'RUNNING', completed_at: null, result_summary: null }) },
        [`/v1/tasks/${REF}/answer`]: answer({ status: 'not_yet', source: null, object: null, format: null, content: null, complete: null, is_error: null, subtype: null, num_turns: null, attempt: attemptBlock(false) }),
        [`/v1/tasks/${REF}/transcript`]: transcript({ stream: stream({ source: 'live', age_seconds: 2 }), format: 'claude-stream-json', steps: [step(1, { text: 'Reading.' })], complete: false, attempt: attemptBlock(false) }),
        [`/v1/tasks/${REF}/logs`]: logs([logStream('agent_stderr', { source: 'live' }), logStream('stdout', { source: 'live' }), logStream('stderr', { source: 'live' })], false),
      }),
    )
    const out = await sectionReady('Outputs', /uploaded when the attempt ends/)
    expect(out.querySelectorAll('.ctl-mark.is-pending').length, 'a not-yet is not drawn as pending').toBeGreaterThanOrEqual(2)
    expect(out.textContent).toMatch(/not yet/)
    expect(out.textContent, 'a list that is not written yet is drawn as empty').not.toMatch(/(none|nothing) uploaded/)
    expect(out.querySelector('.ctl-mark.is-zero'), 'a pending list wears the real-zero mark').toBeNull()
    expect(api.count(`/v1/tasks/${REF}/artifacts`), 'the listing was asked for with no summary to list').toBe(0)
  })

  // task-019, QAFIX3-AGENT (visual QA run 3, wf_d598fa7bffdb41fc9d76,
  // 2026-10-21; part of #1680): a value written only when the attempt ends --
  // the answer, the file list, the commit cloned -- wore the `reading` chip,
  // the word for a read in flight, though no read was in flight. It keeps the
  // temporary (dotted) silhouette and says its own not-yet instead.
  it('words a value due at finish as its not-yet, never `reading`', async () => {
    await openPane(
      finishedRoutes({
        [`/v1/tasks/${REF}`]: { task: task({ state: 'RUNNING', completed_at: null, result_summary: null }) },
        [`/v1/tasks/${REF}/answer`]: answer({ status: 'not_yet', source: null, object: null, format: null, content: null, complete: null, is_error: null, subtype: null, num_turns: null, attempt: attemptBlock(false) }),
        [`/v1/tasks/${REF}/transcript`]: transcript({ stream: stream({ source: 'live', age_seconds: 2 }), format: 'claude-stream-json', steps: [step(1, { text: 'Reading.' })], complete: false, attempt: attemptBlock(false) }),
        [`/v1/tasks/${REF}/logs`]: logs([logStream('agent_stderr', { source: 'live' }), logStream('stdout', { source: 'live' }), logStream('stderr', { source: 'live' })], false),
      }),
    )
    const out = await sectionReady('Outputs', /uploaded when the attempt ends/)
    const inputs = await sectionReady('Inputs', /reported at\s*finish/)
    const marks = [...out.querySelectorAll<HTMLElement>('.ctl-mark.is-pending'), ...inputs.querySelectorAll<HTMLElement>('li .ctl-mark.is-pending')]
    // MUTATION: drop the not-yet marks altogether -- the silhouette that says "temporary" goes with the word.
    expect(marks.length).toBeGreaterThanOrEqual(3)
    for (const m of marks) {
      // MUTATION: draw `<Mark kind="pending">` before the words again -- `reading uploaded when the attempt ends` (task-019).
      expect(m.textContent, m.getAttribute('aria-label') ?? '').not.toMatch(/reading/)
    }
    // MUTATION: lose the words with the chip -- a dotted mark that says nothing.
    expect(marks.map((m) => m.textContent)).toEqual(expect.arrayContaining(['not yet', 'uploaded when the attempt ends', 'reported at finish']))
  })

  it('says a route this deployment does not serve is not served, never that there is no answer', async () => {
    // A fresh Response per read: a body can be read once.
    await openPane(finishedRoutes({ [`/v1/tasks/${REF}/answer`]: () => json({ detail: 'Not Found' }, 404) }))
    const out = await sectionReady('Outputs', /not served by this API/)
    expect(out.textContent).not.toMatch(/no answer recorded/)
  })
})

// ---------------------------------------------------------------------------
// Outputs past one page of the listing
// ---------------------------------------------------------------------------

/**
 * A browser run with `total` files -- `report.md` and screenshots after it,
 * named as `runners/browser.py` names them -- and the listing route answering
 * AS THE SERVER DOES: `paged_limit` turns no `limit` into `default_page_size`
 * (50) and clamps any other to `max_page_size` (200), and
 * `store.list_artifacts` returns `manifest.artifacts[:limit]` with `complete:
 * true` and nothing saying it cut. `served` records how many entries each
 * listing read returned, so the cases derive their figures from what the
 * route did rather than restating either number.
 */
function manyFiles(total: number) {
  const shots = Array.from({ length: total - 1 }, (_, i) => `screenshot-${String(i).padStart(3, '0')}.png`)
  const manifest = [
    { name: 'report.md', bytes: 120, uri: uri(REF, 'report.md') },
    ...shots.map((name) => ({ name, bytes: 2048, uri: uri(REF, name) })),
  ]
  const served: number[] = []
  const routes = finishedRoutes({
    [`/v1/tasks/${REF}`]: {
      task: task({ runner_profile: 'browser', result_summary: { artifacts: manifest, logs: {}, agent_streams: null } }),
    },
    [`/v1/tasks/${REF}/artifacts`]: (q: URLSearchParams) => {
      const asked = q.get('limit')
      const page = manifest.slice(0, Math.min(asked === null ? 50 : Number(asked), 200))
      served.push(page.length)
      return listing({
        artifact_bytes: null,
        artifacts: page.map((e) => {
          const image = e.name.endsWith('.png')
          return { ...e, attempt_id: 'att_fe27', kind: image ? 'image' : 'markdown', content_type: image ? 'image/png' : 'text/markdown', role: null }
        }),
      })
    },
  })
  return { manifest, served, routes }
}

/** Every file row in the Outputs table, by the file name its row header starts with. */
function fileRows(out: HTMLElement): Map<string, HTMLTableRowElement> {
  const rows = new Map<string, HTMLTableRowElement>()
  for (const tr of out.querySelectorAll<HTMLTableRowElement>('.arts-files tbody tr')) {
    const name = tr.querySelector('th button, th .mono')?.textContent?.trim() ?? ''
    rows.set(name, tr)
  }
  return rows
}

describe('Outputs lists every file the run uploaded, past one page of the listing', () => {
  // THE DEFECT. `loadArtifactListing` sent no `limit`, so the server answered
  // its default page of 50, cut from the manifest and still `complete: true`.
  // A browser run that took 60 screenshots drew 50 rows and a chip reading 50,
  // with no partial mark; `screenshot-050`..`059` -- the final page states --
  // were never drawn and could not be downloaded from the pane, while the
  // Details pane's list, which reads the manifest off the task, showed all 60.

  it('asks for a page big enough for a 60-screenshot run, and draws every screenshot inline', async () => {
    const { manifest, served, routes } = manyFiles(61)
    await openPane(routes)
    const out = await sectionReady('Outputs', /screenshot-000\.png/)
    await waitFor(() => expect(fileRows(out).size, 'a file of the manifest has no row').toBe(manifest.length))
    const rows = fileRows(out)
    for (const e of manifest) {
      expect(rows.get(e.name)?.querySelector('a[download]')?.getAttribute('href'), `${e.name} cannot be downloaded`).toBe(
        rawHref(REF, e.name, 'attachment'),
      )
    }
    const drawn = new Set([...out.querySelectorAll('.arts-gallery img')].map((i) => i.getAttribute('src')))
    for (const e of manifest.filter((x) => x.name.endsWith('.png'))) {
      expect(drawn.has(rawHref(REF, e.name, 'inline')), `${e.name} is not drawn inline`).toBe(true)
    }
    expect(served.every((n) => n === manifest.length), 'the listing read was cut at the server’s default page').toBe(true)
    expect(out.querySelector('.arts-files .c-chip.is-n')?.textContent).toBe(String(manifest.length))
    expect(out.textContent, 'a whole listing is marked as cut').not.toMatch(/\d+ of \d+ listed/)
  })

  it('lists the files past the route’s page from the task’s own manifest, and says how many the route listed', async () => {
    const { manifest, served, routes } = manyFiles(260)
    await openPane(routes)
    const out = await sectionReady('Outputs', new RegExp(`of ${manifest.length} listed`))
    const listed = served[served.length - 1]!
    expect(listed, 'the route listed the whole manifest; this case needs one it cuts').toBeLessThan(manifest.length)
    const head = out.querySelector<HTMLElement>('.arts-files .att-sub-head')!
    expect(head.textContent).toMatch(new RegExp(`${listed} of ${manifest.length} listed`))
    expect(head.querySelector('.ctl-mark.is-partial'), 'a cut listing is not marked partial').not.toBeNull()
    expect(out.querySelector('.arts-files .c-chip.is-n')?.textContent, 'the count is the page, not the run').toBe(String(manifest.length))

    await waitFor(() => expect(fileRows(out).size).toBe(manifest.length))
    const rows = fileRows(out)
    for (const e of manifest) {
      expect(rows.get(e.name)?.querySelector('a[download]')?.getAttribute('href'), `${e.name} cannot be downloaded`).toBe(
        rawHref(REF, e.name, 'attachment'),
      )
    }
    // Past the page the server named no kind, so the raw route decides what
    // the bytes are: each such row opens it, and an image is one click away.
    for (const e of manifest.slice(listed)) {
      const open = [...rows.get(e.name)!.querySelectorAll<HTMLAnchorElement>('a')].find((a) => a.textContent === 'open full')
      expect(open?.getAttribute('href'), `${e.name}, past the page, has no way to be opened`).toBe(rawHref(REF, e.name, 'inline'))
    }
  })
})

describe('Outputs says which working-folder files were not uploaded, and why', () => {
  // #225 REVIEW. A task with no repository uploads what its agent created in
  // its working folder, within 50 files and 25 MiB (#184, owner decision of
  // 2026-09-26). The worker records every file it did not upload, with the
  // reason, in `result_summary.workdir_outputs`, and the pane drew none of
  // it: a file over the cap, one holding a key the worker could not take
  // out, and one whose name is not UTF-8 were all simply absent. The worker
  // lists 50 and counts the rest, and its log names every one.
  const NOT_UPLOADED = [
    { name: 'workdir/f50.txt', bytes: 7, reason: 'over cap' },
    { name: 'workdir/dump.bin', bytes: 4096, reason: 'holds a registered secret that could not be redacted' },
    { name: 'workdir/caf\\xe9.txt', bytes: 5, reason: 'name is not valid UTF-8' },
  ]
  const WORKDIR = {
    prefix: 'workdir/',
    uploaded: 50,
    uploaded_bytes: 4096,
    not_uploaded: NOT_UPLOADED,
    not_uploaded_count: 55,
    symlinks_skipped: 0,
    working_folder_is_symlink: false,
    cap_files: 50,
    cap_bytes: 26_214_400,
  }

  function standalone(workdir: unknown): Record<string, Route> {
    return finishedRoutes({
      [`/v1/tasks/${REF}`]: {
        task: task({
          repository_url: null,
          repository_ref: null,
          result_summary: {
            artifacts: [],
            logs: {},
            agent_streams: { stdout: 'claude-code.stdout.log', stderr: 'claude-code.stderr.log', transcript: 'claude-transcript.json', transcript_skipped: null },
            workdir_outputs: workdir,
          },
        }),
      },
    })
  }

  it('lists each one with its size and its reason, and says how many more only the worker log names', async () => {
    await openPane(standalone(WORKDIR))
    const out = await sectionReady('Outputs', /not uploaded from the working folder/)
    const block = out.querySelector<HTMLElement>('.arts-unuploaded')
    expect(block, 'the files the worker did not upload are not drawn').not.toBeNull()
    expect(block!.querySelector('.c-chip.is-n')?.textContent, 'the count is the page, not the whole').toBe('55')
    for (const e of NOT_UPLOADED) {
      const r = row(block!, e.name)
      expect(r.querySelector('td[data-label="Why"]')?.textContent, `${e.name} has no reason`).toBe(e.reason)
      expect(r.querySelector('td[data-label="Size"]')?.textContent).toMatch(/\d+ (B|KiB)/)
    }
    expect(block!.textContent).toMatch(/52 more, named in the worker log/)
    const table = block!.querySelector('table')
    expect(
      table?.parentElement?.classList.contains('ctl-table') && table.parentElement.classList.contains('is-stacked'),
      'a table would scroll sideways at 390',
    ).toBe(true)
    // Not rows of the file list: nothing here can be downloaded.
    expect(block!.querySelector('a[download]')).toBeNull()
    expect(out.querySelector('.arts-files')?.contains(block!)).toBe(false)
  })

  it('draws nothing for a run that uploaded every file it created, or a task with a repository', async () => {
    await openPane(standalone({ ...WORKDIR, not_uploaded: [], not_uploaded_count: 0 }))
    const out = await sectionReady('Outputs', /bundle\.tar/)
    expect(out.querySelector('.arts-unuploaded')).toBeNull()
    expect(out.textContent).not.toMatch(/not uploaded from the working folder/)
  })
})

describe('Outputs says how many files in $SWARM_ARTIFACTS_DIR were past the 500-file cap', () => {
  // #228, OWNER DECISION OF 2026-09-26. The worker uploads at most 500 files
  // from the artifacts folder per attempt, because each is an entry in the
  // task's Firestore document and a document holds 1 MiB. The rest are not
  // uploaded: `result_summary.artifacts_over_cap` counts them and the worker's
  // log names every one, 100 to a line. Without this the file list would read
  // as the whole of what the agent wrote.
  function capped(extra: Record<string, unknown>): Record<string, Route> {
    return finishedRoutes({
      [`/v1/tasks/${REF}`]: {
        task: task({
          result_summary: {
            artifacts: [],
            logs: {},
            agent_streams: { stdout: 'claude-code.stdout.log', stderr: 'claude-code.stderr.log', transcript: 'claude-transcript.json', transcript_skipped: null },
            ...extra,
          },
        }),
      },
    })
  }

  it('counts them, names the cap from the summary, and says the worker log names every one', async () => {
    await openPane(capped({ artifacts_over_cap: 104, artifacts_cap_files: 500 }))
    const out = await sectionReady('Outputs', /not uploaded from \$SWARM_ARTIFACTS_DIR/)
    const block = out.querySelector<HTMLElement>('.arts-overcap')
    expect(block, 'the files past the cap are not drawn').not.toBeNull()
    expect(block!.querySelector('.c-chip.is-n')?.textContent).toBe('104')
    expect(block!.textContent).toMatch(/104 over the 500-file cap, named in the worker log/)
    // The same count line #225 drew for the working folder: a mark that says
    // the list is partial, and where the rest are named.
    expect(block!.querySelector('.att-none .ctl-mark')).not.toBeNull()
    // Not rows of the file list: nothing here was uploaded, so nothing can be read.
    expect(block!.querySelector('a[download]')).toBeNull()
    expect(out.querySelector('.arts-files')?.contains(block!)).toBe(false)
  })

  it('reads the cap from the summary rather than restating it', async () => {
    await openPane(capped({ artifacts_over_cap: 3, artifacts_cap_files: 10 }))
    const out = await sectionReady('Outputs', /not uploaded from \$SWARM_ARTIFACTS_DIR/)
    expect(out.querySelector('.arts-overcap')?.textContent).toMatch(/3 over the 10-file cap/)
  })

  it('draws nothing when every file in the folder fit', async () => {
    await openPane(capped({}))
    const out = await sectionReady('Outputs', /bundle\.tar/)
    expect(out.querySelector('.arts-overcap')).toBeNull()
    expect(out.textContent).not.toMatch(/-file cap/)
  })
})

describe('Outputs names the cause the worker recorded for each skipped file', () => {
  // #165, OWNER DECISION OF 2026-09-28. The worker records WHY each file was
  // not uploaded; the listing route serves each skipped file as
  // `{name, cause}` (owner decision, 2026-10-02). A name with no recorded
  // cause (a summary from before causes) is shown bare, never assumed to be
  // the cap.
  it('says the cap for a cap skip, the upload for an upload error, and nothing for an older name', async () => {
    await openPane(
      finishedRoutes({
        [`/v1/tasks/${REF}/artifacts`]: listing({
          artifacts_skipped: [
            { name: 'core.dump', cause: 'cap' },
            { name: 'notes.md', cause: 'upload_error' },
            { name: 'old.bin', cause: null },
          ],
        }),
      }),
    )
    const out = await sectionReady('Outputs', /3 skipped/)
    const say = [...out.querySelectorAll('.ctl-mark')].map((m) => m.getAttribute('aria-label') ?? '').find((t) => t.includes('core.dump'))
    expect(say, 'the skipped note names no file').toBeDefined()
    expect(say).toContain('core.dump (over the size cap)')
    expect(say).toContain('notes.md (the upload failed)')
    expect(say).toMatch(/old\.bin(,|$)/)
    expect(say).not.toContain('old.bin (')
  })
})

// ---------------------------------------------------------------------------
// Inputs
// ---------------------------------------------------------------------------

describe('Inputs: the prompt, the repository, and every staged file', () => {
  const UP = 'task_upstream0123456789'

  function stagedRoutes(contentStatus: 'ok' | 'absent'): Record<string, Route> {
    return finishedRoutes({
      [`/v1/tasks/${REF}`]: {
        task: task({
          workflow_id: 'wf_1',
          step_id: 'report',
          metadata: { input_from: { [UP]: 'scan.md' } },
          result_summary: {
            artifacts: [],
            logs: {},
            git: { base: '0a09be9f3c1d2e4b5a6978' },
            staged_inputs: [{ task_id: UP, filename: 'scan.md', path: 'scan.md', bytes: 2048 }],
          },
        }),
      },
      '/v1/workflows/wf_1': {
        workflow: { workflow_id: 'wf_1', steps: [{ step_id: 'scan', task_id: UP, runner_profile: 'claude-code', resource_class: 'standard', depends_on: [], input_from: {} }] },
        tasks: [baseTask({ id: UP, state: 'SUCCEEDED', result_summary: { artifacts: [{ name: 'scan.md', bytes: 2048, uri: uri(UP, 'scan.md') }] } })],
      },
      [`/v1/tasks/${UP}/artifacts/content`]: {
        task_id: UP,
        tenant_id: 'eng',
        attempt_id: 'att_up',
        artifact: { name: 'scan.md', bytes: 2048, uri: uri(UP, 'scan.md') },
        status: contentStatus,
        detail: contentStatus === 'absent' ? 'the object is not in the bucket' : null,
        key: null,
        uri: uri(UP, 'scan.md'),
        content: contentStatus === 'ok' ? '# Scan\n' : null,
        total_bytes: contentStatus === 'ok' ? 7 : null,
        offset: 0,
        returned_bytes: contentStatus === 'ok' ? 7 : 0,
        next_offset: null,
        truncated: false,
        redacted: false,
        redaction_count: 0,
        redaction: { applied_at_read_time: true, rules: 12 },
      },
    })
  }

  it('shows the prompt as the API served it, wrapped, and the repository, ref and commit cloned', async () => {
    const api = await openPane(finishedRoutes())
    const inputs = await sectionReady('Inputs', /Audit the capacity code/)
    const pre = inputs.querySelector('pre')
    expect(pre?.textContent, 'the prompt is not the served copy').toBe('Audit the capacity code.\n\nSay what reserve() guarantees.')
    expect(api.count(`/v1/tasks/${REF}/input`), 'the prompt was not read through the masked copy').toBeGreaterThan(0)
    // A clean prompt draws no count: `masked 0` read as jargon beside it
    // (QA1-FIX-07, overview-agents-50); a count above zero is drawn, below.
    // The qualifier that said the prompt was not masked is gone because it
    // no longer is true.
    expect(inputs.querySelector('.art-masked'), 'a clean prompt says `masked 0`').toBeNull()
    expect(inputs.textContent).not.toMatch(/not masked/)
    expect(inputs.textContent).toMatch(/github\.com\/example-org\/SwarmCloud/)
    expect(inputs.textContent).toMatch(/main/)
    expect(inputs.textContent).toMatch(/0a09be9f3c1d/)
  })

  it('masks a credential in the prompt as the API masked it, counts it, and never draws the raw input', async () => {
    // The task document holds the raw prompt; the pane must not draw it. The
    // served copy is what `redaction.redact` made of it, count included.
    const secret = 'sk-proj0123456789abcdefghijklmnopqrstuv'
    const masked = 'Deploy with sk-proj01******** then report.'
    await openPane(
      finishedRoutes({
        [`/v1/tasks/${REF}`]: { task: task({ input: { prompt: `Deploy with ${secret} then report.`, token_hint: secret } }) },
        [`/v1/tasks/${REF}/input`]: inputCopy({
          prompt: { text: masked, redaction_count: 1 },
          rest: { text: '{\n  "token_hint": "sk-proj01********"\n}', redaction_count: 1 },
          full: { text: `{\n  "prompt": "${masked}",\n  "token_hint": "sk-proj01********"\n}`, redaction_count: 2 },
          redacted: true,
          redaction_count: 2,
        }),
      }),
    )
    const inputs = await sectionReady('Inputs', /Deploy with/)
    expect(drawer().textContent, 'the raw input reached the screen').not.toContain(secret)
    expect(inputs.querySelector('pre')?.textContent).toBe(masked)
    const count = inputs.querySelector('.art-masked')
    // Everything the block draws: the prompt and the rest of the input.
    expect(count?.textContent).toBe('2')
    expect(count!.classList.contains('is-warn'), 'a count above zero is drawn as nothing to see').toBe(true)
  })

  it('says an API that does not serve the copy does not, and draws no raw prompt in its place', async () => {
    await openPane(
      finishedRoutes({
        // FastAPI's own 404 for a path it does not route: no error envelope.
        [`/v1/tasks/${REF}/input`]: () =>
          new Response(JSON.stringify({ detail: 'Not Found' }), {
            status: 404,
            headers: { 'content-type': 'application/json' },
          }),
      }),
    )
    const inputs = await sectionReady('Inputs', /not served by this API/)
    expect(inputs.textContent, 'the raw prompt was drawn when the copy was not served').not.toMatch(/Audit the capacity code/)
  })

  it('asks again for a copy that failed, on the pane’s next poll, and draws it when it comes', async () => {
    // PR #210 REVIEW. The read's key was the constant `input`, so one failed
    // copy said `the masked input not read` for as long as the pane was open,
    // while every other read on it came back on the 5 s poll.
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
    let copyFails = true
    const running = () => task({ state: 'RUNNING', completed_at: null, result_summary: null })
    const api = await openPane({
      [`/v1/tasks/${REF}`]: () => ({ task: running() }),
      [`/v1/tasks/${REF}/artifacts`]: listing(),
      [`/v1/tasks/${REF}/answer`]: () =>
        answer({ status: 'not_yet', source: null, object: null, format: null, content: null, complete: null, is_error: null, subtype: null, num_turns: null, attempt: attemptBlock(false) }),
      [`/v1/tasks/${REF}/transcript`]: () =>
        transcript({ stream: stream({ source: 'live', age_seconds: 4 }), format: 'claude-stream-json', steps: [], complete: false, attempt: attemptBlock(false) }),
      [`/v1/tasks/${REF}/logs`]: () =>
        logs([logStream('agent_stderr', { source: 'live' }), logStream('stdout', { source: 'live' }), logStream('stderr', { source: 'live' })], false),
      [`/v1/tasks/${REF}/input`]: () =>
        copyFails
          ? new Response(JSON.stringify({ code: 'upstream_unavailable', message: 'Busy.' }), {
              status: 503,
              headers: { 'content-type': 'application/json' },
            })
          : inputCopy(),
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
    expect(section('Inputs').textContent, 'the failed copy is not said to be not read').toMatch(/masked input not read/)
    const firstAsks = api.count(`/v1/tasks/${REF}/input`)
    expect(firstAsks).toBeGreaterThan(0)

    copyFails = false
    await advance(5_000)
    expect(api.count(`/v1/tasks/${REF}/input`), 'the failed copy was not asked for again on the poll').toBeGreaterThan(firstAsks)
    expect(section('Inputs').querySelector('pre')?.textContent, 'the copy that came back is not drawn').toBe(
      'Audit the capacity code.\n\nSay what reserve() guarantees.',
    )
    // Once read, it is answered from memory: the next poll asks nothing.
    const afterRead = api.count(`/v1/tasks/${REF}/input`)
    await advance(5_000)
    expect(api.count(`/v1/tasks/${REF}/input`), 'a copy already read was asked for again').toBe(afterRead)
  })

  it('links each staged file to the step that produced it, and reads it from THAT run', async () => {
    const api = await openPane(stagedRoutes('ok'))
    const inputs = await sectionReady('Inputs', /scan\.md/)
    const r = row(inputs, 'scan.md')
    const link = r.querySelector<HTMLAnchorElement>('a.ctl-link')
    expect(link?.textContent, 'the upstream run is not named by its step').toBe('scan')
    expect(link?.getAttribute('href')).toBe(`#work/task/${UP}/artifacts`)
    expect(r.querySelector<HTMLAnchorElement>('a[download]')?.getAttribute('href')).toBe(rawHref(UP, 'scan.md', 'attachment'))
    fireEvent.click(r.querySelector('button.art-open')!)
    await waitFor(() => expect(inputs.querySelector('.art-viewer .art-md h4')?.textContent).toBe('Scan'))
    expect(api.count(`/v1/tasks/${UP}/artifacts/content`), 'the staged file was not read from its upstream run').toBe(1)
  })

  it('says a staged file removed upstream was removed, and keeps the size that was staged', async () => {
    await openPane(stagedRoutes('absent'))
    const inputs = await sectionReady('Inputs', /scan\.md/)
    const r = row(inputs, 'scan.md')
    expect(r.textContent).toMatch(/2 KiB/)
    expect(r.textContent, 'a staged size is drawn as zero').not.toMatch(/0 B/)
    fireEvent.click(r.querySelector('button.art-open')!)
    await waitFor(() => expect(inputs.textContent).toMatch(/removed from the upstream run/))
  })
})
