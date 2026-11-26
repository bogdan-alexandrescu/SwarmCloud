// WORK › RUNS LINKS A PLANNED RUN TO ITS DECISION PAGE, AND SHOWS ITS REVIEW
// SPEND (docs/design/plans-pipeline.md §2.3, §7.8; §11 lane U5
// PLAN-EDITOR-MOVE). Red first on main, where the run page has no link to
// `/automate/approvals/run:<id>` and no review figure.
//
// WHAT EACH CASE HOLDS:
//   * a PLANNED run's plan carries a button-styled link to its decision page;
//     a run past PLANNED does not;
//   * a run read without `plan_review_usage` shows no review figure, never $0;
//   * a planned run with usage shows "review $6.40 of $25" beside its cost.
//
// Placeholders only; nothing is token-shaped.

import { render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }
// Built at runtime: a 64-hex literal reads as a key to a credential scan.
const DIGEST = 'sha256:' + 'cd'.repeat(32)
const RUN_ID = 'run_plandecide0000001'

function run(extra: Record<string, unknown> = {}) {
  return {
    id: RUN_ID, tenant_id: 'eng', state: 'PLANNED', terminal: false,
    issue: { ref: 'example/widgets#41', owner: 'example', repo: 'widgets', number: 41,
      url: 'https://github.com/example/widgets/issues/41', repository_url: 'https://github.com/example/widgets' },
    plan_approval: 'required', auto_merge: false, fix_rounds: 3, planner_task_id: 'task_planner0001',
    plan: {
      summary: 'Sort the widget list by name.',
      requirements: ['The Name header sorts the list'],
      steps: [{ step_id: 'ui', title: 'Sort header', prompt: 'Edit the header.', files: ['ui/list.tsx'], tests: ['ui/list.test.tsx'] }],
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

const USAGE = {
  replans: { used: 2, cap: 5 },
  diagram_asks: { used: 7, cap: 20 },
  chat_turns: { used: 12, cap: 40 },
  review_usd: { spent: 6.4, cap: 25, at_least: false },
  caps_read_at: '2026-11-23T04:05:00Z',
}

const ITEM_PATH = `/automate/approvals/${encodeURIComponent('run:' + RUN_ID)}`

async function mount(served: unknown) {
  serve((m, url) => (m === 'GET' && url.split('?')[0] === `/v1/runs/${RUN_ID}` ? { status: 200, body: { run: served } } : null))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  render(<RunsScreen view={`run=${RUN_ID}`} go={vi.fn()} />)
  return screen.findByRole('region', { name: 'Chosen at submission' }, WAIT)
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('the decision page link', () => {
  it('links a PLANNED run to its decision page', async () => {
    await mount(run())
    const link = await screen.findByTestId('rn-decide', undefined, WAIT)
    expect(link.getAttribute('href')!.split('?')[0]).toBe(ITEM_PATH)
    // An action drawn as a button, not a text link.
    expect(link.classList.contains('c-btn')).toBe(true)
  })

  it('draws no decision link once the run is past PLANNED', async () => {
    await mount(run({ state: 'REJECTED', terminal: true, rejected_by: 'operator@example.com', rejection_reason: 'no' }))
    expect(screen.queryByTestId('rn-decide')).toBeNull()
  })
})

const facts = (chosen: HTMLElement) => within(chosen).getAllByRole('listitem').map(visible)

describe('the review spend', () => {
  it('shows no review figure, never $0, for a run read without plan_review_usage', async () => {
    const chosen = await mount(run())
    expect(screen.queryByTestId('rn-review-spend')).toBeNull()
    expect(visible(chosen)).not.toMatch(/review \$/)
  })

  it('shows "review $6.40 of $25" beside the cost of a planned run with usage', async () => {
    const chosen = await mount(run({ plan_review_usage: USAGE }))
    expect(visible(screen.getByTestId('rn-review-spend'))).toBe('review $6.40 of $25')
    // Beside the cost, in the same list: the fact after "cost so far".
    const all = facts(chosen)
    const cost = all.findIndex((f) => f.startsWith('cost so far'))
    expect(cost).toBeGreaterThanOrEqual(0)
    expect(all[cost + 1]).toMatch(/^review spend\s*review \$6\.40 of \$25$/)
  })

  it('says "at least" when a cost went unreported', async () => {
    await mount(run({ plan_review_usage: { ...USAGE, review_usd: { spent: 6.4, cap: 25, at_least: true } } }))
    expect(visible(screen.getByTestId('rn-review-spend'))).toBe('review $6.40 of $25 · at least')
  })
})
