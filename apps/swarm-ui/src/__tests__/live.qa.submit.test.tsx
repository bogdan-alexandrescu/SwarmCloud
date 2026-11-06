// LIVE QA, SECTION 6 (submit), 2026-10-11: the S1 and S2 findings, each case
// asserting the property the finding is about -- how many POSTs left the
// page, what the click would send, whether the form calls itself ready --
// rather than the markup's shape:
//
//   SUB-1  after a 201, submitting the unchanged task form again (Enter in the
//          repository field is an implicit submission) posts nothing, and two
//          submissions inside one request post once;
//   SUB-3  a repository URL the API's scheme check refuses is "Not ready to
//          send" with the button off, on the task and the workflow forms;
//   SUB-4  an input over the served `max_input_bytes` is not ready, measured
//          as the API measures it (`validation.firestore_size`);
//   SUB-5  the workflow card links to the workflow it created;
//   SUB-6  the hub's recent card asks for YOUR tasks and small windows.
//
// SUB-2 and SUB-13 are in intake.issue.test.tsx, beside that form's helpers.
//
// MUTATIONS, each of which turns a case red: drop the `inFlight`/`justSent`
// guard from `submit()` in Submit.tsx; drop `badRepo` from either form's
// `blocked`; drop `tooBig` from the task form's `blocked`; make `inputBytes`
// count UTF-16 code units; put `<span className="mono">` back for the
// workflow id; call `loadTasks()` bare from `useRecentRead`.
//
// Nothing here is credential-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'

// These hand the submit real responses, so the fixture build's answer to the
// submit (QA2-FIX-07, runs2-01 and runs2-02) is turned off here.
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), USE_FIXTURES: false }))
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import { SubmitScreen, byteWords, inputBytes, inputSizeProblem } from '../Submit'
import { SubmitWorkflowScreen } from '../SubmitWorkflow'

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

const realFetch = globalThis.fetch

/** Answer every POST to `path` with `reply`, after `delay` ms; reads go to the fixture. */
function answerWrite(path: string, reply: { status: number; body: unknown }, delay = 0) {
  const posts: unknown[] = []
  const prior = globalThis.fetch
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if ((init?.method ?? 'GET') === 'POST' && url.startsWith(path)) {
      posts.push(JSON.parse(String(init?.body ?? 'null')))
      if (delay > 0) await new Promise((r) => setTimeout(r, delay))
      return new Response(JSON.stringify(reply.body), { status: reply.status, headers: JSON_HEADERS })
    }
    return prior(input, init)
  }) as unknown as typeof fetch
  return posts
}

afterEach(() => {
  globalThis.fetch = realFetch
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  vi.unstubAllEnvs()
})

async function taskForm(runner = 'mock') {
  const r = render(<SubmitScreen />)
  await screen.findByRole('button', { name: 'Start one agent' }, WAIT)
  fireEvent.click(r.container.querySelector<HTMLInputElement>(`input[name="runner-profile"][value="${runner}"]`)!)
  return r.container
}

async function workflowForm() {
  const r = render(<SubmitWorkflowScreen />)
  await screen.findByRole('button', { name: 'Submit this workflow' }, WAIT)
  fireEvent.change(r.container.querySelector<HTMLSelectElement>('.wfb-step select.sb-runner-select')!, { target: { value: 'mock' } })
  return r.container
}

const panel = (c: HTMLElement) => c.querySelector<HTMLElement>('.sbf-send')!
const heading = (c: HTMLElement) => visible(panel(c).querySelector('h2'))
const repoField = (c: HTMLElement) => c.querySelector<HTMLInputElement>('#dsp-repo')!

