/**
 * LANE HW-UI (docs/design/human-steps.md §4.5, §6; part of #1436): the card
 * an `await-input` step draws, and the browser's limits on an answer's files.
 *
 * WHAT EACH CASE HOLDS, each written before the card existed and failing
 * without it:
 *   * the card renders each form -- `verdict` (Approve, Request changes and
 *     the findings), `answers` (the upstream agent's questions as the server
 *     read them) and `text`;
 *   * Approve posts `MERGE` to the step's own route, with the findings typed
 *     and one `answer_id` the card made;
 *   * the picker refuses an eleventh file and a total over 16 MiB BEFORE
 *     anything is sent: the problem is said, Send is disabled, and no POST
 *     leaves the browser;
 *   * the warning is shown above the picker and again beside Send;
 *   * `max_files: 0` shows no picker and no warning;
 *   * an expired or answered step shows who, when and how, and offers no form;
 *   * the pill reads "waiting for a member", never "waiting for you";
 *   * an image attachment is offered as a download, never rendered.
 *
 * The bodies are `swarm_api/awaitinput.py`'s `step_view` shape. Placeholders
 * only; nothing is token-shaped.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import { AwaitInputCard } from '../AwaitInputCard'
import { FILE_WARNING, MAX_ANSWER_BYTES, filesProblem, waitingTitle } from '../awaitInput'

const WAIT = { timeout: 4000 }
const T0 = Date.parse('2026-10-18T10:00:00.000Z')
const at = (ms: number) => new Date(T0 + ms).toISOString()
const ROUTE = '/v1/workflows/wf_r/steps/review/input'

type Json = Record<string, unknown>
type Sent = { method: string; url: string; json: unknown; form: FormData | null }

function view(over: Json = {}): Json {
  return {
    workflow_id: 'wf_r',
    step_id: 'review',
    task_id: 'task_review',
    status: 'waiting',
    spec_verified: true,
    prompt: 'Read draft.md. Approve it, or list what must change.',
    form: 'verdict',
    artifact: 'review.json',
    max_files: 10,
    max_answer_bytes: MAX_ANSWER_BYTES,
    tenant: 'eng',
    may_answer: true,
    remind: ['owner@example.com'],
    opened_at: at(-3_600_000),
    expires_at: at(3 * 3_600_000 + 20 * 60_000),
    on_expire: 'fail',
    shows: [{ step_id: 'draft', task_id: 'task_draft', name: 'draft.md' }],
    questions: null,
    answer: null,
    ...over,
  }
}

/** A fetch double that records JSON and multipart bodies alike. */
function serve(getBody: () => Json, post: (s: Sent) => { status: number; body: unknown } = () => ({ status: 200, body: { state: 'SUCCEEDED' } })): Sent[] {
  const sent: Sent[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    const raw = init?.body
    const form = raw instanceof FormData ? raw : null
    const json = typeof raw === 'string' ? JSON.parse(raw) : null
    const s = { method, url, json, form }
    sent.push(s)
    const r = method === 'GET' && url.split('?')[0] === ROUTE ? { status: 200, body: getBody() } : method === 'POST' ? post(s) : { status: 404, body: { detail: 'Not Found' } }
    return new Response(JSON.stringify(r.body), { status: r.status, headers: { 'content-type': 'application/json' } })
  }) as unknown as typeof fetch
  return sent
}

const posts = (sent: Sent[]) => sent.filter((s) => s.method === 'POST')

/** A file whose size is claimed, not allocated: 16 MiB of zeroes is not what is under test. */
function file(name: string, size = 10): File {
  const f = new File(['x'], name, { type: 'text/plain' })
  Object.defineProperty(f, 'size', { value: size })
  return f
}

async function mount(body: Json) {
  const sent = serve(() => body)
  const onAnswered = vi.fn()
  const utils = render(<AwaitInputCard workflowId="wf_r" stepId="review" onAnswered={onAnswered} />)
  const card = await screen.findByTestId('ai-card')
  await waitFor(() => expect(card.textContent).not.toContain('Reading the step'), WAIT)
  return { ...utils, sent, card, onAnswered }
}

