/**
 * LANE U14, THE RUN PAGE WHILE IT RUNS (owner, 2026-10-04, after the
 * RUNNING-, CHECKING- and DONE-stage QA of run_7a37942a19aa4d2c80d1 at 1440
 * Light):
 *
 *   1  While RUNNING the page drew no step row and no link to the running
 *      agent. A live Steps card now sits under the status line: one row per
 *      workflow step with its name, state mark, elapsed, attempt n of N and
 *      "Open agent →" in EVERY state; a gated step reads "waiting on <step>".
 *   2  The plan's step said "starts at once" after approval; it now carries
 *      the step's live state and its link.
 *   3  Progress listed only the run's transitions; the steps' changes are
 *      interleaved, oldest first.
 *   4  "cost so far" read "not served per run" while a step reported cost: it
 *      sums the step tasks' recorded cost with its coverage, and is a dash with
 *      its reason when none report -- never $0.
 *   5  At CHECKING the PR card sat ~1,000px down and never linked the PR: it
 *      now LEADS, titled with the PR's number as a link, the failed checks
 *      named, and Overlaps and the Plan folded to one line each.
 *   6  At DONE the page never said whether the PR merged: the run does not
 *      serve it, so the card says "merge not reported" and names the field.
 *
 * MUTATIONS: drop the Steps card, or its link for a parked or queued step;
 * put "starts at once" back after approval; draw Progress from `history`
 * alone; print $0 for no cost; put the PR card back below the plan, or draw
 * the full green sha as its text -- each turns a case red.
 */