describe('SUB-1: a task is posted once per request, whatever submits the form', () => {
  it('after a 201, submitting the unchanged form again (Enter in the repository field) posts nothing', async () => {
    const c = await taskForm()
    fireEvent.change(repoField(c), { target: { value: 'https://github.com/example-org/infra.git' } })
    const posts = answerWrite('/v1/tasks', { status: 201, body: { task: { id: 't_live_1', state: 'READY' }, scheduler_woken: true } })
    fireEvent.submit(c.querySelector('form')!)
    await within(panel(c)).findByRole('button', { name: 'Submit another' }, WAIT)
    expect(posts).toHaveLength(1)

    // jsdom has no implicit submission, so this is what Enter in the one text
    // input left on the form fires: the form's own submit.
    repoField(c).focus()
    fireEvent.keyDown(repoField(c), { key: 'Enter' })
    fireEvent.submit(c.querySelector('form')!)
    fireEvent.submit(c.querySelector('form')!)
    await new Promise((r) => setTimeout(r, 50))
    expect(posts, 'the unchanged form posted the same task again').toHaveLength(1)
    expect(heading(c)).toBe('Sent')
  })

  it('two submissions while the first is in flight post once', async () => {
    const c = await taskForm()
    const posts = answerWrite('/v1/tasks', { status: 201, body: { task: { id: 't_live_2', state: 'READY' }, scheduler_woken: true } }, 150)
    const form = c.querySelector('form')!
    fireEvent.submit(form)
    fireEvent.submit(form)
    fireEvent.submit(form)
    await within(panel(c)).findByRole('button', { name: 'Submit another' }, WAIT)
    expect(posts).toHaveLength(1)
  })

  it('a changed form is a new request and is sent (the control)', async () => {
    const c = await taskForm()
    const posts = answerWrite('/v1/tasks', { status: 201, body: { task: { id: 't_live_3', state: 'READY' }, scheduler_woken: true } })
    fireEvent.submit(c.querySelector('form')!)
    await within(panel(c)).findByRole('button', { name: 'Submit another' }, WAIT)
    fireEvent.change(repoField(c), { target: { value: 'https://github.com/example-org/other.git' } })
    fireEvent.submit(c.querySelector('form')!)
    await waitFor(() => expect(posts).toHaveLength(2), WAIT)
  })
})