function pick(files: File[]) {
  const input = document.querySelector<HTMLInputElement>('input[type="file"]')!
  fireEvent.change(input, { target: { files } })
}

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(T0)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('the await-input card renders each form', () => {
  it('verdict: Approve, Request changes and the findings box', async () => {
    const { card } = await mount(view())
    expect(within(card).getByRole('button', { name: 'Approve' })).toBeTruthy()
    expect(within(card).getByRole('button', { name: 'Request changes' })).toBeTruthy()
    expect(within(card).getByLabelText(/Findings, one per line/)).toBeTruthy()
    expect(card.textContent).toContain('Read draft.md. Approve it')
    // What the step shows, read through the artifact viewer on demand.
    expect(within(card).getByRole('list', { name: 'What the step shows' }).textContent).toContain('draft.md')
  })

  it('answers: the upstream agent’s questions, each option a choice', async () => {
    const questions = [
      { question: 'Which database?', options: [{ label: 'Postgres', description: 'the default' }, { label: 'SQLite', description: null }], recommended: 'Postgres', context: null },
      { question: 'Ship today?', options: [{ label: 'Yes', description: null }, { label: 'No', description: null }], recommended: null, context: 'The freeze starts at 18:00.' },
    ]
    const { card, sent } = await mount(view({ form: 'answers', questions, max_files: 0 }))
    expect(card.textContent).toContain('Which database?')
    expect(card.textContent).toContain('Ship today?')
    expect(card.textContent).toContain('recommended')
    const send = within(card).getByRole('button', { name: 'Send' }) as HTMLButtonElement
    // Every question must be answered before Send does anything.
    expect(send.disabled).toBe(true)
    fireEvent.click(within(card).getByLabelText(/Postgres/))
    fireEvent.click(within(card).getAllByLabelText(/Other…/)[1]!)
    fireEvent.change(within(card).getByLabelText('Your answer to question 2'), { target: { value: 'Tomorrow morning' } })
    // The button is drawn again once its reason clears: read it afresh.
    const ready = within(card).getByRole('button', { name: 'Send' }) as HTMLButtonElement
    expect(ready.disabled).toBe(false)
    fireEvent.click(ready)
    await waitFor(() => expect(posts(sent).length).toBe(1), WAIT)
    expect((posts(sent)[0]!.json as Json).payload).toEqual({
      answers: [
        { question_index: 0, choice: 'Postgres', text: null },
        { question_index: 1, choice: null, text: 'Tomorrow morning' },
      ],
    })
  })

  it('text: a text box and Send', async () => {
    const { card, sent } = await mount(view({ form: 'text', max_files: 0 }))
    fireEvent.change(within(card).getByLabelText('The answer'), { target: { value: 'Use the second outline.' } })
    fireEvent.click(within(card).getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(posts(sent).length).toBe(1), WAIT)
    expect((posts(sent)[0]!.json as Json).payload).toEqual({ text: 'Use the second outline.' })
  })
})

describe('answering', () => {
  it('Approve posts MERGE, with the findings and one answer id, to the step’s route', async () => {
    const { card, sent, onAnswered } = await mount(view())
    fireEvent.change(within(card).getByLabelText(/Findings, one per line/), { target: { value: 'Section 3 omits the cost.\n\n  Typo in the title. ' } })
    fireEvent.click(within(card).getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(posts(sent).length).toBe(1), WAIT)
    const p = posts(sent)[0]!
    expect(p.url).toBe(ROUTE)
    const body = p.json as Json
    expect(body.payload).toEqual({ verdict: 'MERGE', findings: ['Section 3 omits the cost.', 'Typo in the title.'] })
    expect(body.via).toBe('console')
    expect(String(body.answer_id)).toMatch(/^[a-z0-9-]{1,64}$/)
    await waitFor(() => expect(onAnswered).toHaveBeenCalled(), WAIT)
  })

  it('Request changes posts NOT_YET', async () => {
    const { card, sent } = await mount(view())
    fireEvent.click(within(card).getByRole('button', { name: 'Request changes' }))
    await waitFor(() => expect(posts(sent).length).toBe(1), WAIT)
    expect(((posts(sent)[0]!.json as Json).payload as Json).verdict).toBe('NOT_YET')
  })

  it('a second answer is the server’s 409, said in place, and the step is read again', async () => {
    let body = view()
    const sent = serve(() => body, () => {
      body = view({ status: 'answered', may_answer: false, answer: { answer_id: 'other', answered_by: 'lee@example.com', answered_at: at(-5_000), via: 'mcp', sha256: null, bytes: 40, files: [] } })
      return { status: 409, body: { code: 'conflict', message: "step 'review' was already answered by lee@example.com", detail: { reason: 'already_answered' } } }
    })
    render(<AwaitInputCard workflowId="wf_r" stepId="review" />)
    const card = await screen.findByTestId('ai-card')
    fireEvent.click(await within(card).findByRole('button', { name: 'Approve' }, WAIT))
    await waitFor(() => expect(within(card).getByTestId('ai-closed').textContent).toContain('lee@example.com'), WAIT)
    expect(card.textContent).toContain('already_answered')
    expect(card.textContent).toContain('through the plugin')
    expect(sent.filter((s) => s.method === 'GET').length).toBe(2)
  })

  it('sends files as multipart: one answer part and a file part each', async () => {
    const { card, sent } = await mount(view())
    pick([file('notes.md', 120)])
    fireEvent.click(within(card).getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(posts(sent).length).toBe(1), WAIT)
    const form = posts(sent)[0]!.form!
    expect(form).not.toBeNull()
    const part = form.get('answer') as Blob
    expect(part.type).toBe('application/json')
    // jsdom's Blob is read through jsdom's FileReader.
    const text = await new Promise<string>((resolve) => {
      const r = new FileReader()
      r.onload = () => resolve(String(r.result))
      r.readAsText(part)
    })
    const answer = JSON.parse(text) as Json
    expect((answer.payload as Json).verdict).toBe('MERGE')
    expect((form.getAll('file') as File[]).map((f) => f.name)).toEqual(['notes.md'])
  })
})

