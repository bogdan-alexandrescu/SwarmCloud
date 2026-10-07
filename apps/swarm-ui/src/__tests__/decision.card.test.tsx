// THE DECISION CARD (owner request 2026-10-07, on a fix step skipped because
// the review said MERGE): "right now I have no idea what happened and why
// that decision was made and based on what."
//
// Every fact below is one the worker already writes into the step's
// `result_summary` (agent_worker/lifecycle.py `_evaluate_verdict_gate`, the
// summary block, `_adopt_pull_request_text`) or the API already lifts into
// `dispatch` (swarm_api/codec.py `dispatch_of`). The card draws them; these
// tests drive `Run` -- the Details tab's body -- and the Artifacts pane through
// the real App, and assert on what is drawn:
//
//   * a MERGE-skipped step: the rule, the verdict linked to the review and to
//     its verdict.json, the findings grouped by severity with a count line,
//     "No agent was started", the PR, the branch's short sha, and whose PR
//     text it used, and the inputs it staged, each linked to its source;
//   * a NOT_YET step: "agent ran because", with the findings it was handed;
//   * a review step: "Verdict this review wrote", and the step it gated and
//     what that step did;
//   * fields the worker did not record: dashes, each with its reason, never
//     an invented verdict or a zero count;
//   * the Artifacts tab of a skipped step says why it holds only the PR text.
//
// CONTROL: each case asserts a value only its own fixture carries (the PR
// number, the sha, the finding texts), so a card drawn from the wrong task or
// from nothing at all goes red.

