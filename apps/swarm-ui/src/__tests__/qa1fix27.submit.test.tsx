/**
 * VISUAL QA PASS 1, LANE QA1-FIX-27 (part of #2596): Submit's chooser and
 * Submit a workflow, on the fixture console.
 *
 *   runs-submit-02  A 404 from POST /v1/workflows read as "We cannot say
 *                   whether that workflow was created … expired sign-in". A
 *                   refusal with a status is classified by that status; only a
 *                   2xx page stays the expired-sign-in unknown.
 *   runs-submit-17  "Ready to send" headed a summary whose priority row said
 *                   "out of range" over a disabled button.
 *   runs-submit-22  The stage was labelled by a raw word in bold mono
 *                   ("first", "then"); it is "Stage N".
 *   runs-submit-26  Five recent rows read alike ("claude-code agent · <hex>");
 *                   the footnote counted windows the list never showed.
 *   chrome-C30      "Visual QA of a registered app" sat above step 1; the form
 *                   opens on the plan, and the entry follows it.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { recentSubmissions, recentScope } from '../SubmitChooser'
import { SubmitWorkflowScreen, postWorkflow } from '../SubmitWorkflow'
import { task } from './runfixture'
import SUBMIT_CSS from '../styles/submit.css?raw'

const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

function respond(status: number, body: string, type: string): void {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(body, { status, headers: { 'content-type': type } })))
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('runs-submit-02: a refused workflow POST is classified by its status', () => {
  it('reads a 404 page as not created, never as "may exist"', async () => {
    respond(404, '<!doctype html><title>Not found</title>', 'text/html')
    const sub = await postWorkflow({ steps: [] })
    // MUTATION: send every non-JSON answer to `session_expired`/uncertain
    // again (drop `res.ok &&` in postWorkflow) and this reads 'uncertain'.
    expect(sub.kind).toBe('rejected')
    if (sub.kind !== 'rejected') throw new Error(sub.kind)
    expect(sub.error.kind).toBe('not_found')
    expect(sub.error.httpStatus).toBe(404)
  })

  it('keeps a 2xx page as the expired sign-in it is, and a 5xx page as unknown', async () => {
    respond(200, '<html>sign in</html>', 'text/html')
    const signIn = await postWorkflow({ steps: [] })
    expect(signIn.kind).toBe('uncertain')
    if (signIn.kind === 'uncertain') expect(signIn.error.kind).toBe('session_expired')
    respond(502, '<html>bad gateway</html>', 'text/html')
    const gateway = await postWorkflow({ steps: [] })
    expect(gateway.kind).toBe('uncertain')
    if (gateway.kind === 'uncertain') expect(gateway.error.httpStatus).toBe(502)
  })
})

async function readyForm() {
  const view = render(<SubmitWorkflowScreen />)
  await screen.findByRole('button', { name: 'Submit this workflow' }, { timeout: 4000 })
  const select = view.container.querySelector<HTMLSelectElement>('.wfb-step select.sb-runner-select')!
  fireEvent.change(select, { target: { value: 'mock' } })
  return view
}

describe('runs-submit-17: the summary heading follows what disables the button', () => {
  it('reads "Not ready to send" over a priority out of range', async () => {
    const { container } = await readyForm()
    const head = () => visible(container.querySelector('.sbf-send h2'))
    // The control: the same plan with the default priority is ready.
    await waitFor(() => expect(head()).toBe('Ready to send'))
    fireEvent.change(container.querySelector<HTMLInputElement>('#wf-priority')!, { target: { value: '500' } })
    expect(visible(container.querySelector('.sbf-send'))).toContain('out of range (−100 to 100)')
    expect(screen.getByRole('button', { name: 'Submit this workflow' })).toHaveProperty('disabled', true)
    // MUTATION: leave `!priorityOk` out of `blocked` and this reads "Ready to send".
    expect(head()).toBe('Not ready to send')
  }, 20000)
})

describe('runs-submit-02 (re-review): a 404 with no reason says the route is missing, not the plan', () => {
  it('does not tell the reader to correct a plan nothing refused', async () => {
    const { container } = await readyForm()
    respond(404, '<!doctype html><title>Not found</title>', 'text/html')
    fireEvent.click(screen.getByRole('button', { name: 'Submit this workflow' }))
    const card = await waitFor(() => {
      const el = container.querySelector('.sbf-side .state.failed')
      if (el === null) throw new Error('no refusal card yet')
      return el
    })
    const said = visible(card)
    expect(said).toContain('Nothing was created')
    // MUTATION: drop `unserved` in SubmitOutcome and the card reads "Correct
    // the plan and submit again." over a plan nothing refused, and says
    // "HTTP 404" twice ("The API returned HTTP 404." over "HTTP 404").
    expect(said).not.toContain('Correct the plan')
    expect(said).toContain('This deployment has no route that takes a workflow')
    expect(said.match(/HTTP 404/g)?.length).toBe(1)
  }, 20000)
})

describe('runs-submit-22 and chrome-C30: the plan opens the form, its stages named in words', () => {
  it('labels each stage "Stage N", not a raw word', async () => {
    const { container } = await readyForm()
    const heads = () => [...container.querySelectorAll('.wfb-stage-n')].map(visible)
    // MUTATION: draw 'first' / 'then' again and this reads ['first'].
    expect(heads()).toEqual(['Stage 1'])
    expect(visible(container.querySelector('.wfb-stage-say'))).toBe('starts immediately')
    fireEvent.click(container.querySelector<HTMLButtonElement>('button.wfb-add.is-stage')!)
    expect(heads()).toEqual(['Stage 1', 'Stage 2'])
  }, 20000)

  it('puts the Visual QA entry after the plan, not above step 1', async () => {
    const { container } = await readyForm()
    const entry = container.querySelector('[data-entry="visual-qa"]')!
    const firstStage = container.querySelector('.wfb-stage')!
    expect(entry).not.toBeNull()
    // MUTATION: render <VisualQaEntry /> first in `.sbf-build` again and the
    // entry precedes step 1.
    expect(firstStage.compareDocumentPosition(entry) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(container.querySelector('.sbf-build')!.firstElementChild).not.toBe(entry)
  }, 20000)
})

describe('runs-submit-26: each recent row says what it was, and the footnote counts the rows', () => {
  const ME = 'alice@example.com'
  const lone = { submitted_by: ME, step_id: null, workflow_id: null, parent_task_id: null }

  it('names a task by its title, else its prompt\'s first line, and carries its state', () => {
    const got = recentSubmissions(ME, [
      task({ ...lone, id: 'task_titled', created_at: '2026-10-04T12:00:00Z', state: 'RUNNING', metadata: { title: 'Fix flaky lease test' }, input: { prompt: 'ignored' } }),
      task({ ...lone, id: 'task_prompted', created_at: '2026-10-04T11:00:00Z', state: 'QUEUED', metadata: null, input: { prompt: 'Open the page\nand report what it says.' } }),
      task({ ...lone, id: 'task_bare', created_at: '2026-10-04T10:00:00Z', state: 'FAILED', metadata: null, input: null }),
    ], [], [])
    // MUTATION: name every task by `agentName` alone again and the first two
    // read "claude-code agent · …".
    expect(got.map((g) => g.name)).toEqual(['Fix flaky lease test', 'Open the page', 'claude-code agent · bare'])
    // MUTATION: drop `state` from a row and three bare rows read alike.
    expect(got.map((g) => g.state)).toEqual(['running', 'queued', 'failed'])
  })

  it('counts what is listed before the windows it was found in', () => {
    const items = recentSubmissions(ME, [
      task({ ...lone, id: 'task_a', created_at: '2026-10-04T12:00:00Z' }),
      task({ ...lone, id: 'task_b', created_at: '2026-10-04T11:00:00Z' }),
    ], [], [])
    const looked = [
      { kind: 'task' as const, read: 30, yours: 2 },
      { kind: 'workflow' as const, read: 3, yours: 0 },
      { kind: 'issue run' as const, read: 2, yours: 0 },
    ]
    // MUTATION: print only the windows ("Among …") again and nothing says the
    // list holds two agents and no workflow or issue run.
    expect(recentScope(items, looked)).toBe(
      'Listed: 2 agents, newest first. Found among your newest 30 agents. None of yours among the tenant’s newest 3 workflows or its newest 2 issue runs.',
    )
    expect(recentScope([], [{ kind: 'workflow', read: 1, yours: 0 }])).toBe('Among the tenant’s newest 1 workflow.')
    expect(recentScope([], [])).toBeNull()
  })
})

describe('runs-submit-26 (re-review): on a phone the state does not cut the name', () => {
  it('drops a recent row\'s state and age under its name at 560px and below', () => {
    // At 390px the state word ("dispatched · 9d ago") beside the name cut
    // "claude-code agent · 19d91b82" to "claude-code agent · 19…": the id,
    // the one part that tells the rows apart, was what went. jsdom lays out
    // nothing, so the rule is read from the sheet. MUTATION: delete the
    // phone block for `.sb-recent-i` and neither match is found.
    const flat = SUBMIT_CSS.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\s+/g, ' ')
    const phone = [...flat.matchAll(/@media \(max-width: 560px\) \{(.*?\})\s*\}/g)].map((m) => m[1]).join(' ')
    expect(phone).toMatch(/\.sb-recent-i \{[^}]*grid-template-columns: auto minmax\(0, 1fr\);/)
    expect(phone).toMatch(/\.sb-recent-at \{[^}]*grid-column: 2;/)
  })
})
