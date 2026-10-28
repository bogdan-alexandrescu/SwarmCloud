// #1857: THE OWNER-ONLY RAW PATCH, AS THE CONSOLE OFFERS IT.
//
// A served `.patch` is redacted at read time, so credential-shaped test
// fixtures in it come back masked and it no longer applies. BGD decided on
// 2026-10-27: the masking stays, and the task's SUBMITTER may download the
// patch as stored (`GET /v1/tasks/{id}/artifacts/raw?redaction=none`). The
// console offers "Download raw patch" beside the normal download, with a
// one-line note that it is not redacted -- to the submitter only.
//
// Through <App /> and a stubbed fetch answering by PATH, as artifacts.pane
// does: `/v1/tenants/me` says who is signed in, and the task says who
// submitted it. MUTATIONS, each a case below: drop the submitter check (the
// member sees the control); compare case-insensitively (a near-miss email
// sees it); offer it for any name (report.md grows one); drop `redaction=none`
// from the link (the href is the redacted one); drop the note.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/react'

import STYLES from '../styles.css?raw'
import { artifactRawUrl } from '../api'
import { offersRawPatch, RawPatchDownload } from '../ArtifactViewer'
import { isSubmitter } from '../Artifacts'
import type { Task } from '../types'
import { cascade } from './cssgate'
import { task as baseTask } from './runfixture'

vi.mock('../Agents', () => ({ AgentsScreen: () => null }))

const REF = 'task_5be0c1a7d2e94f3a8c61'
const SUBMITTER = 'alice@example.com'
const MEMBER = 'dave@example.com'
const iso = (msAgo: number) => new Date(Date.now() - msAgo).toISOString()
const uri = (name: string) => `gs://swarm-artifacts/tenants/eng/tasks/${REF}/attempts/att_9a01/artifacts/${name}`

const PATCH = [
  'diff --git a/tests/test_login.py b/tests/test_login.py',
  '--- a/tests/test_login.py',
  '+++ b/tests/test_login.py',
  '@@ -1,2 +1,3 @@',
  ' import pytest',
  '+USER = dict(name="fixture")',
  ' # end',
  '',
].join('\n')

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

function task(over: Partial<Task> = {}) {
  return baseTask({
    id: REF,
    tenant_id: 'eng',
    state: 'SUCCEEDED',
    runner_profile: 'claude-code',
    started_at: iso(120_000),
    completed_at: iso(30_000),
    submitted_by: SUBMITTER,
    input: { prompt: 'Fix the login test.' },
    result_summary: { artifacts: [], logs: {} },
    ...over,
  })
}

function me(email: string) {
  return {
    tenant: { tenant_id: 'eng', kind: 'group', principal: 'eng@example.com', display_name: null },
    principal: { email, domain: 'example.com', groups: ['eng@example.com'], is_admin: false, is_admin_unresolved: false },
    environment: 'dev',
    environment_declared: true,
  }
}

const entry = (name: string, bytes: number, kind: string) => ({
  name,
  bytes,
  uri: uri(name),
  attempt_id: 'att_9a01',
  kind,
  content_type: 'text/plain',
  role: null,
})

const LISTING = {
  task_id: REF,
  complete: true,
  attempt_id: 'att_9a01',
  artifact_bytes: 400,
  artifacts_skipped: [],
  artifacts: [entry('fix-1.patch', PATCH.length, 'text'), entry('report.md', 120, 'markdown')],
}

function content(name: string) {
  const body = name === 'fix-1.patch' ? PATCH : '# Report\n'
  return {
    task_id: REF,
    tenant_id: 'eng',
    attempt_id: 'att_9a01',
    artifact: { name, bytes: body.length, uri: uri(name) },
    status: 'ok',
    detail: null,
    key: null,
    uri: uri(name),
    content: body,
    total_bytes: body.length,
    offset: 0,
    returned_bytes: body.length,
    next_offset: null,
    truncated: false,
    redacted: false,
    redaction_count: 0,
    redaction: { applied_at_read_time: true, rules: 12 },
    kind: name === 'fix-1.patch' ? 'text' : 'markdown',
    content_type: 'text/plain',
    invalid_utf8_bytes: 0,
  }
}

const ATTEMPT = { status: 'latest', known: true, generation: 1, created_at: iso(120_000), completed_at: iso(30_000), exit_code: 0 }
const READ = { task_id: REF, tenant_id: 'eng', attempt_id: 'att_9a01', attempt: ATTEMPT, read_at: new Date().toISOString() }
const REDACTION = { applied_at_read_time: true, rules: 12 }

