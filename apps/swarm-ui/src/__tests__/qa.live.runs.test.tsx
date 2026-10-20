/**
 * LIVE QA, WORK › RUNS (section work-runs-timeline, report of 2026-10-11).
 * Each case states the property the finding broke, not the markup:
 *
 *   RT-2   the plan keeps to its card: the fold's body is one track of the
 *          card's width, and a long estimate chip wraps inside it.
 *   RT-3   a plan step reads the state of the workflow step it ran as
 *          (`impl-<id>`), not "the workflow read has no such step".
 *   RT-4   a CHECKING run with no CI read past CI_UNREAD_WARN_MS warns.
 *   RT-5   a step that ran no agent reads "skipped · <why>", not succeeded.
 *   RT-6   the list's chip and search are its address, and are read back.
 *   RT-8   a finished run's CI facts are final, not "not read yet".
 *   RT-10/12/13/14/16/17/18   the smaller ones, each by its rule.
 *
 * MUTATIONS: drop `.rn-fold-b`'s `grid-template-columns` in runs.css; look
 * plan steps up by `step_id` alone; return null from `stepSkip`; drop the
 * `useEffect` that writes `?state=`/`?q=`; return 'not read yet' from
 * `ChecksUnread` -- each turns a case red.
 */
import { fireEvent, render, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import COMPONENTS_CSS from '../styles/components.css?raw'
import INTAKE_CSS from '../styles/intake.css?raw'
import RUNS_CSS from '../styles/runs.css?raw'
import NAMES_CSS from '../styles/names.css?raw'
import STYLES from '../styles.css?raw'
import { cascade, type CascadeEnv } from './cssgate'
import { task } from './runfixture'
import type { IssueRun, Task } from '../types'
import type { StepRow } from '../RunSteps'

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
// Built at runtime: a 64-hex literal reads as a key to a credential scan.
const DIGEST = 'sha256:' + 'ab'.repeat(32)
const OWNER = 'example-org'
const REF = `${OWNER}/app#940`
const ISSUE_URL = `https://github.com/${OWNER}/app/issues/940`
const RUN_ID = 'run_live0a1b2c3d4e5f6071'
const WF = 'wf_live0a1b2c3d4e5f6071'
const HOUR = 3_600_000

const SHEETS = [STYLES, COMPONENTS_CSS, NAMES_CSS, INTAKE_CSS, RUNS_CSS].join('\n')
const DESKTOP: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 400 }
const painted = (el: Element, prop: string, env: CascadeEnv, pseudo: string | null = null) => {
  const r = cascade(SHEETS, el, prop, env, pseudo)
  expect(r.unsupported).toEqual([])
  return r.winner?.value ?? null
}

const PLAN = {
  summary: 'Write the contract, then check the reads.',
  mode: 'workflow',
  estimate: 'about three hours across six steps, the contract first and the read checks after it, '.repeat(3),
  requirements: ['The contract is written'],
  steps: [
    { step_id: 'contract', title: 'Write docs/contracts', prompt: 'Write it.', depends_on: [] },
    { step_id: 'api-reads-check', title: 'Check the reads', prompt: 'Check them.', depends_on: ['contract'] },
  ],
}

function run(over: Record<string, unknown> = {}) {
  return {
    id: RUN_ID, tenant_id: 'eng', state: 'DONE', terminal: true,
    issue: { ref: REF, owner: OWNER, repo: 'app', number: 940, url: ISSUE_URL, repository_url: `https://github.com/${OWNER}/app` },
    plan_approval: 'required', auto_merge: false, fix_rounds: 2, planner_task_id: 'task_planner940',
    plan: PLAN, plan_digest: DIGEST, plan_revision: 1, plan_edited_by: null, workflow_id: WF,
    created_by: 'operator@example.com', created_at: '2026-10-10T20:00:00Z', updated_at: '2026-10-10T21:00:00Z',
    approved_by: 'operator@example.com', approved_at: '2026-10-10T20:09:00Z', approved_digest: DIGEST,
    rejected_by: null, rejection_reason: null, error: null, green_sha: null, requirements_met: null,
    pull_request: null,
    history: [
      { at: '2026-10-10T20:00:00Z', from: null, to: 'PLANNING', by: 'operator@example.com' },
      { at: '2026-10-10T20:05:00Z', from: 'PLANNING', to: 'PLANNED', by: 'swarm-api' },
    ],
    issue_read: { title: 'Write the contract', state: 'open', labels: [], body: '## Description\n\n**Bold** text.',
      body_truncated: false, body_redacted: false, comments: 0, read_at: '2026-10-10T20:00:00Z', url: ISSUE_URL },
    issue_read_error: null,
    ...over,
  }
}