import { render, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { task } from './runfixture'

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
// Built at runtime: a 64-hex literal reads as a key to a credential scan.
const DIGEST = 'sha256:' + 'cd'.repeat(32)
const HEAD = '7138b1a' + '0f'.repeat(16) + '9'
const REF = 'bogdan-alexandrescu/SwarmCloud#72'
const ISSUE_URL = 'https://github.com/bogdan-alexandrescu/SwarmCloud/issues/72'
const PR_URL = 'https://github.com/bogdan-alexandrescu/SwarmCloud/pull/564'
const RUN_ID = 'run_7a37942a19aa4d2c80d1'
const WF = 'wf_5d0e2b7c41a94e3f8a61'
const FIX_WF = 'wf_fix1b2c3d4e5f60718293'

const PLAN = {
  summary: 'Draw the live steps on the run page.',
  requirements: ['The run page lists its steps'],
  overlaps: [{ ref: 'bogdan-alexandrescu/SwarmCloud#560', kind: 'pull_request', note: 'No action: touches another screen.' }],
  steps: [
    { step_id: 'impl', title: 'ui: the Steps card', prompt: 'Build it.', depends_on: [] },
    { step_id: 'docs', title: 'docs: the run page', prompt: 'Write it.', depends_on: [] },
    { step_id: 'wire', title: 'wire the two', prompt: 'Wire them.', depends_on: ['impl', 'docs'] },
  ],
}

function run(over: Record<string, unknown> = {}) {
  return {
    id: RUN_ID, tenant_id: 'eng', state: 'RUNNING', terminal: false,
    issue: { ref: REF, owner: 'bogdan-alexandrescu', repo: 'SwarmCloud', number: 72, url: ISSUE_URL,
      repository_url: 'https://github.com/bogdan-alexandrescu/SwarmCloud' },
    plan_approval: 'required', auto_merge: true, fix_rounds: 3, planner_task_id: 'task_planner72',
    plan: PLAN, plan_digest: DIGEST, plan_revision: 1, plan_edited_by: null, workflow_id: WF,
    created_by: 'operator@example.com', created_at: '2026-10-04T20:00:00Z', updated_at: '2026-10-04T20:20:00Z',
    approved_by: 'operator@example.com', approved_at: '2026-10-04T20:09:00Z', approved_digest: DIGEST,
    rejected_by: null, rejection_reason: null, error: null,
    history: [
      { at: '2026-10-04T20:00:00Z', from: null, to: 'PLANNING', by: 'operator@example.com' },
      { at: '2026-10-04T20:05:00Z', from: 'PLANNING', to: 'PLANNED', by: 'swarm-api' },
      { at: '2026-10-04T20:09:00Z', from: 'PLANNED', to: 'APPROVED', by: 'operator@example.com' },
      { at: '2026-10-04T20:10:00Z', from: 'APPROVED', to: 'RUNNING', by: 'swarm-api' },
    ],
    issue_read: { title: 'The run page shows no live step', state: 'open', labels: [], body: 'Body.', body_truncated: false,
      body_redacted: false, comments: 0, read_at: '2026-10-04T20:00:00Z', url: ISSUE_URL },
    issue_read_error: null,
    ...over,
  }
}

const cost = (usd: number) => ({ runner: { usage: { total_cost_usd: usd } } })

/** A RUNNING workflow: one step running, one parked, one queued behind them, and a gated review. */
function workflow(tasks = [
  task({ id: 'task_impl', state: 'RUNNING', workflow_id: WF, step_id: 'impl', created_at: '2026-10-04T20:10:00Z',
    started_at: '2026-10-04T20:11:00Z', updated_at: '2026-10-04T20:11:00Z', attempt_count: 1, max_attempts: 3 }),
  task({ id: 'task_docs', state: 'PARKED', workflow_id: WF, step_id: 'docs', created_at: '2026-10-04T20:10:00Z',
    started_at: null, updated_at: '2026-10-04T20:12:00Z', attempt_count: 1, max_attempts: 3, park_reason: 'QUOTA_EXHAUSTED' }),
  task({ id: 'task_wire', state: 'QUEUED', workflow_id: WF, step_id: 'wire', created_at: '2026-10-04T20:10:00Z',
    updated_at: '2026-10-04T20:10:00Z', attempt_count: 0, max_attempts: 3, depends_on: ['impl', 'docs'] }),
]) {
  return {
    workflow: {
      workflow_id: WF, tenant_id: 'eng', state: 'RUNNING', stored_state: 'RUNNING', state_source: 'derived',
      created_at: '2026-10-04T20:10:00Z', updated_at: '2026-10-04T20:12:00Z', submitted_by: 'operator@example.com',
      priority: 5, on_step_failure: 'stop', cancel_requested: false,
      steps: [
        { step_id: 'impl', runner_profile: 'claude-code', resource_class: 'standard', depends_on: [], input_from: {}, task_id: 'task_impl' },
        { step_id: 'docs', runner_profile: 'claude-code', resource_class: 'standard', depends_on: [], input_from: {}, task_id: 'task_docs' },
        { step_id: 'wire', runner_profile: 'claude-code', resource_class: 'standard', depends_on: ['impl', 'docs'], input_from: {}, task_id: 'task_wire' },
        { step_id: 'review', runner_profile: 'claude-code', resource_class: 'standard', depends_on: ['wire'], input_from: {}, task_id: null },
      ],
    },
    tasks,
  }
}

/** Each task's attempts, served at `/v1/tasks/<id>/attempts`; a task not named here 404s. */
let attemptsByTask: Record<string, unknown[]> = {}

/** One attempt row with the figures the cost read sums. */
function attempt(taskId: string, costUsd: number | null) {
  return {
    attempt_id: `att_${taskId}`, task_id: taskId, tenant_id: 'eng', generation: 1, lease_id: `lease_${taskId}`,
    backend: 'cloudrun', execution_name: null, created_at: '2026-10-04T20:11:00Z', started_at: '2026-10-04T20:11:00Z',
    completed_at: null, exit_code: null, error: null, peak_rss_bytes: null, peak_disk_bytes: null, oom_near_miss: false,
    checkpoints: [], input_tokens: null, output_tokens: null, cache_read_input_tokens: null,
    cache_creation_input_tokens: null, cost_usd: costUsd,
  }
}

function serve(body: unknown, workflows: Record<string, unknown> = {}) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url === `/v1/runs/${RUN_ID}`) return new Response(JSON.stringify({ run: body }), { status: 200, headers: JSON_HEADERS })
    for (const [id, rows] of Object.entries(attemptsByTask)) {
      if (url.startsWith(`/v1/tasks/${id}/attempts`)) {
        return new Response(JSON.stringify({ attempts: rows }), { status: 200, headers: JSON_HEADERS })
      }
    }
    for (const [id, wf] of Object.entries(workflows)) {
      if (url === `/v1/workflows/${id}`) return new Response(JSON.stringify(wf), { status: 200, headers: JSON_HEADERS })
    }
    return new Response(JSON.stringify({ code: 'not_found', message: url }), { status: 404, headers: JSON_HEADERS })
  }) as unknown as typeof fetch
}

