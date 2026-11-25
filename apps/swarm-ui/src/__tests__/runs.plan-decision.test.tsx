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

import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
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

async function mount(served: unknown) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url === `/v1/runs/${RUN_ID}`) return new Response(JSON.stringify({ run: served }), { status: 200, headers: JSON_HEADERS })
    return new Response(JSON.stringify({ code: 'not_found', message: url }), { status: 404, headers: JSON_HEADERS })
  }) as unknown as typeof fetch
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  const go = vi.fn()
  const utils = render(<RunsScreen view={`run=${RUN_ID}`} go={go} />)
  await waitFor(() => expect(utils.container.querySelector('.rn-chosen')).not.toBeNull(), WAIT)
  return { page: utils.container, go }
}

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('the decision page link', () => {
  it('links a PLANNED run to its decision page', async () => {
    const { page } = await mount(run())
    const link = page.querySelector<HTMLAnchorElement>('[data-testid="rn-decide"]')
    expect(link).not.toBeNull()
    expect(link!.getAttribute('href')!.split('?')[0]).toBe(`/automate/approvals/${encodeURIComponent(`run:${RUN_ID}`)}`)
    // An action drawn as a button, not a text link.
    expect(link!.classList.contains('c-btn')).toBe(true)
  })

  it('draws no decision link once the run is past PLANNED', async () => {
    const { page } = await mount(run({ state: 'REJECTED', terminal: true, rejected_by: 'operator@example.com', rejection_reason: 'no' }))
    expect(page.querySelector('[data-testid="rn-decide"]')).toBeNull()
  })
})

describe('the review spend', () => {
  it('shows no review figure, never $0, for a run read without plan_review_usage', async () => {
    const { page } = await mount(run())
    expect(page.querySelector('[data-testid="rn-review-spend"]')).toBeNull()
    expect(visible(page.querySelector('.rn-chosen'))).not.toMatch(/review \$/)
  })

  it('shows "review $6.40 of $25" beside the cost of a planned run with usage', async () => {
    const { page } = await mount(run({ plan_review_usage: USAGE }))
    const fact = page.querySelector('[data-testid="rn-review-spend"]')
    expect(fact).not.toBeNull()
    expect(visible(fact)).toBe('review $6.40 of $25')
    // Beside the cost, in the same list.
    const cost = Array.from(page.querySelectorAll('.rn-chosen .ctl-fact')).find((li) => visible(li.querySelector('b')) === 'cost so far')
    expect(cost).toBeDefined()
    expect(cost!.nextElementSibling).toBe(fact!.closest('li'))
  })

  it('says "at least" when a cost went unreported', async () => {
    const { page } = await mount(run({ plan_review_usage: { ...USAGE, review_usd: { spent: 6.4, cap: 25, at_least: true } } }))
    expect(visible(page.querySelector('[data-testid="rn-review-spend"]'))).toBe('review $6.40 of $25 · at least')
  })
})