const stepTask = (id: string, stepId: string, over: Partial<Task> = {}) =>
  task({ id, workflow_id: WF, step_id: stepId, state: 'SUCCEEDED', started_at: '2026-10-10T20:10:00Z', completed_at: '2026-10-10T20:20:00Z', ...over })

function workflowRead(tasks: Task[]) {
  return {
    workflow: {
      workflow_id: WF, tenant_id: 'eng', state: 'SUCCEEDED', stored_state: 'SUCCEEDED', state_source: 'derived',
      created_at: '2026-10-10T20:09:00Z', updated_at: '2026-10-10T21:00:00Z',
      steps: tasks.map((t) => ({ step_id: t.step_id, task_id: t.id, depends_on: [] })),
    },
    tasks,
  }
}

function serve(routes: Record<string, unknown>) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input).split('?')[0]!
    if (url in routes) return new Response(JSON.stringify(routes[url]), { status: 200, headers: JSON_HEADERS })
    return new Response(JSON.stringify({ code: 'not_found', message: url }), { status: 404, headers: JSON_HEADERS })
  }) as unknown as typeof fetch
}

async function mountRun(r: Record<string, unknown>, extra: Record<string, unknown> = {}) {
  serve({ [`/v1/runs/${RUN_ID}`]: { run: r }, ...extra })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  const utils = render(<RunsScreen view={`run=${RUN_ID}`} go={vi.fn()} />)
  await waitFor(() => expect(utils.container.querySelector('.rn-plan, .rn-history')).not.toBeNull(), WAIT)
  return utils
}

async function mountList(runs: unknown[], view: string | null = null) {
  serve({ '/v1/runs': { runs, next_page_token: null, tenant_id: 'eng' } })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  const utils = render(<RunsScreen view={view} go={vi.fn()} />)
  await waitFor(() => expect(utils.container.querySelector('.rn-list')).not.toBeNull(), WAIT)
  return utils
}

