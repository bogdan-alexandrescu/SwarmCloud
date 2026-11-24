/**
 * THE COST HOLD AND THE READINESS PRE-PASS ON THE RUN PAGE (owner decisions,
 * 2026-11-24). Each adds at most a status word, its reason in the tooltip:
 * `cost hold` on an auto plan held for its expected cost, `pre-pass` on a run
 * the readiness pre-pass ended NOT_READY. No new prose.
 */
import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
// Built at runtime: a 64-hex literal reads as a key to a credential scan.
const DIGEST = 'sha256:' + 'cd'.repeat(32)
const RUN_ID = 'run_costhold0prepass1'

const FINDINGS = [
  { code: 'step_tests_missing', step: 'api', requirement: null, message: 'Step api names no tests that prove it.' },
  { code: 'requirement_uncovered', step: null, requirement: 2, message: 'No step delivers requirement 2.' },
]

function lint(extra: Record<string, unknown> = {}) {
  return { replans: 1, held: true, findings: FINDINGS, replan_findings: FINDINGS,
    planner_task_ids: ['task_planner0001', 'task_planner0002'], replan_error: null, ...extra }
}

function run(extra: Record<string, unknown> = {}) {
  return {
    id: RUN_ID, tenant_id: 'eng', state: 'PLANNED', terminal: false,
    issue: { ref: 'example/widgets#40', owner: 'example', repo: 'widgets', number: 40,
      url: 'https://github.com/example/widgets/issues/40', repository_url: 'https://github.com/example/widgets' },
    plan_approval: 'required', auto_merge: false, fix_rounds: 3, planner_task_id: 'task_planner0002',
    plan: {
      summary: 'Sort the widget list by name.',
      requirements: ['The Name header sorts the list', 'The sort survives a reload'],
      steps: [
        { step_id: 'api', title: 'Sort on the server', prompt: 'Edit the list route.', files: ['api/list.py'] },
        { step_id: 'ui', title: 'Sort header', prompt: 'Edit the header.', files: ['ui/list.tsx'], tests: ['ui/list.test.tsx'], requirements: [1] },
      ],
      overlaps: [],
    },
    plan_digest: DIGEST, plan_revision: 1, plan_edited_by: null, workflow_id: null,
    created_by: 'operator@example.com', created_at: '2026-11-23T04:00:00Z', updated_at: '2026-11-23T04:05:00Z',
    approved_by: null, approved_at: null, approved_digest: null, rejected_by: null, rejection_reason: null,
    error: null,
    history: [{ at: '2026-11-23T04:00:00Z', from: null, to: 'PLANNING', by: 'operator@example.com' }],
    issue_read: null,
    issue_read_error: null,
    ...extra,
  }
}

/** What the page POSTed: each write's path and body. */
type Sent = { url: string; body: Record<string, unknown> }

async function mount(served: unknown, sent: Sent[] = [], ready = '.rn-steps') {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (init?.method === 'POST') {
      const body = JSON.parse(String(init.body ?? '{}')) as Record<string, unknown>
      sent.push({ url, body })
      return new Response(JSON.stringify({ run: served }), { status: 200, headers: JSON_HEADERS })
    }
    if (url === `/v1/runs/${RUN_ID}`) return new Response(JSON.stringify({ run: served }), { status: 200, headers: JSON_HEADERS })
    return new Response(JSON.stringify({ code: 'not_found', message: url }), { status: 404, headers: JSON_HEADERS })
  }) as unknown as typeof fetch
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  const utils = render(<RunsScreen view={`run=${RUN_ID}`} go={vi.fn()} />)
  await waitFor(() => expect(utils.container.querySelector(ready)).not.toBeNull(), WAIT)
  return utils.container
}

afterEach(() => {
  vi.unstubAllEnvs()
})

const COST_REASON = 'The plan\'s expected cost is $27.00-$81.00, above the $30.00 an automatic approval may start (SWARM_PLAN_COST_HOLD_USD); a person approves it.'

describe('an auto plan held for its expected cost', () => {
  it('says cost hold, with the reason in the tooltip and the finding listed', async () => {
    const page = await mount(run({ plan_lint: lint({ replans: 0, replan_findings: [], planner_task_ids: [],
      findings: [{ code: 'EXPECTED_COST_OVER_THRESHOLD', step: null, requirement: null, message: COST_REASON }] }) }))
    const block = page.querySelector('.rn-lint')!
    const chip = block.querySelector('[title]')
    expect(visible(chip)).toBe('cost hold')
    expect(chip?.getAttribute('title')).toBe(COST_REASON)
  })

  it('keeps the lint word for a lint hold', async () => {
    const page = await mount(run({ plan_lint: lint() }))
    expect(visible(page.querySelector('.rn-lint [title]'))).toBe('lint · 2 findings')
  })
})

describe('a run the readiness pre-pass found not ready', () => {
  const notReady = (outcome: string) => run({
    state: 'NOT_READY', terminal: true, plan: null, plan_digest: null, planner_task_id: '',
    not_ready: { kind: 'blocked', reason: 'Blocked by #12.', needs: [], lane: null },
    readiness: { task_id: 'task_prepass0001', outcome, reason: 'Blocked by #12.', cost_usd: 0.4 },
  })

  it('says pre-pass beside the heading, with its reason in the tooltip', async () => {
    const page = await mount(notReady('not_ready'), [], '.rn-not-ready')
    const chip = page.querySelector('.rn-not-ready h3 [title]')
    expect(visible(chip)).toBe('pre-pass')
    expect(chip?.getAttribute('title')).toBe('Blocked by #12.')
  })

  it('says nothing more when the planner itself found it not ready', async () => {
    const page = await mount(notReady('ready'), [], '.rn-not-ready')
    expect(visible(page.querySelector('.rn-not-ready h3'))).toBe('Not ready · blocked')
  })
})