describe('SUB-3: a repository the API will refuse is not ready to send', () => {
  it('the task form: "not a repo" holds the heading and the button, for collect and direct-pr, and Enter posts nothing', async () => {
    const c = await taskForm()
    const posts = answerWrite('/v1/tasks', { status: 201, body: { task: { id: 't_never', state: 'READY' }, scheduler_woken: true } })
    expect(heading(c)).toBe('Ready to send')
    fireEvent.change(repoField(c), { target: { value: 'not a repo' } })
    expect(heading(c)).toBe('Not ready to send')
    expect((screen.getByRole('button', { name: 'Start one agent' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(c.querySelector<HTMLInputElement>('input[name="dispatch-strategy"][value="direct-pr"]')!)
    expect(heading(c)).toBe('Not ready to send')
    fireEvent.submit(c.querySelector('form')!)
    await new Promise((r) => setTimeout(r, 50))
    expect(posts).toHaveLength(0)
    // The control: a URL the API takes makes it ready again.
    fireEvent.change(repoField(c), { target: { value: 'git@github.com:example-org/infra.git' } })
    expect(heading(c)).toBe('Ready to send')
  })

  it('the workflow form: "foo" holds the heading and the button', async () => {
    const c = await workflowForm()
    expect(heading(c)).toBe('Ready to send')
    fireEvent.change(repoField(c), { target: { value: 'foo' } })
    expect(heading(c)).toBe('Not ready to send')
    expect((screen.getByRole('button', { name: 'Submit this workflow' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(repoField(c), { target: { value: 'https://github.com/example-org/infra.git' } })
    expect(heading(c)).toBe('Ready to send')
  })
})

describe('SUB-4: an input over the served limit is not ready to send', () => {
  it('measures an input as the API does: UTF-8 bytes + 1 a string, 8 a number, 1 a flag, keys as strings', () => {
    // 'prompt' 6+1, 'é' is 2 UTF-8 bytes +1. UTF-16 would say 1.
    expect(inputBytes({ prompt: 'é' })).toBe(10)
    // n: 1+1 and 8; b: 1+1 and 1; l: 1+1 and ('a' 1+1, 2 is 8).
    expect(inputBytes({ n: 1, b: true, l: ['a', 2] })).toBe(10 + 3 + 12)
    expect(inputBytes({})).toBe(0)
    expect(inputSizeProblem({ p: 'x'.repeat(100) }, 50)).toBe("the input is 103 bytes, over the API's 50 bytes limit")
    expect(inputSizeProblem({ p: 'x'.repeat(100) }, 1024)).toBeNull()
    // An unread limit is not a limit.
    expect(inputSizeProblem({ p: 'x'.repeat(1_000_000) }, null)).toBeNull()
    expect(byteWords(256 * 1024)).toBe('256 KiB')
  })

  it('the task form: a prompt over the fixture\'s 64 KiB reads "Not ready to send" with the limit, and one under it is ready', async () => {
    const c = await taskForm('claude-code')
    const prompt = c.querySelector<HTMLTextAreaElement>('.sbf-field.is-required textarea')!
    fireEvent.change(prompt, { target: { value: 'x'.repeat(70_000) } })
    await waitFor(() => expect(heading(c)).toBe('Not ready to send'), WAIT)
    expect(visible(panel(c))).toContain("over the API's 64 KiB limit")
    expect((screen.getByRole('button', { name: 'Start one agent' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(prompt, { target: { value: 'x'.repeat(1_000) } })
    expect(heading(c)).toBe('Ready to send')
  })
})

describe('SUB-5: the workflow card is a way back to the workflow', () => {
  it('links the created id to its page', async () => {
    const c = await workflowForm()
    answerWrite('/v1/workflows', { status: 201, body: {
      workflow: { workflow_id: 'wf_live_1', steps: [{ step_id: 'mock-1' }], created_at: '2026-10-11T00:00:00Z' }, dispatch: null,
    } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit this workflow' }))
    const link = await within(panel(c)).findByRole('link', { name: 'wf_live_1' }, WAIT)
    expect(link.getAttribute('href')).toBe('#work/workflows?wf=wf_live_1')
  })
})

describe('SUB-6: the hub reads your recent submissions, not the tenant\'s whole history', () => {
  it('asks /v1/tasks for submitted_by=me, and every list for a small window', async () => {
    vi.stubEnv('VITE_LIVE', '1')
    // A mock outlives resetModules: the live api is the real module, read afresh.
    vi.doUnmock('../api')
    vi.resetModules()
    const urls: string[] = []
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      urls.push(url)
      const body = url.startsWith('/v1/tasks') ? { tasks: [], next_page_token: null, tenant_id: 'eng' }
        : url.startsWith('/v1/workflows') ? { workflows: [], next_page_token: null }
          : url.startsWith('/v1/runs') ? { runs: [], next_page_token: null }
            : { code: 'not_found', message: 'no stub' }
      return new Response(JSON.stringify(body), { status: url.startsWith('/v1/tenants/me') ? 404 : 200, headers: JSON_HEADERS })
    }) as unknown as typeof fetch
    const { SubmitChooser } = await import('../SubmitChooser')
    render(<SubmitChooser go={() => {}} />)
    const query = (prefix: string) => {
      const u = urls.find((x) => x.startsWith(prefix))
      expect(u, `${prefix} was not read`).toBeDefined()
      return new URL(u!, 'http://x').searchParams
    }
    await waitFor(() => expect(urls.some((u) => u.startsWith('/v1/runs'))).toBe(true), WAIT)
    const tasks = query('/v1/tasks')
    expect(tasks.get('submitted_by')).toBe('me')
    expect(Number(tasks.get('limit'))).toBeLessThanOrEqual(30)
    expect(Number(query('/v1/workflows').get('limit'))).toBeLessThanOrEqual(20)
    expect(Number(query('/v1/runs').get('limit'))).toBeLessThanOrEqual(20)
  })
})