import { render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AgentRun, WorkflowRead } from '../api'
import type { Task, Workflow } from '../types'
import { at, attempt, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadWorkflow: vi.fn(),
  loadArtifactContent: vi.fn(),
  loadTaskInputOnce: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { Run } = await import('../AgentDetail')

const WF = 'wf_decide'
const IMPL = 'task_impl0000000000000001'
const REVIEW = 'task_review00000000000001'
const FIX = 'task_fix00000000000000001'
const SHA = '0123456789abcdef'.repeat(3).slice(0, 40)
const PR_URL = 'https://github.com/example/swarm/pull/812'

const gs = (id: string, name: string) => `gs://swarm-artifacts/tenants/acme/tasks/${id}/attempts/att_1/artifacts/${name}`

/** Four minors, three as the worker keeps a structured finding (JSON text) and one as an object. */
const MINORS = [
  JSON.stringify({ file: 'apps/a.ts', fix: 'rename the helper', problem: 'the helper name says less than it does', severity: 'minor' }),
  JSON.stringify({ file: 'apps/b.ts', fix: 'drop the dead branch', problem: 'a branch no input reaches', severity: 'minor' }),
  JSON.stringify({ file: 'apps/c.css', fix: 'use the scale token', problem: 'a literal font size', severity: 'minor' }),
  { file: 'docs/d.md', problem: 'a stale sentence', fix: 'say what was built', severity: 'minor' },
]

function gateDispatch(over: Record<string, unknown> = {}): Task['dispatch'] {
  return {
    strategy: 'direct-pr',
    carrier: 'branches',
    role: null,
    integrates: [],
    builds_on: IMPL,
    verdict_gate: { task_id: REVIEW, verdict_in: ['NOT_YET'] },
    ...over,
  } as unknown as Task['dispatch']
}

function implTask(): Task {
  return runTask({ id: IMPL, workflow_id: WF, step_id: 'implement', state: 'SUCCEEDED' })
}

function reviewTask(over: Partial<Task> = {}): Task {
  return runTask({
    id: REVIEW,
    workflow_id: WF,
    step_id: 'review',
    state: 'SUCCEEDED',
    started_at: at(1),
    completed_at: at(5),
    result_summary: { artifacts: [{ name: 'verdict.json', bytes: 400, uri: gs(REVIEW, 'verdict.json') }] },
    ...over,
  })
}

function mergeSkipped(over: Partial<Task> = {}): Task {
  return runTask({
    id: FIX,
    workflow_id: WF,
    step_id: 'fix',
    state: 'SUCCEEDED',
    started_at: at(6),
    completed_at: at(8),
    repository_url: 'https://github.com/example/swarm',
    dispatch: gateDispatch(),
    metadata: { workflow_step: 'fix' },
    result_summary: {
      verdict_gate: {
        task_id: REVIEW,
        file: 'verdict.json',
        verdict: 'MERGE',
        verdict_in: ['NOT_YET'],
        agent_ran: false,
        findings: MINORS,
        findings_dropped: 0,
      },
      skipped_agent: 'review verdict MERGE',
      pull_request_text_from: { title: 'implementer', body: 'implementer' },
      branch: { name: 'swarm/wf_decide-fix', head: SHA, head_sha: SHA, base_sha: 'f'.repeat(40) },
      git: {
        published: true,
        branch: 'swarm/wf_decide-fix',
        pull_request: { number: 812, url: PR_URL, state: 'open', created: true },
      },
      runner: {
        status: 'skipped',
        summary:
          "the review verdict was MERGE, and this step's agent runs only on NOT_YET; the agent was not started and the step published the reviewed work",
      },
      staged_inputs: [
        { task_id: IMPL, filename: 'swarm-work.patch', path: 'swarm-work.patch', bytes: 1200 },
        { task_id: REVIEW, filename: 'verdict.json', path: 'verdict.json', bytes: 400 },
      ],
      artifacts: [
        { name: 'pr-title.txt', bytes: 60, uri: gs(FIX, 'pr-title.txt') },
        { name: 'pr-body.md', bytes: 900, uri: gs(FIX, 'pr-body.md') },
      ],
    },
    ...over,
  })
}

function notYetRan(): Task {
  const base = mergeSkipped()
  const summary = base.result_summary as Record<string, unknown>
  return {
    ...base,
    result_summary: {
      ...summary,
      verdict_gate: {
        task_id: REVIEW,
        file: 'verdict.json',
        verdict: 'NOT_YET',
        verdict_in: ['NOT_YET'],
        agent_ran: true,
        findings: [
          { file: 'apps/x.py', problem: 'the lease is released twice', fix: 'release once, in finally', severity: 'blocker' },
          JSON.stringify({ file: 'apps/y.py', fix: 'count from LEASED', problem: 'the cap counts RUNNING', severity: 'major' }),
          'the docstring names the wrong route',
        ],
        findings_dropped: 0,
      },
      skipped_agent: undefined,
      pull_request_text_from: undefined,
      runner: { status: 'succeeded', summary: 'fixed both findings' },
    },
  }
}

function workflowRead(tasks: Task[]): WorkflowRead {
  const workflow = {
    workflow_id: WF,
    tenant_id: 'acme',
    state: 'SUCCEEDED',
    steps: tasks.map((t) => ({ step_id: t.step_id, task_id: t.id, depends_on: [], input_from: {}, runner_profile: 'claude-code', resource_class: 'standard' })),
  } as unknown as Workflow
  return { workflow, tasks }
}

function run(t: Task): AgentRun {
  return {
    task: t,
    events: [],
    eventsDetail: null,
    attempts: [attempt(1, { task_id: t.id })],
    attemptsDetail: null,
    classes: null,
    classesDetail: null,
    classesRouteMissing: false,
  }
}

function card(): HTMLElement {
  return screen.getByRole('region', { name: 'Decision' })
}

beforeEach(() => {
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskInputOnce.mockReturnValue(new Promise(() => {}))
  api.loadArtifactContent.mockReturnValue(new Promise(() => {}))
  api.loadWorkflow.mockResolvedValue({ status: 'ok', data: workflowRead([implTask(), reviewTask(), mergeSkipped()]), fetchedAt: Date.now() })
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('a step whose agent the review verdict MERGE kept from running', () => {
  it('states the rule in words from the gate, naming the review step', async () => {
    render(<Run run={run(mergeSkipped())} />)
    await waitFor(() => expect(within(card()).getAllByRole('link', { name: 'review' }).length).toBeGreaterThan(0))
    expect(card().textContent).toContain("This step's agent runs only when review says NOT_YET.")
    // The card leads the tab: it is the first card under the alerts.
    const cards = Array.from(document.querySelectorAll('.dt > section.dt-card'))
    expect(cards[0]?.getAttribute('aria-label')).toBe('Decision')
  })

  it('links the verdict to the review inspector and to its verdict.json in the viewer', async () => {
    render(<Run run={run(mergeSkipped())} />)
    await waitFor(() => expect(within(card()).getAllByRole('link', { name: 'review' }).length).toBeGreaterThan(0))
    const verdict = within(card()).getByTestId('decision-verdict')
    expect(verdict.textContent).toContain('MERGE')
    expect(within(verdict).getByRole('link', { name: 'review' }).getAttribute('href')).toBe(`#work/task/${REVIEW}`)
    expect(within(verdict).getByRole('link', { name: 'verdict.json' }).getAttribute('href')).toBe(
      `#work/task/${REVIEW}/artifacts/verdict.json`,
    )
    // When it was read: the worker reads the gate as the step's attempt
    // starts, and says no other instant; the card says which.
    expect(verdict.textContent).toMatch(/read as this step started · 2026-09-22 10:06:00 UTC/)
  })

  it('groups what the review weighed by severity, with a count line and each finding whole', async () => {
    render(<Run run={run(mergeSkipped())} />)
    const weighed = within(card()).getByTestId('decision-findings')
    expect(within(weighed).getByTestId('decision-count').textContent).toBe('0 blockers, 0 majors, 4 minors')
    const minors = within(weighed).getByRole('list', { name: 'Minor findings' })
    const items = within(minors).getAllByRole('listitem')
    expect(items).toHaveLength(4)
    expect(items[0]?.textContent).toContain('apps/a.ts')
    expect(items[0]?.textContent).toContain('the helper name says less than it does')
    expect(items[0]?.textContent).toContain('rename the helper')
    expect(items[3]?.textContent).toContain('docs/d.md')
    expect(items[3]?.textContent).toContain('say what was built')
    expect(within(weighed).queryByRole('list', { name: 'Blocker findings' })).toBeNull()
  })

  it('says no agent was started and what the step published, from which branch, sha and PR text', async () => {
    render(<Run run={run(mergeSkipped())} />)
    await waitFor(() => expect(within(card()).getAllByRole('link', { name: 'implement' }).length).toBeGreaterThan(0))
    const did = within(card()).getByTestId('decision-happened')
    expect(did.textContent).toContain('No agent was started.')
    expect(within(did).getByRole('link', { name: 'PR #812' }).getAttribute('href')).toBe(PR_URL)
    expect(did.textContent).toContain('swarm/wf_decide-fix')
    const sha = within(did).getByRole('link', { name: '0123456' })
    expect(sha.getAttribute('href')).toBe(`https://github.com/example/swarm/commit/${SHA}`)
    expect(did.textContent).toMatch(/PR title and body from implement/)
    expect(within(did).getAllByRole('link', { name: 'implement' })[0]?.getAttribute('href')).toBe(`#work/task/${IMPL}`)
    // The worker's own sentence, as it wrote it.
    expect(did.textContent).toContain('the agent was not started and the step published the reviewed work')
  })

  it('lists the inputs it staged, each linked to its source step and id', async () => {
    render(<Run run={run(mergeSkipped())} />)
    await waitFor(() => expect(within(card()).getAllByRole('link', { name: 'implement' }).length).toBeGreaterThan(0))
    const staged = within(card()).getByRole('list', { name: 'Inputs it staged' })
    const rows = within(staged).getAllByRole('listitem')
    expect(rows).toHaveLength(2)
    expect(rows[0]?.textContent).toContain('swarm-work.patch')
    expect(within(rows[0] as HTMLElement).getByRole('link', { name: 'implement' }).getAttribute('href')).toBe(`#work/task/${IMPL}`)
    expect(rows[0]?.textContent).toContain(IMPL)
    expect(rows[1]?.textContent).toContain('verdict.json')
    expect(within(rows[1] as HTMLElement).getByRole('link', { name: 'review' }).getAttribute('href')).toBe(`#work/task/${REVIEW}`)
  })
})

describe('a step whose agent the review verdict NOT_YET started', () => {
  it('says the agent ran because of the verdict, with the findings it was handed', async () => {
    render(<Run run={run(notYetRan())} />)
    await waitFor(() => expect(within(card()).getAllByRole('link', { name: 'review' }).length).toBeGreaterThan(0))
    const did = within(card()).getByTestId('decision-happened')
    expect(did.textContent).toMatch(/This step's agent ran because review said NOT_YET/)
    expect(did.textContent).toContain('3 findings')
    expect(did.textContent).not.toContain('No agent was started')
    const weighed = within(card()).getByTestId('decision-findings')
    expect(within(weighed).getByTestId('decision-count').textContent).toBe('1 blocker, 1 major, 0 minors, 1 not graded')
    expect(within(weighed).getByRole('list', { name: 'Blocker findings' }).textContent).toContain('the lease is released twice')
    expect(within(weighed).getByRole('list', { name: 'Major findings' }).textContent).toContain('count from LEASED')
    expect(within(weighed).getByRole('list', { name: 'Not graded findings' }).textContent).toContain('the docstring names the wrong route')
  })
})

describe("a review step's own inspector", () => {
  it('shows the verdict this review wrote and the step it gated, with what that step did', async () => {
    render(<Run run={run(reviewTask())} />)
    await waitFor(() => expect(within(card()).getByText('Verdict this review wrote')).toBeTruthy())
    const verdict = within(card()).getByTestId('decision-verdict')
    expect(verdict.textContent).toContain('MERGE')
    expect(within(verdict).getByRole('link', { name: 'verdict.json' }).getAttribute('href')).toBe(
      `#work/task/${REVIEW}/artifacts/verdict.json`,
    )
    expect(within(card()).getByTestId('decision-count').textContent).toBe('0 blockers, 0 majors, 4 minors')
    const gated = within(card()).getByTestId('decision-gated')
    expect(within(gated).getByRole('link', { name: 'fix' }).getAttribute('href')).toBe(`#work/task/${FIX}`)
    expect(gated.textContent).toContain('runs only when this review says NOT_YET')
    expect(gated.textContent).toContain('No agent was started')
    expect(within(gated).getByRole('link', { name: 'PR #812' }).getAttribute('href')).toBe(PR_URL)
  })

  it('draws no card on a step nothing gates on', async () => {
    api.loadWorkflow.mockResolvedValue({ status: 'ok', data: workflowRead([implTask(), reviewTask()]), fetchedAt: Date.now() })
    render(<Run run={run(reviewTask())} />)
    await waitFor(() => expect(api.loadWorkflow).toHaveBeenCalledWith(WF))
    await new Promise((r) => setTimeout(r, 0))
    expect(screen.queryByRole('region', { name: 'Decision' })).toBeNull()
  })
})

describe('fields the worker did not record are dashes with reasons', () => {
  it('a gate block with no findings, no file, no branch and no PR-text source', async () => {
    const t = mergeSkipped({
      result_summary: {
        verdict_gate: { task_id: REVIEW, verdict: 'MERGE', verdict_in: ['NOT_YET'], agent_ran: false },
        git: { published: false, publish_reason: 'the push was rejected' },
      },
    })
    render(<Run run={run(t)} />)
    await waitFor(() => expect(within(card()).getAllByRole('link', { name: 'review' }).length).toBeGreaterThan(0))
    expect(within(card()).getByRole('img', { name: /did not record the review's findings/ })).toBeTruthy()
    expect(within(card()).queryByTestId('decision-count')).toBeNull()
    expect(within(card()).getByRole('img', { name: /did not record which file it read the verdict from/ })).toBeTruthy()
    const did = within(card()).getByTestId('decision-happened')
    expect(did.textContent).toContain('No agent was started.')
    expect(did.textContent).toContain('the push was rejected')
    expect(within(did).getByRole('img', { name: /did not record which branch/ })).toBeTruthy()
    expect(within(did).getByRole('img', { name: /did not record whose pull request title and body/ })).toBeTruthy()
    expect(within(card()).getByRole('img', { name: /reported no staged inputs/ })).toBeTruthy()
  })

  it('a gated step that failed before a verdict was recorded reads the verdict as unreadable', async () => {
    const t = runTask({
      id: FIX,
      workflow_id: WF,
      step_id: 'fix',
      state: 'FAILED',
      dispatch: gateDispatch(),
      last_error: "the verdict file 'verdict.json' staged from task task_review00000000000001 is not JSON (JSONDecodeError)",
      result_summary: null,
    })
    render(<Run run={run(t)} />)
    await waitFor(() => expect(within(card()).getAllByRole('link', { name: 'review' }).length).toBeGreaterThan(0))
    const verdict = within(card()).getByTestId('decision-verdict')
    expect(verdict.textContent).toContain('unreadable')
    expect(verdict.textContent).toContain('is not JSON')
    expect(within(card()).getByTestId('decision-happened').textContent).toContain('No agent was started')
  })

  it('a gated step still running says the verdict is not recorded yet, never a guess', async () => {
    const t = runTask({ id: FIX, workflow_id: WF, step_id: 'fix', state: 'RUNNING', started_at: at(6), dispatch: gateDispatch(), result_summary: null })
    render(<Run run={run(t)} />)
    await waitFor(() => expect(within(card()).getAllByRole('link', { name: 'review' }).length).toBeGreaterThan(0))
    const verdict = within(card()).getByTestId('decision-verdict')
    expect(within(verdict).getByRole('img', { name: /written when this step finishes/ })).toBeTruthy()
    expect(verdict.textContent).not.toContain('MERGE')
  })
})

// ---------------------------------------------------------------------------
// The Artifacts tab, through the real App
// ---------------------------------------------------------------------------

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

describe("a skipped step's Artifacts tab", () => {
  afterEach(() => {
    vi.unstubAllEnvs()
    window.history.replaceState(null, '', '/')
  })

  it('says why it holds only pr-title.txt and pr-body.md, and links to the Decision card', async () => {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    vi.doUnmock('../api')
    vi.doMock('../Agents', () => ({ AgentsScreen: () => null }))
    const t = mergeSkipped()
    const entry = (name: string, bytes: number) => ({ name, bytes, uri: gs(FIX, name), attempt_id: 'att_1', kind: 'text', content_type: 'text/plain', role: null })
    const routes: Record<string, unknown> = {
      [`/v1/tasks/${FIX}`]: { task: t },
      [`/v1/tasks/${FIX}/artifacts`]: {
        task_id: FIX,
        complete: true,
        attempt_id: 'att_1',
        artifact_bytes: 960,
        artifacts_skipped: [],
        artifacts: [entry('pr-title.txt', 60), entry('pr-body.md', 900)],
      },
      [`/v1/workflows/${WF}`]: workflowRead([implTask(), reviewTask(), t]),
    }
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const u = new URL(String(input), 'http://ui.test')
      const body = routes[u.pathname]
      if (body === undefined) return json({ message: 'not stubbed here' }, 404)
      return json(body)
    }) as unknown as typeof fetch
    window.location.hash = `#work/task/${FIX}/artifacts`
    const { App } = await import('../App')
    render(<App />)
    const note = await screen.findByTestId('skipped-artifacts')
    expect(note.textContent).toContain('No agent was started')
    expect(note.textContent).toContain('review verdict MERGE')
    expect(note.textContent).toContain('pr-title.txt')
    expect(note.textContent).toContain('pr-body.md')
    expect(note.textContent).toMatch(/copied from implement/)
    expect(within(note).getByRole('link', { name: 'Decision card' }).getAttribute('href')).toBe(`#work/task/${FIX}`)
  })
})
