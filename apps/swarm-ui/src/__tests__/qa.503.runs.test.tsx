/**
 * #503, THE RUN PAGE'S PULL REQUEST CARD (QA of run_7a37942a19aa4d2c80d1, #72,
 * PR #564, 2026-10-04):
 *
 *   * CHECKING read "pending at 7138b1a" with no counts. The run serves one
 *     aggregate reading (`pull_request.checks`) and no per-check counts; what
 *     IS served is the failures its excerpt names and, while a merge step is
 *     parked on CI_PENDING, the checks it waits for (`merge_wait.pending`).
 *     The card counts both and says the rest are not served.
 *   * DONE never said whether the PR merged or the issue closed. The run does
 *     not serve `pull_request.merged`, but the merge step's result does, and a
 *     DONE run with no green sha was ended by GitHub reporting it merged (a
 *     closed PR fails the run; a green one records its sha). `issue_closed`
 *     says the run closed the issue itself; the merge step's `issues_closed`
 *     and the merged PR's keyword say what the merge did.
 *
 * MUTATIONS: make `mergeKnown` ignore the merge step's result, or the
 * no-green-sha DONE, or `IssueFact` ignore `issue_closed` -- a case below
 * turns red. A green DONE run with no merge step still says "merge not
 * reported" (qa.u14 6), so the inference is no wider than the state machine.
 */
import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { task } from './runfixture'

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
/** A fact's value: its text without its `<b>` label. */
const said = (li: Element | null) => visible(li).slice(visible(li?.querySelector('b') ?? null).length).trim()
// Built at runtime: a long hex literal reads as a key to a credential scan.
const DIGEST = 'sha256:' + 'cd'.repeat(32)
const HEAD = '7138b1a' + '0f'.repeat(16) + '9'
const COMMIT = 'e4a91c0' + '1b'.repeat(16) + '5'
const REF = 'bogdan-alexandrescu/SwarmCloud#72'
const ISSUE_URL = 'https://github.com/bogdan-alexandrescu/SwarmCloud/issues/72'
const PR_URL = 'https://github.com/bogdan-alexandrescu/SwarmCloud/pull/564'
const RUN_ID = 'run_7a37942a19aa4d2c80d1'
const WF = 'wf_5d0e2b7c41a94e3f8a61'

function run(over: Record<string, unknown> = {}) {
  return {
    id: RUN_ID, tenant_id: 'eng', state: 'CHECKING', terminal: false,
    issue: { ref: REF, owner: 'bogdan-alexandrescu', repo: 'SwarmCloud', number: 72, url: ISSUE_URL,
      repository_url: 'https://github.com/bogdan-alexandrescu/SwarmCloud' },
    plan_approval: 'required', auto_merge: false, fix_rounds: 3, planner_task_id: 'task_planner72',
    plan: { summary: 'Draw it.', requirements: ['It is drawn'], overlaps: [],
      steps: [{ step_id: 'impl', title: 'ui', prompt: 'Build it.', depends_on: [] }] },
    plan_digest: DIGEST, plan_revision: 1, plan_edited_by: null, workflow_id: WF,
    created_by: 'operator@example.com', created_at: '2026-10-04T20:00:00Z', updated_at: '2026-10-04T22:40:00Z',
    approved_by: 'operator@example.com', approved_at: '2026-10-04T20:09:00Z', approved_digest: DIGEST,
    rejected_by: null, rejection_reason: null, error: null,
    history: [{ at: '2026-10-04T20:00:00Z', from: null, to: 'PLANNING', by: 'operator@example.com' }],
    issue_read: null, issue_read_error: null,
    pull_request: { number: 564, url: PR_URL, head_sha: HEAD, checks: 'pending' },
    ...over,
  }
}