async function mount(served: unknown, workflows: Record<string, unknown> = {}) {
  serve(served, workflows)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  const utils = render(<RunsScreen view={`run=${RUN_ID}`} go={vi.fn()} />)
  await waitFor(() => expect(utils.container.querySelector('.rn-plan')).not.toBeNull(), WAIT)
  return utils
}

afterEach(() => {
  vi.unstubAllEnvs()
  attemptsByTask = {}
})

const stepsCard = async (container: HTMLElement) =>
  waitFor(() => {
    const card = within(container).getByRole('region', { name: 'Steps' })
    expect(card.querySelectorAll('.rn-srow').length).toBeGreaterThan(0)
    return card
  }, WAIT)

const rowOf = (card: HTMLElement, step: string) =>
  [...card.querySelectorAll<HTMLElement>('.rn-srow')].find((r) => r.getAttribute('data-step') === step)!

describe('1: a running run draws a live Steps card under its status line', () => {
  it('draws one row per step, each with Open agent, its state, elapsed and attempt', async () => {
    const { container } = await mount(run(), { [WF]: workflow() })
    const card = await stepsCard(container)
    // Directly under the status line: nothing but the state line comes first.
    const main = container.querySelector('.rn-main')!
    const order = [...main.children]
    expect(order.indexOf(card)).toBe(order.indexOf(main.querySelector('.rn-state')!) + 1)

    // Each under the list that holds it (QA G2-17): a parked or queued task waits.
    for (const [step, id, mark, tab] of [['impl', 'task_impl', 'running', 'live'], ['docs', 'task_docs', 'parked', 'waiting'], ['wire', 'task_wire', 'queued', 'waiting']] as const) {
      const row = rowOf(card, step)
      expect(row, step).toBeDefined()
      const open = within(row).getByRole('link', { name: /Open agent/ })
      expect(open.getAttribute('href')).toBe(`/agents/${tab}/${id}`)
      expect(row.querySelector('[data-mark]')?.getAttribute('data-mark'), step).toBe(mark)
      expect(visible(row), step).toMatch(/attempt|no attempt yet/)
    }
    expect(visible(rowOf(card, 'impl'))).toContain('ui: the Steps card')
    expect(visible(rowOf(card, 'impl'))).toContain('attempt 1 of 3')
    expect(visible(rowOf(card, 'impl'))).toMatch(/elapsed \d/)
    expect(visible(rowOf(card, 'wire'))).toContain('no attempt yet · up to 3')
    expect(visible(rowOf(card, 'wire'))).toContain('not started')
  })

  it('draws a gated round with no task yet as "waiting on" its step, with no link', async () => {
    const { container } = await mount(run(), { [WF]: workflow() })
    const card = await stepsCard(container)
    const review = rowOf(card, 'review')
    expect(visible(review)).toContain('waiting on wire')
    expect(within(review).queryByRole('link')).toBeNull()
  })

  it('adds a CI fix round\'s steps as rows once the run names its workflow', async () => {
    const fix = {
      workflow: { ...workflow().workflow, workflow_id: FIX_WF, steps: [
        { step_id: 'ci-fix', runner_profile: 'claude-code', resource_class: 'standard', depends_on: [], input_from: {}, task_id: 'task_fix1' },
      ] },
      tasks: [task({ id: 'task_fix1', state: 'RUNNING', workflow_id: FIX_WF, step_id: 'ci-fix', started_at: '2026-10-04T22:00:00Z' })],
    }
    const { container } = await mount(run({ state: 'FIXING', ci_fix_round: 1, ci_fix_workflows: [FIX_WF],
      pull_request: { number: 564, url: PR_URL, head_sha: HEAD, checks: 'red' } }), { [WF]: workflow(), [FIX_WF]: fix })
    const card = await stepsCard(container)
    await waitFor(() => expect(rowOf(card, 'ci-fix')).toBeDefined(), WAIT)
    expect(visible(rowOf(card, 'ci-fix'))).toContain('fix round 1')
    expect(within(rowOf(card, 'ci-fix')).getByRole('link', { name: /Open agent/ }).getAttribute('href')).toBe('/agents/live/task_fix1')
  })

  it('says the workflow could not be read, rather than drawing no steps', async () => {
    const { container } = await mount(run())
    const card = await waitFor(() => {
      const c = within(container).getByRole('region', { name: 'Steps' })
      expect(visible(c)).toMatch(/could not be read/)
      return c
    }, WAIT)
    expect(card.querySelector('.rn-srow')).toBeNull()
  })

  it('draws no Steps card before the run has a workflow', async () => {
    const { container } = await mount(run({ state: 'PLANNED', workflow_id: null, approved_by: null, approved_at: null }))
    expect(within(container).queryByRole('region', { name: 'Steps' })).toBeNull()
  })
})