describe('the file picker refuses before sending', () => {
  it('refuses an eleventh file', async () => {
    const { card, sent } = await mount(view())
    pick(Array.from({ length: 11 }, (_, i) => file(`f${i}.md`)))
    const problem = await within(card).findByTestId('ai-files-problem')
    expect(problem.textContent).toContain('at most 10 files')
    const approve = within(card).getByRole('button', { name: 'Approve' }) as HTMLButtonElement
    expect(approve.disabled).toBe(true)
    fireEvent.click(approve)
    expect(posts(sent).length).toBe(0)
  })

  it('refuses a total over 16 MiB', async () => {
    const { card, sent } = await mount(view())
    pick([file('a.pdf', 9 * 1024 * 1024), file('b.png', 8 * 1024 * 1024)])
    const problem = await within(card).findByTestId('ai-files-problem')
    expect(problem.textContent).toMatch(/at most 16 MiB/)
    const approve = within(card).getByRole('button', { name: 'Approve' }) as HTMLButtonElement
    expect(approve.disabled).toBe(true)
    fireEvent.click(approve)
    expect(posts(sent).length).toBe(0)
  })

  it('takes exactly ten files and exactly 16 MiB with the form', () => {
    const ten = Array.from({ length: 10 }, (_, i) => ({ name: `f${i}.md`, size: 1 }))
    expect(filesProblem(ten, 10)).toBeNull()
    expect(filesProblem([...ten, { name: 'g.md', size: 1 }], 10)).not.toBeNull()
    expect(filesProblem([{ name: 'a.md', size: MAX_ANSWER_BYTES - 100 }], 10, 100)).toBeNull()
    expect(filesProblem([{ name: 'a.md', size: MAX_ANSWER_BYTES - 100 }], 10, 101)).not.toBeNull()
    // The step's own bound is below ten.
    expect(filesProblem([{ name: 'a.md', size: 1 }, { name: 'b.md', size: 1 }], 1)).not.toBeNull()
    // A name or a type the server would refuse.
    expect(filesProblem([{ name: 'tools.zip', size: 1 }], 10)).not.toBeNull()
    expect(filesProblem([{ name: '../up.md', size: 1 }], 10)).not.toBeNull()
    expect(filesProblem([{ name: 'A.md', size: 1 }, { name: 'a.md', size: 1 }], 10)).not.toBeNull()
  })

  it('shows the warning above the picker and again beside Send', async () => {
    const { card } = await mount(view())
    const above = within(card).getByTestId('ai-warning-picker')
    const beside = within(card).getByTestId('ai-warning-send')
    expect(above.textContent).toBe(FILE_WARNING)
    expect(beside.textContent).toBe(FILE_WARNING)
    const picker = card.querySelector('input[type="file"]')!
    // Above: the warning comes before the picker in the document.
    expect(above.compareDocumentPosition(picker) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // Beside: in the same row as the Send buttons.
    const row = beside.parentElement!
    expect(within(row).getByRole('button', { name: 'Approve' })).toBeTruthy()
  })

  it('max_files 0 shows no picker and no warning', async () => {
    const { card } = await mount(view({ max_files: 0 }))
    expect(card.querySelector('input[type="file"]')).toBeNull()
    expect(within(card).queryByTestId('ai-warning-picker')).toBeNull()
    expect(within(card).queryByTestId('ai-warning-send')).toBeNull()
    expect(within(card).getByRole('button', { name: 'Approve' })).toBeTruthy()
  })
})

describe('a step that is no longer waiting', () => {
  const answer = {
    answer_id: 'r1-owner',
    answered_by: 'owner@example.com',
    answered_at: at(-600_000),
    via: 'console',
    sha256: 'a'.repeat(64),
    bytes: 40_300,
    files: [
      { name: 'review.files/shot.png', bytes: 40_000, sha256: 'b'.repeat(64) },
      { name: 'review.files/notes.md', bytes: 213, sha256: 'c'.repeat(64) },
    ],
  }

  it('answered: says who, when and how, and offers no form', async () => {
    const { card } = await mount(view({ status: 'answered', may_answer: false, answer }))
    const closed = within(card).getByTestId('ai-closed')
    expect(closed.dataset.status).toBe('answered')
    expect(closed.textContent).toContain('owner@example.com')
    expect(closed.querySelector(`time[datetime="${answer.answered_at}"]`)).not.toBeNull()
    expect(closed.textContent).toContain('in the console')
    expect(within(card).queryByRole('button', { name: 'Approve' })).toBeNull()
    expect(card.querySelector('textarea, input[type="file"]')).toBeNull()
    expect(within(card).queryByTestId('ai-pill')).toBeNull()
  })

  it('expired: says nobody answered, when it expired and how it ended, and offers no form', async () => {
    const expires = at(-60_000)
    const { card } = await mount(view({ status: 'expired', may_answer: false, expires_at: expires, on_expire: 'default' }))
    const closed = within(card).getByTestId('ai-closed')
    expect(closed.dataset.status).toBe('expired')
    expect(closed.textContent).toContain('Nobody answered')
    expect(closed.querySelector(`time[datetime="${expires}"]`)).not.toBeNull()
    expect(closed.textContent).toContain('took the default')
    expect(within(card).queryByRole('button', { name: /Approve|Send|Request changes/ })).toBeNull()
    expect(card.querySelector('textarea, input[type="file"]')).toBeNull()
  })

  it('offers an image attachment as a download, never rendered', async () => {
    const { card } = await mount(view({ status: 'answered', may_answer: false, answer }))
    expect(card.querySelector('img, iframe, object, embed')).toBeNull()
    const files = within(card).getByRole('list', { name: "The answer's files" })
    const shot = files.querySelector<HTMLElement>('li[data-file="review.files/shot.png"]')!
    const link = shot.querySelector<HTMLAnchorElement>('a[download]')!
    expect(link.getAttribute('download')).toBe('shot.png')
    const href = new URL(link.getAttribute('href')!, 'http://x')
    expect(href.pathname).toBe('/v1/tasks/task_review/artifacts/raw')
    expect(href.searchParams.get('name')).toBe('review.files/shot.png')
    expect(href.searchParams.get('disposition')).toBe('attachment')
  })
})

describe('the waiting pill', () => {
  it('reads "waiting for a member", names the tenant, and says when it expires', async () => {
    const { card } = await mount(view())
    const pill = within(card).getByTestId('ai-pill')
    expect(pill.textContent).toBe('waiting for a member')
    expect(card.textContent).not.toContain('waiting for you')
    expect(pill.getAttribute('title')).toBe('Any member of workspace eng may answer.')
    expect(card.textContent).toContain('expires in 3 h 20 m')
  })

  it('a member who may not answer is told so and shown no form', async () => {
    const { card } = await mount(view({ may_answer: false }))
    expect(within(card).getByTestId('ai-not-member').textContent).toBe(
      'Only a member of workspace eng may answer, checked when the answer arrives. Switch to eng to answer it.')
    expect(within(card).queryByRole('button', { name: 'Approve' })).toBeNull()
  })

  // VQ-056 (BGD, 2026-10-26): the console speaks of workspaces; the `tenant`
  // field keeps its name, the words a person reads do not say "tenant".
  it('says workspace, never tenant, in anything a reader sees', async () => {
    for (const may of [true, false]) {
      const { card, unmount } = await mount(view({ may_answer: may }))
      const read = [card.textContent ?? '', ...Array.from(card.querySelectorAll('[title]'), (e) => e.getAttribute('title') ?? '')]
      expect(read.filter((t) => /\btenants?\b/i.test(t))).toEqual([])
      unmount()
    }
    expect(waitingTitle('eng')).toBe(
      'Waiting for any member of workspace eng to answer. Holds no capacity; it ends on the first answer or at its declared expiry.')
    expect(waitingTitle(null)).toBe(
      'Waiting for any member of its workspace to answer. Holds no capacity; it ends on the first answer or at its declared expiry.')
  })
})