/** The run's workflow: the build step, then a merge step in whatever state the case needs. */
function workflow(merge: Record<string, unknown> | null) {
  const impl = task({ id: 'task_impl', state: 'SUCCEEDED', workflow_id: WF, step_id: 'impl',
    result_summary: { git: { pull_request: { number: 564, url: PR_URL, state: 'open', created: true }, pushed_head: HEAD } } })
  const tasks = [impl]
  const steps: Record<string, unknown>[] = [
    { step_id: 'impl', runner_profile: 'claude-code', resource_class: 'standard', depends_on: [], input_from: {}, task_id: 'task_impl' },
  ]
  if (merge !== null) {
    tasks.push(task({ id: 'task_merge', workflow_id: WF, step_id: 'merge', runner_profile: 'merge', depends_on: ['impl'], ...merge }))
    steps.push({ step_id: 'merge', runner_profile: 'merge', resource_class: 'standard', depends_on: ['impl'], input_from: {}, task_id: 'task_merge' })
  }
  return {
    workflow: {
      workflow_id: WF, tenant_id: 'eng', state: 'RUNNING', stored_state: 'RUNNING', state_source: 'derived',
      created_at: '2026-10-04T20:10:00Z', updated_at: '2026-10-04T22:40:00Z', submitted_by: 'operator@example.com',
      priority: 5, on_step_failure: 'stop', cancel_requested: false, steps,
    },
    tasks,
  }
}

const WAITING = {
  state: 'PARKED', park_reason: 'CI_PENDING',
  metadata: { merge_wait: { code: 'ci_pending', pending: ['unit', 'lint', 'build images'], head: HEAD, first_parked_at: '2026-10-04T22:30:00Z' } },
}
const MERGED = {
  state: 'SUCCEEDED',
  result_summary: { merge: { merged_by_this_task: true, merge_commit: COMMIT, issues_closed: [72], pull_request: 564, repository: 'bogdan-alexandrescu/SwarmCloud' } },
}

function serve(body: unknown, wf: unknown) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url === `/v1/runs/${RUN_ID}`) return new Response(JSON.stringify({ run: body }), { status: 200, headers: JSON_HEADERS })
    if (url === `/v1/workflows/${WF}`) return new Response(JSON.stringify(wf), { status: 200, headers: JSON_HEADERS })
    return new Response(JSON.stringify({ code: 'not_found', message: url }), { status: 404, headers: JSON_HEADERS })
  }) as unknown as typeof fetch
}

async function mount(served: unknown, wf: unknown = workflow(null)) {
  serve(served, wf)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  const utils = render(<RunsScreen view={`run=${RUN_ID}`} go={vi.fn()} />)
  await waitFor(() => expect(utils.container.querySelector('.rn-plan')).not.toBeNull(), WAIT)
  const ci = await waitFor(() => utils.container.querySelector<HTMLElement>('.rn-ci')!, WAIT)
  const fact = (label: string) => [...ci.querySelectorAll('.ctl-fact')].find((li) => visible(li.querySelector('b')).startsWith(label)) ?? null
  return { ...utils, ci, fact }
}

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('#503: the check counts the run serves', () => {
  it('counts and names the checks a parked merge step waits for, and says the rest are not served', async () => {
    const { fact } = await mount(run(), workflow(WAITING))
    await waitFor(() => expect(said(fact('by check'))).toContain('pending 3'), WAIT)
    const counts = fact('by check')!
    expect(visible(counts)).toContain('the other counts not served')
    expect(counts.querySelector('[title]')?.getAttribute('title')).toMatch(/passed or were skipped.*pull_request\.checks/)
    const pending = fact('pending at')!
    expect(visible(pending.querySelector('code'))).toBe(HEAD.slice(0, 7))
    expect([...pending.querySelectorAll('.rn-pending-check')].map(visible)).toEqual(['unit', 'lint', 'build images'])
    // Pending is not a failure: it is not drawn in the failure's red class.
    expect(pending.querySelector('.rn-failed-check')).toBeNull()
    expect(said(fact('merge'))).toBe('open · the merge step waits for CI')
  })

  it('counts the failures the excerpt names at the head, beside the pending ones', async () => {
    const excerpt = `CI is red at ${HEAD.slice(0, 12)}: unit, lint\n`
    const { fact } = await mount(run({ pull_request: { number: 564, url: PR_URL, head_sha: HEAD, checks: 'red' }, failure_excerpt: excerpt }))
    const counts = said(fact('by check'))
    expect(counts).toBe('failed 2 · the other counts not served')
    expect(counts).not.toContain('pending')
  })
})

