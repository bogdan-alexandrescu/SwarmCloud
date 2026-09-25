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
// route cut short as the whole manifest.

import STYLES from '../styles.css?raw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, waitFor } from '@testing-library/react'

import type { Task } from '../types'
import { cascade, type CascadeEnv } from './cssgate'
import { task as baseTask } from './runfixture'

vi.mock('../Agents', () => ({ AgentsScreen: () => null }))

const REF = 'task_73b5f4d9ca3641fbb914'
const WAIT = { timeout: 8000 }
const iso = (msAgo: number) => new Date(Date.now() - msAgo).toISOString()

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

const uri = (id: string, name: string) => `gs://swarm-artifacts/tenants/eng/tasks/${id}/attempts/att_fe27/artifacts/${name}`

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
    repository_url: 'https://github.com/bogdan-alexandrescu/SwarmCloud',
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
    uri: `gs://swarm-artifacts/tenants/eng/tasks/${REF}/attempts/att_fe27/logs/${name}.log`,
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
    ...over,
  }
}

function drawer(): HTMLElement {
  const el = document.querySelector<HTMLElement>('.ctl-drawer')
  expect(el, 'no agent drawer is open').not.toBeNull()
  return el!
}

function section(title: string): HTMLElement {
  const s = [...drawer().querySelectorAll<HTMLElement>('section')].find((x) => x.querySelector('h2')?.textContent === title)
  expect(s, `no ${title} section in the drawer`).toBeTruthy()
  return s!
}