/** The pane's other reads, as artifacts.pane answers them: nothing in them is under test here. */
const ANSWER = {
  ...READ,
  status: 'ok',
  source: 'agent_result_event',
  object: { stream: 'agent_stdout', source: 'artifact', uri: uri('claude-code.stdout.log'), object_updated_at: iso(30_000) },
  format: 'markdown',
  content: '# Fixed\n',
  complete: true,
  is_error: false,
  subtype: 'success',
  stop_reason: 'end_turn',
  terminal_reason: 'completed',
  num_turns: 3,
  bytes: 8,
  redacted: false,
  redaction_count: 0,
  detail: null,
}
const TRANSCRIPT = {
  ...READ,
  stream: {
    stream: 'agent_stdout', source: 'artifact', status: 'ok', detail: null, uri: uri('claude-code.stdout.log'),
    object_updated_at: iso(30_000), age_seconds: 30, total_bytes: 0, offset: 0, returned_bytes: 0,
    next_offset: null, truncated: false, tail_window: null,
  },
  format: 'claude-json',
  steps: [],
  complete: true,
  window_starts_mid_stream: false,
  skipped_lines: 0,
  answer_in_window: true,
  redaction: REDACTION,
  redaction_count: 0,
}
const LOGS = { ...READ, streams: [], prefix: `tenants/eng/tasks/${REF}/attempts/att_9a01/`, redaction: REDACTION }
const INPUT = {
  task_id: REF,
  tenant_id: 'eng',
  read_at: new Date().toISOString(),
  prompt_key: 'string',
  prompt: { text: 'Fix the login test.', redaction_count: 0 },
  rest: null,
  full: { text: '{"prompt": "Fix the login test."}', redaction_count: 0 },
  redacted: false,
  redaction_count: 0,
  redaction: REDACTION,
}

type Route = unknown | ((q: URLSearchParams) => unknown)

async function openPane(signedIn: string, over: Partial<Task> = {}): Promise<void> {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const routes: Record<string, Route> = {
    '/v1/tenants/me': me(signedIn),
    [`/v1/tasks/${REF}`]: { task: task(over) },
    [`/v1/tasks/${REF}/artifacts`]: LISTING,
    [`/v1/tasks/${REF}/answer`]: ANSWER,
    [`/v1/tasks/${REF}/transcript`]: TRANSCRIPT,
    [`/v1/tasks/${REF}/logs`]: LOGS,
    [`/v1/tasks/${REF}/input`]: INPUT,
    [`/v1/tasks/${REF}/artifacts/content`]: (q: URLSearchParams) => content(q.get('name') ?? ''),
  }
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const u = new URL(String(input), 'http://ui.test')
    const answer = routes[u.pathname]
    if (answer === undefined) return new Promise<Response>(() => {})
    const body = typeof answer === 'function' ? (answer as (q: URLSearchParams) => unknown)(u.searchParams) : answer
    return json(body)
  }) as unknown as typeof fetch
  window.location.hash = `#work/task/${REF}/artifacts`
  const { App } = await import('../App')
  render(<App />)
}

function drawer(): HTMLElement {
  const el = document.querySelector<HTMLElement>('.ctl-drawer')
  expect(el, 'no agent drawer is open').not.toBeNull()
  return el!
}

function outputs(): HTMLElement {
  const s = [...drawer().querySelectorAll<HTMLElement>('section')].find((x) => x.querySelector('h2')?.textContent === 'Outputs')
  expect(s, 'no Outputs section').toBeTruthy()
  return s!
}

function row(name: string): HTMLTableRowElement {
  const r = [...outputs().querySelectorAll<HTMLTableRowElement>('tbody tr')].find((tr) =>
    (tr.querySelector('th')?.textContent ?? '').trim().startsWith(name),
  )
  expect(r, `no row for ${name}`).toBeTruthy()
  return r!
}

const rawLinks = (within: HTMLElement) =>
  [...within.querySelectorAll<HTMLAnchorElement>('a')].filter((a) => a.textContent === 'Download raw patch')

/** Waits for the file list, then for the signed-in read to have landed. */
async function listed(): Promise<void> {
  await waitFor(() => expect(row('fix-1.patch')).toBeTruthy())
  await waitFor(() => expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.some((c) => String(c[0]).includes('/v1/tenants/me'))).toBe(true))
  // One more turn, so a `me` answer that WOULD draw the control has drawn it.
  await new Promise((r) => setTimeout(r, 30))
}

afterEach(() => {
  window.location.hash = ''
})