describe('#503: at DONE the card says whether the PR merged and the issue closed', () => {
  it("reads the merge step's result: merged, its commit, and the issue it closed", async () => {
    const { container, fact } = await mount(
      run({ state: 'DONE', terminal: true, green_sha: HEAD, auto_merge: true, requirements_met: true,
        pull_request: { number: 564, url: PR_URL, head_sha: HEAD, checks: 'green' } }),
      workflow(MERGED),
    )
    await waitFor(() => expect(fact('merge')!.getAttribute('data-merge')).toBe('merged'), WAIT)
    expect(said(fact('merge'))).toBe(`merged · ${COMMIT.slice(0, 7)} · by the merge step`)
    expect(fact('merge')!.classList.contains('is-absent')).toBe(false)
    expect(said(fact('issue'))).toBe('#72 · closed by the merge')
    expect(visible(container.querySelector('.rn-state'))).toContain('The pull request merged, with every required check green.')
    expect(visible(container.querySelector('.rn-state'))).not.toMatch(/not reported/)
  })

  it('says the issue stayed open when the merge step closed others but not this one', async () => {
    const left = { ...MERGED, result_summary: { merge: { ...MERGED.result_summary.merge, issues_closed: [80] } } }
    const { fact } = await mount(run({ state: 'DONE', terminal: true, green_sha: HEAD, auto_merge: true }), workflow(left))
    await waitFor(() => expect(said(fact('issue'))).toBe('#72 · not closed by the merge'), WAIT)
  })

  it('says not merged, with the code, when the merge step refused', async () => {
    const refused = { state: 'FAILED', end_cause: 'merge_refused',
      result_summary: { merge: { refusal: { code: 'review_not_merge', message: 'the review did not say MERGE' } } } }
    const { fact } = await mount(run({ state: 'FAILED', terminal: true, green_sha: HEAD, auto_merge: true }), workflow(refused))
    await waitFor(() => expect(fact('merge')!.getAttribute('data-merge')).toBe('refused'), WAIT)
    expect(said(fact('merge'))).toBe('not merged · the merge step refused · review_not_merge')
  })

  it('reads a DONE run with no green sha as merged, and its Closes keyword as closing the issue', async () => {
    const { container, fact } = await mount(run({ state: 'DONE', terminal: true, green_sha: null, requirements_met: true }))
    expect(fact('merge')!.getAttribute('data-merge')).toBe('merged')
    expect(said(fact('merge'))).toBe('merged · commit and time not served')
    expect(fact('merge')!.querySelector('[title]')?.getAttribute('title')).toMatch(/closed pull request fails the run/)
    expect(said(fact('issue'))).toBe('#72 · closed by the merge · Closes #72')
    expect(visible(container.querySelector('.rn-state'))).toContain('The pull request merged, which ended the run before its checks were green.')
  })

  it('says a part-of pull request left the issue open', async () => {
    const { fact } = await mount(run({ state: 'DONE', terminal: true, green_sha: null, requirements_met: false }))
    expect(said(fact('issue'))).toBe('#72 · left open · part of #72')
  })

  it('says the run closed the issue itself when issue_closed is served', async () => {
    const { fact } = await mount(run({
      state: 'DONE', terminal: true, green_sha: null, pull_request: null, outcome: 'already_on_main',
      requirements_met: true, issue_closed: true,
    }))
    expect(said(fact('issue'))).toBe('#72 · closed by this run')
    expect(fact('merge')).toBeNull()
  })

  it('still says "merge not reported" for a green DONE run with no merge step', async () => {
    const { fact } = await mount(run({ state: 'DONE', terminal: true, green_sha: HEAD,
      pull_request: { number: 564, url: PR_URL, head_sha: HEAD, checks: 'green' } }))
    expect(said(fact('merge'))).toBe('merge not reported')
    expect(fact('merge')!.querySelector('[title]')?.getAttribute('title')).toMatch(/pull_request\.merged/)
    expect(visible(fact('issue'))).toMatch(/state not served/)
  })
})

/**
 * THE FIELDS SWARM-API NOW SERVES (#503 boxes 5985235915 and 5985301167):
 * `pull_request.check_counts`, `check_list`, `ci_url`, `merged`, `merged_at`.
 *
 * MUTATIONS: drop the served counts from the "by check" fact, the CI run link,
 * or `mergeKnown`'s `pull_request.merged` read -- a case below turns red.
 */