async function sectionReady(title: string, text: string | RegExp): Promise<HTMLElement> {
  return waitFor(() => {
    const s = section(title)
    expect(s.textContent ?? '').toMatch(text)
    return s
  }, WAIT)
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

describe('the drawer has three panes: Details, Attempts, Artifacts', () => {
  it('names them so, and #work/task/<id>/artifacts opens the third', async () => {
    await openPane(finishedRoutes())
    const tabs = await waitFor(() => {
      const list = drawer().querySelector('[role="tablist"][aria-label="Agent panes"]')
      expect(list).not.toBeNull()
      return [...list!.querySelectorAll<HTMLButtonElement>('[role="tab"]')]
    }, WAIT)
    expect(tabs.map((t) => t.textContent?.trim())).toEqual(['Details', 'Attempts', 'Artifacts'])
    expect(tabs[2]!.getAttribute('aria-selected')).toBe('true')
    for (const title of ['Inputs', 'Outputs', 'Logs']) await sectionReady(title, /./)
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
    await waitFor(() => expect(out.textContent).toMatch(/image could not be loaded/), WAIT)
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
    await waitFor(() => expect(out.querySelector('.art-viewer .art-md h4')?.textContent).toBe('Report'), WAIT)

    open('claude-transcript.json')
    await waitFor(() => expect(out.querySelector('.art-viewer pre.art-text')?.textContent).toBe('{\n  "type": "result",\n  "num_turns": 7\n}'), WAIT)

    open('big.json')
    await waitFor(() => expect(out.querySelector('.art-viewer')?.textContent).toMatch(/partial, not pretty-printed/), WAIT)
    expect(out.querySelector('.art-viewer pre.art-text')?.textContent, 'a partial JSON window was reformatted').toBe('{"steps":[{"a":1},{"a"')

    const before = api.count(`/v1/tasks/${REF}/artifacts/content`)
    fireEvent.click(row(out, 'bundle.tar').querySelector('button.art-open')!)
    await waitFor(() => expect(out.textContent).toMatch(/binary · 4 KiB/), WAIT)
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
    expect(out.textContent, 'a list that is not written yet is drawn as empty').not.toMatch(/none uploaded/)
    expect(out.querySelector('.ctl-mark.is-zero'), 'a pending list wears the real-zero mark').toBeNull()
    expect(api.count(`/v1/tasks/${REF}/artifacts`), 'the listing was asked for with no summary to list').toBe(0)
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
    await waitFor(() => expect(fileRows(out).size, 'a file of the manifest has no row').toBe(manifest.length), WAIT)
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
    expect(out.querySelector('.arts-files .count-chip')?.textContent).toBe(String(manifest.length))
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
    expect(out.querySelector('.arts-files .count-chip')?.textContent, 'the count is the page, not the run').toBe(String(manifest.length))

    await waitFor(() => expect(fileRows(out).size).toBe(manifest.length), WAIT)
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

  it('shows the prompt verbatim and wrapped, and the repository, ref and commit cloned', async () => {
    await openPane(finishedRoutes())
    const inputs = await sectionReady('Inputs', /Audit the capacity code/)
    const pre = inputs.querySelector('pre')
    expect(pre?.textContent, 'the prompt is not verbatim').toBe('Audit the capacity code.\n\nSay what reserve() guarantees.')
    expect(inputs.textContent).toMatch(/github\.com\/bogdan-alexandrescu\/SwarmCloud/)
    expect(inputs.textContent).toMatch(/main/)
    expect(inputs.textContent).toMatch(/0a09be9f3c1d/)
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
    await waitFor(() => expect(inputs.querySelector('.art-viewer .art-md h4')?.textContent).toBe('Scan'), WAIT)
    expect(api.count(`/v1/tasks/${UP}/artifacts/content`), 'the staged file was not read from its upstream run').toBe(1)
  })

  it('says a staged file removed upstream was removed, and keeps the size that was staged', async () => {
    await openPane(stagedRoutes('absent'))
    const inputs = await sectionReady('Inputs', /scan\.md/)
    const r = row(inputs, 'scan.md')
    expect(r.textContent).toMatch(/2 KiB/)
    expect(r.textContent, 'a staged size is drawn as zero').not.toMatch(/0 B/)
    fireEvent.click(r.querySelector('button.art-open')!)
    await waitFor(() => expect(inputs.textContent).toMatch(/removed from the upstream run/), WAIT)
  })
})

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
    const logsSection = await sectionReady('Logs', /Read/)
    expect(logsSection.textContent).toMatch(/earlier steps are not in this window/)
    const read = [...logsSection.querySelectorAll('li')].find((li) => li.querySelector('summary')?.textContent?.includes('Read'))
    expect(read, 'the Read tool call is not a step').toBeTruthy()
    const summaries = [...read!.querySelectorAll('summary')].map((s) => s.textContent ?? '')
    expect(summaries.some((s) => /result · error/.test(s)), 'the failed result is not paired with its call, or not called an error').toBe(true)
    expect([...read!.querySelectorAll('details')].every((d) => !d.hasAttribute('open')), 'a tool step is not collapsed').toBe(true)
    expect(logsSection.textContent, 'the paired result was drawn a second time').not.toMatch(/its call is not in this window/)
    const task = [...logsSection.querySelectorAll('li')].find((li) => li.querySelector(':scope > details > summary')?.textContent?.includes('Task'))
    expect(task?.querySelector('ol')?.textContent, 'the sub-agent is not nested under its tool call').toMatch(/The sub-agent reports back/)
    expect(logsSection.textContent).toMatch(/skipped/)
  })

  it('labels a run that recorded only its final result, rather than drawing a short transcript', async () => {
    await openPane(finishedRoutes())
    const logsSection = await sectionReady('Logs', /recorded only its final result/)
    expect(logsSection.textContent).toMatch(/turn-by-turn steps need stream-json/)
  })

  it('draws the agent’s empty stderr as a measured zero, and the runner’s own log behind its own choice', async () => {
    await openPane(finishedRoutes())
    const logsSection = await sectionReady('Logs', /transcript/)
    const choose = (label: string) => {
      const b = [...logsSection.querySelectorAll<HTMLButtonElement>('[aria-label="Which log"] button')].find((x) => x.textContent === label)
      expect(b, `no ${label} choice`).toBeTruthy()
      fireEvent.click(b!)
    }
    choose('stderr')
    await waitFor(() => expect(row(logsSection, 'agent_stderr').querySelector('td[data-label="Size"] .ctl-mark.is-zero')).not.toBeNull(), WAIT)
    choose('runner (platform)')
    await waitFor(() => expect(logsSection.querySelector('pre.logwin-body')?.textContent).toMatch(/child started/), WAIT)
    row(logsSection, 'stdout')
    row(logsSection, 'stderr')
  })

  it('says a runner with no agent CLI has no agent stream, which is not a missing one', async () => {
    await openPane(
      finishedRoutes({
        [`/v1/tasks/${REF}`]: { task: task({ runner_profile: 'mock' }) },
        [`/v1/tasks/${REF}/transcript`]: transcript({ stream: stream({ status: 'not_applicable', source: null, total_bytes: null, returned_bytes: 0 }), format: null, steps: null, complete: false }),
        [`/v1/tasks/${REF}/logs`]: logs([logStream('agent_stderr', { status: 'not_applicable', source: null, content: null, total_bytes: null }), logStream('stdout'), logStream('stderr')]),
      }),
    )
    const logsSection = await sectionReady('Logs', /no agent CLI/)
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
    const logsSection = await sectionReady('Logs', /cut at its cap/)
    const capture = [...logsSection.querySelectorAll('li.ctl-fact')].find((li) => li.querySelector('b')?.textContent === 'capture')
    expect(capture?.querySelector('.ctl-mark.is-partial'), 'a cut capture is not marked partial').not.toBeNull()
    expect(logsSection.textContent, 'the server’s reason for an ok window is drawn nowhere').toContain(CUT_DETAIL)
    const notice = [...logsSection.querySelectorAll('li.arts-step')].find((li) => /capture_truncated/.test(li.textContent ?? ''))
    expect(notice, 'the capture’s notice is not a step').toBeTruthy()
    expect(notice!.textContent, 'the notice’s own words are not drawn').toMatch(/110592 bytes dropped/)
    expect(notice!.querySelector('.ctl-mark.is-partial'), 'where the run is not whole is not marked').not.toBeNull()
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
    const logsSection = await sectionReady('Logs', /line longer than this window/)
    expect(logsSection.textContent).toContain(OVERSIZE_DETAIL)
    const s = logsSection.querySelector('li.arts-step')
    expect(s?.querySelector('.ctl-mark.is-partial'), 'an undecoded line is not marked partial').not.toBeNull()
    expect(s?.textContent, 'an over-long line is drawn as an event with nothing in it').not.toMatch(/^\s*other/)
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
    const logsSection = await sectionReady('Logs', /Read/)
    const call = [...logsSection.querySelectorAll('li.arts-step')].find((li) => li.querySelector('summary')?.textContent?.includes('Read'))
    expect(call?.textContent, 'two unknown ids were joined as a call and its result').toMatch(/no result in this window/)
    expect(logsSection.textContent, 'the unjoined result was dropped').toMatch(/its call is not in this window/)
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
    await waitFor(() => expect(fact(), 'bytes shown as U+FFFD are not counted').toBeTruthy(), WAIT)
    expect(fact()!.textContent).toMatch(/3/)
    expect(fact()!.querySelector('.ctl-mark.is-partial')).not.toBeNull()
    fireEvent.click(row(out, 'claude-transcript.json').querySelector('button.art-open')!)
    await waitFor(() => expect(out.querySelector('.art-viewer pre.art-text')?.textContent).toBe('{}'), WAIT)
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
    const transcripts = `/v1/tasks/${REF}/transcript`
    const logReads = `/v1/tasks/${REF}/logs`
    expect(api.count(transcripts)).toBe(1)
    expect(section('Logs').textContent, 'a live read does not say how old it is').toMatch(/published 42s ago/)

    await advance(5_000)
    expect(api.count(transcripts), 'the transcript was not re-read at 5 s').toBe(2)
    expect(api.count(logReads), 'the logs were not re-read at 5 s').toBe(2)
    expect(api.calls.some((c) => c.includes('stream=agent_stdout')), 'the raw agent stdout was polled with its view closed').toBe(false)

    finished = true
    await advance(5_000)
    expect(api.count(transcripts)).toBe(3)
    await advance(30_000)
    expect(api.count(transcripts), 'the pane kept polling a settled finish').toBe(3)
    expect(api.count(logReads)).toBe(3)
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
    const logsSection = await sectionReady('Logs', /published \d+s ago/)
    const ages = () => [...logsSection.querySelectorAll<HTMLElement>('.ctl-sub')].filter((s) => /published \d+s ago/.test(s.textContent ?? ''))
    expect(ages().length, 'the transcript shows no age').toBeGreaterThan(0)
    for (const a of ages()) expect(shownAt(a, { width: 390 }), `${a.textContent} is hidden at 390`).toBe(true)

    const stderr = [...logsSection.querySelectorAll<HTMLButtonElement>('[aria-label="Which log"] button')].find((b) => b.textContent === 'stderr')
    fireEvent.click(stderr!)
    const cell = await waitFor(() => {
      const c = row(logsSection, 'agent_stderr').querySelector<HTMLElement>('td[data-label="Age"] .ctl-sub')
      expect(c?.textContent).toMatch(/published \d+s ago/)
      return c!
    }, WAIT)
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
    await sectionReady('Logs', /recorded only its final result/)
    const tables = [...drawer().querySelectorAll('table')]
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