describe('the raw patch link', () => {
  it('asks for redaction=none, as an attachment, and only when asked', () => {
    const raw = new URL(artifactRawUrl(REF, 'fix-1.patch', 'attachment', { redaction: 'none' }), 'http://ui.test')
    expect(raw.pathname).toBe(`/v1/tasks/${REF}/artifacts/raw`)
    expect(raw.searchParams.get('redaction')).toBe('none')
    expect(raw.searchParams.get('disposition')).toBe('attachment')
    expect(raw.searchParams.get('name')).toBe('fix-1.patch')
    const plain = new URL(artifactRawUrl(REF, 'fix-1.patch', 'attachment'), 'http://ui.test')
    expect(plain.searchParams.has('redaction'), 'the default link asked for the unredacted patch').toBe(false)
  })

  it('is offered for a top-level .patch name only, as the server honours it', () => {
    expect(offersRawPatch('fix-1.patch')).toBe(true)
    expect(offersRawPatch('report.md')).toBe(false)
    expect(offersRawPatch('deep/fix-2.patch')).toBe(false)
    expect(offersRawPatch('.patch')).toBe(false)
    expect(offersRawPatch('fix.patch.txt')).toBe(false)
  })

  it('compares the submitter exactly, and unknown is no', () => {
    expect(isSubmitter(SUBMITTER, SUBMITTER)).toBe(true)
    expect(isSubmitter(MEMBER, SUBMITTER)).toBe(false)
    expect(isSubmitter('Alice@example.com', SUBMITTER)).toBe(false)
    expect(isSubmitter(null, SUBMITTER)).toBe(false)
    expect(isSubmitter(SUBMITTER, null)).toBe(false)
    expect(isSubmitter('', '')).toBe(false)
  })
})

describe('the Artifacts tab offers the raw patch to its submitter only', () => {
  it('the submitter sees "Download raw patch" beside the download, with its note, on the .patch row only', async () => {
    await openPane(SUBMITTER)
    await listed()
    await waitFor(() => expect(rawLinks(row('fix-1.patch'))).toHaveLength(1))
    const link = rawLinks(row('fix-1.patch'))[0]!
    const href = new URL(link.getAttribute('href') ?? '', 'http://ui.test')
    expect(href.searchParams.get('redaction')).toBe('none')
    expect(href.searchParams.get('disposition')).toBe('attachment')
    expect(link.hasAttribute('download')).toBe(true)
    expect(row('fix-1.patch').textContent).toMatch(/not redacted/)
    // The normal download is still there, and still the redacted one.
    const normal = [...row('fix-1.patch').querySelectorAll<HTMLAnchorElement>('a')].find((a) => a.textContent === 'download')
    expect(normal, 'the normal download moved').toBeTruthy()
    expect(new URL(normal!.getAttribute('href') ?? '', 'http://ui.test').searchParams.has('redaction')).toBe(false)
    expect(rawLinks(row('report.md'))).toHaveLength(0)
  })

  it('the submitter sees it in the open patch viewer too', async () => {
    await openPane(SUBMITTER)
    await listed()
    fireEvent.click(row('fix-1.patch').querySelector('button.art-open')!)
    const viewer = await waitFor(() => {
      const v = outputs().querySelector<HTMLElement>('.art-viewer')
      expect(v).not.toBeNull()
      expect(v!.textContent).toMatch(/test_login\.py/)
      return v!
    })
    await waitFor(() => expect(rawLinks(viewer)).toHaveLength(1))
    expect(viewer.textContent).toMatch(/not redacted/)
  })

  it('another member sees neither the control nor its note, in the row or the viewer', async () => {
    await openPane(MEMBER)
    await listed()
    expect(rawLinks(outputs())).toHaveLength(0)
    expect(row('fix-1.patch').textContent).not.toMatch(/not redacted/)
    fireEvent.click(row('fix-1.patch').querySelector('button.art-open')!)
    const viewer = await waitFor(() => {
      const v = outputs().querySelector<HTMLElement>('.art-viewer')
      expect(v).not.toBeNull()
      expect(v!.textContent).toMatch(/test_login\.py/)
      return v!
    })
    await new Promise((r) => setTimeout(r, 30))
    expect(rawLinks(viewer)).toHaveLength(0)
    expect(viewer.textContent).not.toMatch(/not redacted/)
  })

  it('a task with no recorded submitter offers it to nobody', async () => {
    await openPane(SUBMITTER, { submitted_by: null })
    await listed()
    expect(rawLinks(outputs())).toHaveLength(0)
  })
})

// THE CONTROL MUST NOT WIDEN THE GET COLUMN (review of #1857, rendered at
// desktop 1440): a table cell is `white-space: nowrap`, so the note drawn as
// one unbounded line set the Get cell's minimum at ~505px and crushed every
// row's File column from 306px to 40px. MUTATIONS: drop the `.art-rawpatch`
// rule (the span stays inline-flex, unbounded) or the note's `white-space`
// (it inherits nowrap).
describe('the raw patch control wraps inside its cell', () => {
  it('sits on its own bounded line, and its note wraps', () => {
    const { container } = render(
      <table className="ctl-table">
        <tbody>
          <tr>
            <td>
              <RawPatchDownload taskId={REF} name="fix-1.patch" />
            </td>
          </tr>
        </tbody>
      </table>,
    )
    const span = container.querySelector('.art-rawpatch')!
    const note = span.querySelector('.ctl-sub')!
    const env = { width: 1440, theme: 'light' as const }
    expect(cascade(STYLES, span, 'display', env).winner?.value).toBe('flex')
    expect(cascade(STYLES, span, 'max-width', env).winner?.value).toMatch(/^\d+ch$/)
    expect(cascade(STYLES, note, 'white-space', env).winner?.value).toBe('normal')
  })
})