describe('#503: the counts, the checks and the merge swarm-api serves', () => {
  const RUN_URL = 'https://github.com/bogdan-alexandrescu/SwarmCloud/actions/runs/' + '1'.repeat(10)
  const checkList = [
    ...Array.from({ length: 7 }, (_, i) => ({ name: `pending ${i}`, state: 'pending', url: i === 0 ? `${RUN_URL}/job/7` : null })),
    ...Array.from({ length: 5 }, (_, i) => ({ name: `passed ${i}`, state: 'passed', url: `${RUN_URL}/job/${i}` })),
    ...Array.from({ length: 3 }, (_, i) => ({ name: `skipped ${i}`, state: 'skipped', url: null })),
  ]
  const checking = (over: Record<string, unknown> = {}) => run({
    pull_request: {
      number: 564, url: PR_URL, head_sha: HEAD, checks: 'pending', merged: false, merged_at: null,
      check_counts: { passed: 5, failed: 0, pending: 7, skipped: 3 }, check_list: checkList,
      check_list_truncated: false, ci_url: `${RUN_URL}/job/7`, ...over,
    },
  })

  it('prints the three counts, a folded list linking each check, and a CI run link at CHECKING', async () => {
    const { ci, fact } = await mount(checking())
    await waitFor(() => expect(said(fact('by check'))).toBe('5 passed · 7 pending · 3 skipped'), WAIT)
    const list = fact('each check')!
    expect(list.querySelector('details')).not.toBeNull()
    expect(list.querySelector('details')!.hasAttribute('open')).toBe(false)
    expect(visible(list.querySelector('summary'))).toBe('15 checks')
    const links = [...list.querySelectorAll('a')]
    expect(links.map((a) => a.getAttribute('href'))).toContain(`${RUN_URL}/job/0`)
    expect(links).toHaveLength(6)
    const run = [...ci.querySelectorAll('a')].find((a) => visible(a) === 'CI run →')
    expect(run?.getAttribute('href')).toBe(`${RUN_URL}/job/7`)
    expect(said(fact('merge'))).toBe('open · not merged at the last read')
  })

  it('says the list was cut when the server truncated it', async () => {
    const { fact } = await mount(checking({ check_list_truncated: true }))
    await waitFor(() => expect(visible(fact('each check')!.querySelector('summary'))).toBe('15 checks shown · the rest not kept'), WAIT)
  })

  it('says merged with its age and UTC time at DONE when merged is true, with no merge step', async () => {
    const { container, fact } = await mount(run({
      state: 'DONE', terminal: true, green_sha: HEAD,
      pull_request: { number: 564, url: PR_URL, head_sha: HEAD, checks: 'green', merged: true,
        merged_at: '2026-10-04T22:41:07Z', check_counts: { passed: 12, failed: 0, pending: 0, skipped: 3 } },
    }))
    await waitFor(() => expect(fact('merge')!.getAttribute('data-merge')).toBe('merged'), WAIT)
    expect(said(fact('merge'))).toMatch(/^merged \S+ ago$/)
    expect(fact('merge')!.querySelector('[title="2026-10-04 22:41:07 UTC"]')).not.toBeNull()
    expect(visible(fact('merge'))).not.toMatch(/not reported|not served/)
    expect(visible(container.querySelector('.rn-state'))).toContain('The pull request merged, with every required check green.')
  })

  it('says not merged at DONE when merged is false', async () => {
    const { fact } = await mount(run({
      state: 'DONE', terminal: true, green_sha: HEAD,
      pull_request: { number: 564, url: PR_URL, head_sha: HEAD, checks: 'green', merged: false, merged_at: null },
    }))
    await waitFor(() => expect(said(fact('merge'))).toBe('not merged'), WAIT)
  })

  it('still says "merge not reported" where merged is null', async () => {
    const { fact } = await mount(run({
      state: 'DONE', terminal: true, green_sha: HEAD,
      pull_request: { number: 564, url: PR_URL, head_sha: HEAD, checks: 'green', merged: null, merged_at: null,
        check_counts: null, check_list: null, check_list_truncated: null, ci_url: null },
    }))
    await waitFor(() => expect(said(fact('merge'))).toBe('merge not reported'), WAIT)
    expect(said(fact('by check'))).toBe('per-check counts not served')
    expect(fact('each check')).toBeNull()
  })
})