describe('2: after approval the plan\'s steps carry their live state and link', () => {
  it('replaces "starts at once" with the step\'s state and Open agent', async () => {
    const { container } = await mount(run(), { [WF]: workflow() })
    await stepsCard(container)
    const plan = within(container).getByRole('region', { name: 'The plan' })
    const first = plan.querySelectorAll('.rn-step').item(0) as HTMLElement
    await waitFor(() => expect(first.querySelector('[data-mark]')?.getAttribute('data-mark')).toBe('running'), WAIT)
    expect(visible(first)).not.toContain('starts at once')
    expect(within(first).getByRole('link', { name: /Open agent/ }).getAttribute('href')).toBe('/agents/live/task_impl')
    // A dependent step still says what it waits for, beside its state.
    const third = plan.querySelectorAll('.rn-step').item(2) as HTMLElement
    expect(visible(third)).toContain('after impl, docs')
    expect(third.querySelector('[data-mark]')?.getAttribute('data-mark')).toBe('queued')
  })

  it('keeps "starts at once" on a plan not yet approved', async () => {
    const { container } = await mount(run({ state: 'PLANNED', workflow_id: null, approved_by: null, approved_at: null }))
    const plan = within(container).getByRole('region', { name: 'The plan' })
    expect(visible(plan.querySelectorAll('.rn-step').item(0))).toContain('starts at once')
  })
})

describe('3: Progress interleaves the steps\' changes with the run\'s', () => {
  it('lists each step change at its time, oldest first, among the run transitions', async () => {
    const { container } = await mount(run(), { [WF]: workflow() })
    await stepsCard(container)
    const history = within(container).getByRole('region', { name: 'History' })
    await waitFor(() => expect(visible(history)).toContain('impl started'), WAIT)
    const rows = [...history.querySelectorAll('li')].map(visible)
    const at = (needle: string) => rows.findIndex((r) => r.includes(needle))
    expect(at('from approved')).toBeGreaterThan(-1)
    // RUNNING at 20:10, impl started 20:11, docs parked 20:12.
    expect(at('impl started')).toBeGreaterThan(at('from approved'))
    expect(at('docs parked')).toBeGreaterThan(at('impl started'))
    expect(at('docs parked')).toBe(rows.length - 1)
    // Not the Steps card again: no agent links in Progress.
    expect(within(history).queryByRole('link', { name: /Open agent/ })).toBeNull()
  })
})