afterEach(() => {
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

describe('RT-2: the plan keeps to its card, at 1440 and on a phone', () => {
  it('lays the fold out in one track of the card\'s width, and lets every block of the plan wrap', async () => {
    const { container } = await mountRun(run())
    const body = container.querySelector('.rn-plan .rn-fold-b')
    expect(body, 'an approved plan is folded').not.toBeNull()
    for (const env of [DESKTOP, PHONE]) {
      expect(painted(body!, 'grid-template-columns', env), 'an auto track grows to its min-content').toBe('minmax(0, 1fr)')
      for (const sel of ['.rn-summary', '.rn-plan-facts', '.rn-reqs-wrap', '.rn-raw']) {
        const el = container.querySelector(`.rn-plan ${sel}`)!
        expect(el, sel).not.toBeNull()
        expect(painted(el, 'min-width', env), sel).toBe('0')
      }
      // The estimate chip is `nowrap` everywhere else; here it is prose.
      const chip = [...container.querySelectorAll('.rn-plan-facts > .c-chip')].find((c) => /estimate/.test(c.textContent ?? ''))!
      expect(painted(chip, 'white-space', env)).toBe('normal')
      expect(painted(container.querySelector('.rn-raw-json')!, 'white-space', env)).toBe('pre-wrap')
    }
  })
})

describe('RT-3: a plan step reads the workflow step it ran as', () => {
  it('maps `contract` to `impl-contract`, then falls back to the plan\'s own name', async () => {
    const { planStepRow } = await import('../RunSteps')
    const row = (stepId: string, round = 0) => ({ key: `${round}:${stepId}`, round, stepId }) as unknown as StepRow
    const rows = [row('impl-contract'), row('api-reads-check'), row('impl-contract', 1)]
    expect(planStepRow(rows, 'contract')?.key).toBe('0:impl-contract')
    expect(planStepRow(rows, 'api-reads-check')?.key).toBe('0:api-reads-check')
    expect(planStepRow(rows, 'missing')).toBeNull()
  })

  it('draws each planned step\'s state, never "the workflow read has no such step"', async () => {
    const tasks = [stepTask('task_c', 'impl-contract'), stepTask('task_r', 'impl-api-reads-check')]
    const { container } = await mountRun(run(), { [`/v1/workflows/${WF}`]: workflowRead(tasks) })
    await waitFor(() => expect(container.querySelectorAll('.rn-srow').length).toBe(2), WAIT)
    const plan = container.querySelector('.rn-plan')!
    expect(visible(plan)).not.toMatch(/no such step/)
    expect(plan.querySelectorAll('.rn-step-live').length).toBe(2)
    // The Steps card names them by the plan's titles, through the prefix too.
    expect(visible(container.querySelector('.rn-srow[data-step="impl-contract"]'))).toContain('Write docs/contracts')
  })
})

describe('RT-5: a step that ran no agent is skipped, not succeeded', () => {
  it('reads `result_summary.skipped` and the verdict gate', async () => {
    const { stepSkip } = await import('../RunSteps')
    expect(stepSkip(task({ state: 'SUCCEEDED', result_summary: { skipped: { reason: 'nothing to change' } } }))).toBe('nothing to change')
    expect(stepSkip(task({ state: 'SUCCEEDED', result_summary: { verdict_gate: { agent_ran: false, verdict: 'MERGE' } } }))).toBe('by verdict MERGE')
    expect(stepSkip(task({ state: 'SUCCEEDED', result_summary: { verdict_gate: { agent_ran: true } } }))).toBeNull()
    expect(stepSkip(task({ state: 'FAILED', result_summary: { skipped: { reason: 'x' } } })), 'only a success can be a skip').toBeNull()
  })

  it('draws the review row as skipped, with no elapsed time', async () => {
    const tasks = [
      stepTask('task_c', 'impl-contract'),
      stepTask('task_rv', 'review', { result_summary: { skipped: { reason: 'nothing to change' } } }),
    ]
    const { container } = await mountRun(run(), { [`/v1/workflows/${WF}`]: workflowRead(tasks) })
    await waitFor(() => expect(container.querySelector('.rn-srow[data-step="review"]')).not.toBeNull(), WAIT)
    const review = container.querySelector('.rn-srow[data-step="review"]')!
    expect(visible(review)).toContain('skipped · nothing to change')
    expect(visible(review)).not.toMatch(/elapsed/)
    expect(review.querySelector('[data-mark="skipped"]')).not.toBeNull()
    expect(visible(container.querySelector('.rn-srow[data-step="impl-contract"]'))).toMatch(/elapsed/)
  })
})

describe('RT-4 and RT-8: the checks fact says what can still change', () => {
  it('warns when a CHECKING run has had no CI read for longer than CI_UNREAD_WARN_MS', async () => {
    const { CI_UNREAD_WARN_MS } = await import('../Runs')
    const began = new Date(Date.now() - 15 * HOUR).toISOString()
    const r = run({
      state: 'CHECKING', terminal: false,
      pull_request: { number: 261, url: `https://github.com/${OWNER}/app/pull/261`, head_sha: null, checks: null },
      history: [...run().history, { at: began, from: 'RUNNING', to: 'CHECKING', by: 'swarm-api' }],
    })
    const { container } = await mountRun(r)
    const warn = await waitFor(() => {
      const el = container.querySelector('.rn-ci-unread')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    expect(visible(warn)).toMatch(/no CI read · the run began checking 15h ago/)
    expect(15 * HOUR).toBeGreaterThan(CI_UNREAD_WARN_MS)
  })

  it('says a finished run\'s last reading is the last, and a run with no pull request has none', async () => {
    const closed = run({
      state: 'FAILED',
      pull_request: { number: 7, url: `https://github.com/${OWNER}/app/pull/7`, head_sha: '25fd13e' + '0'.repeat(33), checks: 'pending' },
    })
    const a = await mountRun(closed)
    await waitFor(() => expect(a.container.querySelector('.rn-ci')).not.toBeNull(), WAIT)
    expect(visible(a.container.querySelector('.rn-ci'))).toMatch(/last read before the run ended: pending/)
    a.unmount()
    const none = run({ outcome: 'already_on_main', requirements_met: true })
    const b = await mountRun(none)
    await waitFor(() => expect(b.container.querySelector('.rn-ci')).not.toBeNull(), WAIT)
    const ci = visible(b.container.querySelector('.rn-ci'))
    expect(ci).not.toMatch(/not read yet|not green yet/)
    expect(ci).toMatch(/none · no pull request/)
  })
})

describe('RT-6: the list\'s chip and search are its address', () => {
  const runs = [
    run({ id: 'run_a', state: 'FAILED', issue_read: { ...run().issue_read, title: 'merge queue stalls' } }),
    run({ id: 'run_b', state: 'DONE' }),
    run({ id: 'run_c', state: 'NOT_READY', workflow_id: null }),
  ]
  const rowIds = (c: HTMLElement) => [...c.querySelectorAll('.rn-row td[data-label="Run"]')].map(visible)

  it('reads `?state=` and `?q=` back on mount, as Back to the list does', async () => {
    const { runListQuery, runListQueryString } = await import('../Runs')
    expect(runListQuery(runListQueryString('not ready', 'merge'))).toEqual({ which: 'not ready', text: 'merge' })
    expect(runListQueryString('all', '  ')).toBe('')
    window.history.replaceState(null, '', '/runs?state=failed&q=merge')
    const { container } = await mountList(runs, 'state=failed&q=merge')
    await waitFor(() => expect(rowIds(container)).toEqual(['run_a']), WAIT)
    expect((within(container).getByRole('searchbox') as HTMLInputElement).value).toBe('merge')
  })

  it('writes a chip and a search to the address in place', async () => {
    window.history.replaceState(null, '', '/runs')
    const before = window.history.length
    const { container } = await mountList(runs)
    const chips = within(container).getByRole('group', { name: 'Which runs' })
    fireEvent.click(within(chips).getByRole('button', { name: /^Failed/ }))
    fireEvent.change(within(container).getByRole('searchbox'), { target: { value: 'merge' } })
    await waitFor(() => expect(window.location.pathname + window.location.search).toBe('/runs?state=failed&q=merge'))
    expect(window.history.length, 'replaced, never pushed').toBe(before)
  })

  it('offers not ready and rejected, finds a run by its workflow id, and links the issue (RT-10, RT-12)', async () => {
    const { container } = await mountList(runs)
    const chips = within(container).getByRole('group', { name: 'Which runs' })
    fireEvent.click(within(chips).getByRole('button', { name: /^Not ready/ }))
    expect(rowIds(container)).toEqual(['run_c'])
    fireEvent.click(within(chips).getByRole('button', { name: /^All/ }))
    fireEvent.change(within(container).getByRole('searchbox'), { target: { value: WF } })
    expect(rowIds(container)).toEqual(['run_a', 'run_b'])
    fireEvent.change(within(container).getByRole('searchbox'), { target: { value: '' } })
    const link = container.querySelector<HTMLAnchorElement>('.rn-row td[data-label="Issue"] a')!
    expect(link.getAttribute('href')).toBe(ISSUE_URL)
    // RT-16: a run that ended with no workflow does not promise one.
    expect(visible(container.querySelectorAll('.rn-row')[2]!.querySelector('td[data-label="Workflow"]'))).toBe('none · the run ended before a workflow')
  })
})

describe('RT-31: a run\'s title is read whole on a phone', () => {
  it('lifts the two-line clamp from a prose title on a phone, and keeps a reference to one line', async () => {
    const { container } = await mountRun(run())
    const h1 = await waitFor(() => {
      const el = container.querySelector('.rn-run .c-phead > div.head > h1')
      expect(el).not.toBeNull()
      expect(container.querySelector('.rn-run.is-ref')).toBeNull()
      return el!
    }, WAIT)
    expect(painted(h1, 'line-clamp', PHONE)).toBe('none')
    expect(painted(h1, 'overflow', PHONE)).toBe('visible')
    expect(painted(h1, 'line-clamp', DESKTOP), 'the desktop clamp stays').toBe('2')
  })
})

describe('the smaller findings, each by its rule', () => {
  it('RT-13: an overlap row draws its own chevron, which turns when open', async () => {
    const { container } = await mountRun(run({
      plan: { ...PLAN, overlaps: [{ ref: `${OWNER}/app#12`, kind: 'pull_request', note: 'Needs action: rebase.' }] },
      state: 'PLANNED', terminal: false, approved_by: null, workflow_id: null,
    }))
    const row = await waitFor(() => {
      const el = container.querySelector<HTMLDetailsElement>('details.rn-overlap')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    const summary = row.querySelector(':scope > summary')!
    expect(painted(summary, 'content', DESKTOP, 'before')).toMatch(/25B8/)
    row.open = true
    expect(painted(summary, 'content', DESKTOP, 'before')).toMatch(/25BE/)
  })

  it('RT-14: drops terminal colour codes from a log tail, with or without the ESC', async () => {
    const { stripSgr } = await import('../Runs')
    const esc = String.fromCharCode(27)
    expect(stripSgr(`${esc}[36;1mfailed=3${esc}[0m and [36;1mok[0m`)).toBe('failed=3 and ok')
    expect(stripSgr('a[0] stays')).toBe('a[0] stays')
  })

  it('RT-17: a plan edit reads as "plan edited · revision 2"', async () => {
    const edited = run({
      plan_revision: 2, plan_edited_by: 'operator@example.com', state: 'PLANNED', terminal: false, approved_by: null, workflow_id: null,
      history: [...run().history, { at: '2026-10-10T20:07:00Z', from: 'PLANNED', to: 'PLANNED', by: 'operator@example.com' }],
    })
    const { container } = await mountRun(edited)
    await waitFor(() => expect(visible(container.querySelector('.rn-history'))).toMatch(/plan edited · revision 2 · by operator@example.com/), WAIT)
  })

  it('RT-16: the Linked card of an ended run with no workflow says none, not "none yet"', async () => {
    const { container } = await mountRun(run({ state: 'REJECTED', workflow_id: null, approved_by: null }))
    await waitFor(() => expect(container.querySelector('.rn-links')).not.toBeNull(), WAIT)
    const links = visible(container.querySelector('.rn-links'))
    expect(links).toContain('none · the run ended before a workflow')
    expect(links).not.toMatch(/created on approval/)
  })

  it('RT-18: the issue body is rendered, not shown as raw Markdown', async () => {
    const { container } = await mountRun(run())
    const md = await waitFor(() => {
      const el = container.querySelector('.rn-issue-md')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    expect(md.querySelector('h4, h5, h6'), 'a `##` line is a heading').not.toBeNull()
    expect(md.querySelector('strong, b'), '`**…**` is bold').not.toBeNull()
    expect(visible(md)).not.toMatch(/##|\*\*/)
  })
})

// The run's type is the served shape; this keeps the fixture honest to it.
export type _RunShape = IssueRun