describe('4: cost so far sums the steps\' recorded cost with its coverage', () => {
  const costFact = (container: HTMLElement) =>
    [...within(container).getByRole('region', { name: 'Chosen at submission' }).querySelectorAll('.ctl-fact')]
      .find((li) => visible(li.querySelector('b')) === 'cost so far')!

  it('sums the reporting steps and says how many report', async () => {
    const done = workflow([
      task({ id: 'task_impl', state: 'SUCCEEDED', workflow_id: WF, step_id: 'impl', result_summary: cost(0.3) }),
      task({ id: 'task_docs', state: 'SUCCEEDED', workflow_id: WF, step_id: 'docs', result_summary: cost(0.12) }),
      task({ id: 'task_wire', state: 'RUNNING', workflow_id: WF, step_id: 'wire' }),
    ])
    const { container } = await mount(run(), { [WF]: done })
    await waitFor(() => expect(visible(costFact(container))).toContain('$0.42 · 2 of 4 steps reporting'), WAIT)
  })

  it('counts a RUNNING step through its attempts, which is how it reports before it finishes', async () => {
    // The stage the owner measured: one step running, its cost only on its
    // attempt (no result_summary yet). Results-only summing reads a dash here.
    attemptsByTask = { task_impl: [attempt('task_impl', 0.42)], task_docs: [attempt('task_docs', null)] }
    const { container } = await mount(run(), { [WF]: workflow() })
    await waitFor(() => expect(visible(costFact(container))).toContain('$0.42 · 1 of 4 steps reporting'), WAIT)
    expect(costFact(container).querySelector('[title]')!.getAttribute('title')).toMatch(/attempts/)
  })

  it('is a dash with its reason when no step reports, never $0', async () => {
    const { container } = await mount(run(), { [WF]: workflow() })
    await stepsCard(container)
    const fact = costFact(container)
    const dash = fact.querySelector('.c-dash')
    expect(dash).not.toBeNull()
    expect(dash!.getAttribute('title')).toMatch(/no step has reported/i)
    expect(visible(fact)).not.toMatch(/\$0/)
    expect(visible(fact)).not.toContain('not served per run')
  })
})

describe('5: at CHECKING the pull request card leads, and the plan folds', () => {
  const excerpt = `CI is red at ${HEAD.slice(0, 12)}: unit (python), ui (typecheck)\n\n## unit (python) (failure)\nassert 1 == 2\n\n## ui (typecheck) (failure)\nTS2322`
  const checking = (over: Record<string, unknown> = {}) => run({
    state: 'CHECKING', ci_fix_round: 1,
    pull_request: { number: 564, url: PR_URL, head_sha: HEAD, checks: 'red' },
    failure_excerpt: excerpt,
    history: [...(run().history as unknown[]),
      { at: '2026-10-04T21:00:00Z', from: 'RUNNING', to: 'CHECKING', by: 'swarm-api' }],
    ...over,
  })

  it('leads the main column, titled with the PR number as a link', async () => {
    const { container } = await mount(checking(), { [WF]: workflow() })
    const ci = await waitFor(() => {
      const el = container.querySelector<HTMLElement>('.rn-ci')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    const main = container.querySelector('.rn-main')!
    const order = [...main.children]
    expect(order.indexOf(ci)).toBe(order.indexOf(main.querySelector('.rn-state')!) + 1)
    const head = ci.querySelector('.c-card-h')!
    expect(within(head as HTMLElement).getByRole('link', { name: /#564/ }).getAttribute('href')).toBe(PR_URL)
    // No bare dash for the title the run does not serve (QA G2-06): the number alone.
    expect(head.querySelector('.c-dash')).toBeNull()
    expect(visible(head)).toBe('Pull request #564')
    // The head sha, short, with the whole one in its title.
    const sha = ci.querySelector(`code[title="${HEAD}"]`)
    expect(visible(sha)).toBe(HEAD.slice(0, 7))
  })

  it('names each failed check and links the checks on GitHub', async () => {
    const { container } = await mount(checking(), { [WF]: workflow() })
    const ci = await waitFor(() => container.querySelector<HTMLElement>('.rn-ci')!, WAIT)
    const failed = [...ci.querySelectorAll('.rn-failed-check')].map(visible)
    expect(failed).toEqual(['unit (python)', 'ui (typecheck)'])
    expect(within(ci).getByRole('link', { name: /checks on GitHub/ }).getAttribute('href')).toBe(`${PR_URL}/checks`)
    expect(visible(ci)).toContain('failed 2')
    expect(visible(ci)).toContain('fix round 1 of 3')
  })

  it('does not call a previous round\'s red checks the current ones', async () => {
    const moved = 'aa11bb2' + '0e'.repeat(16) + '1'
    const { container } = await mount(checking({ pull_request: { number: 564, url: PR_URL, head_sha: moved, checks: 'pending' } }), { [WF]: workflow() })
    const ci = await waitFor(() => container.querySelector<HTMLElement>('.rn-ci')!, WAIT)
    expect(visible(ci)).toMatch(/red at 7138b1a0f0f0, before the head moved/)
    expect(visible(ci)).not.toContain('failed 2')
  })

  it('folds Overlaps and the Plan to one line each once the plan is approved', async () => {
    const { container } = await mount(checking(), { [WF]: workflow() })
    await waitFor(() => expect(container.querySelector('.rn-ci')).not.toBeNull(), WAIT)
    const plan = within(container).getByRole('region', { name: 'The plan' })
    const folds = plan.querySelector(':scope > details.rn-fold') as HTMLDetailsElement
    expect(folds).not.toBeNull()
    expect(folds.open).toBe(false)
    expect(visible(folds.querySelector('summary'))).toMatch(/The plan .*3 planned steps \+ review \+ fix/)
    const overlaps = container.querySelector('.rn-overlaps > details.rn-fold') as HTMLDetailsElement
    expect(overlaps).not.toBeNull()
    expect(overlaps.open).toBe(false)
    expect(visible(overlaps.querySelector('summary'))).toMatch(/1 checked · none need action/)
    // The PR card comes before both.
    const ci = container.querySelector('.rn-ci')!
    expect(ci.compareDocumentPosition(plan) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('leaves the plan open while it waits for approval', async () => {
    const { container } = await mount(run({ state: 'PLANNED', workflow_id: null, approved_by: null, approved_at: null }))
    const plan = within(container).getByRole('region', { name: 'The plan' })
    expect(plan.querySelector('details.rn-fold')).toBeNull()
  })
})

describe('6: at DONE the card says what is known about the merge and the issue', () => {
  it('says "merge not reported", draws the green sha short and links the issue', async () => {
    const { container } = await mount(run({
      state: 'DONE', terminal: true, green_sha: HEAD, requirements_met: true,
      pull_request: { number: 564, url: PR_URL, head_sha: HEAD, checks: 'green' },
    }), { [WF]: workflow() })
    const ci = await waitFor(() => container.querySelector<HTMLElement>('.rn-ci')!, WAIT)
    const fact = (label: string) => [...ci.querySelectorAll('.ctl-fact')].find((li) => visible(li.querySelector('b')) === label)!
    expect(visible(fact('merge'))).toContain('merge not reported')
    expect(fact('merge').querySelector('[title]')?.getAttribute('title')).toMatch(/pull_request\.merged/)
    const green = fact('green at').querySelector('code')!
    expect(visible(green)).toBe(HEAD.slice(0, 7))
    expect(green.getAttribute('title')).toBe(HEAD)
    expect(within(fact('issue') as HTMLElement).getByRole('link', { name: /#72/ }).getAttribute('href')).toBe(ISSUE_URL)
    expect(visible(fact('issue'))).toMatch(/state not served/)
    expect(visible(fact('keyword'))).toContain('Closes #72')
    expect(visible(container.querySelector('.rn-state'))).toMatch(/merge is not reported/)
  })

  it('says the pull request merged and ended a run that closed without a green sha, not that the workflow succeeded', async () => {
    const { container } = await mount(run({
      state: 'DONE', terminal: true, green_sha: null,
      pull_request: { number: 564, url: PR_URL, head_sha: HEAD, checks: 'pending' },
    }), { [WF]: workflow() })
    const line = visible(container.querySelector('.rn-state'))
    // #503: a closed PR fails the run and a green one records its sha, so a
    // DONE run with no green sha was ended by the PR merging.
    expect(line).toMatch(/pull request merged, which ended the run/)
    expect(line).not.toMatch(/not reported/)
    expect(line).not.toMatch(/workflow succeeded/)
  })
})
