/**
 * A HELD RUN'S PLAN LINT FINDINGS ON THE RUN PAGE (improvement plan
 * 2026-11-23, 'Plans that can be checked'). A planner output that failed the
 * plan lint, re-planned once and failed again, holds the run PLANNED for a
 * person with the findings on `plan_lint`. The page lists them where the plan
 * is shown, code and sentence, with what the hold means behind the chip's
 * tooltip, and draws nothing on a run that is not held.
 */
import { fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
// Built at runtime: a 64-hex literal reads as a key to a credential scan.
const DIGEST = 'sha256:' + 'cd'.repeat(32)
const RUN_ID = 'run_planlint0held0001'

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

describe("a held run's plan lint findings", () => {
  it('lists each finding, code and sentence, in the plan above the steps', async () => {
    const page = await mount(run({ plan_lint: lint() }))
    const block = page.querySelector('.rn-plan .rn-lint')
    expect(block).not.toBeNull()
    const items = Array.from(block!.querySelectorAll('li')).map(visible)
    expect(items).toEqual([
      'step_tests_missing Step api names no tests that prove it.',
      'requirement_uncovered No step delivers requirement 2.',
    ])
    const steps = page.querySelector('.rn-steps')!
    expect(block!.compareDocumentPosition(steps) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('adds no visible explanatory sentence: the hold is explained behind a tooltip', async () => {
    const page = await mount(run({ plan_lint: lint() }))
    const block = page.querySelector('.rn-lint')!
    const chip = block.querySelector('[title]')
    expect(chip?.getAttribute('title')).toMatch(/re-plan/)
    const prose = visible(block).replace(items(block), '')
    expect(prose).toBe('lint · 2 findings')
  })

  it('draws nothing on a run that is not held', async () => {
    for (const served of [run(), run({ plan_lint: null }), run({ plan_lint: lint({ held: false }) }),
      run({ plan_lint: lint({ findings: [] }) })]) {
      const page = await mount(served)
      expect(page.querySelector('.rn-lint')).toBeNull()
    }
  })
})

describe('a held run with no plan', () => {
  // Both planner outputs failed to parse and none earlier did: no plan, no digest.
  const planless = () => run({
    plan: null, plan_digest: null,
    plan_lint: lint({ findings: [{ code: 'plan_invalid', step: null, requirement: null, message: 'plan.json is not JSON.' }] }),
  })
  const buttons = (page: HTMLElement) => Array.from(page.querySelectorAll('button')).map(visible)

  it('draws Edit and Reject, and no Approve: there is no plan to approve', async () => {
    const page = await mount(planless(), [], '.rn-lint')
    expect(page.querySelector('.rn-lint')).not.toBeNull()
    expect(buttons(page)).toContain('Edit plan')
    expect(buttons(page)).toContain('Reject')
    expect(buttons(page)).not.toContain('Approve and run')
  })

  it('edits a plan in from an empty one, sending no digest', async () => {
    const sent: Sent[] = []
    const page = await mount(planless(), sent, '.rn-lint')
    fireEvent.click(Array.from(page.querySelectorAll('button')).find((b) => visible(b) === 'Edit plan')!)
    const form = await waitFor(() => {
      const f = page.querySelector<HTMLFormElement>('.rn-edit')
      expect(f).not.toBeNull()
      return f!
    }, WAIT)
    const summary = form.querySelector<HTMLTextAreaElement>('textarea')!
    expect(summary.value).toBe('')
    fireEvent.change(summary, { target: { value: 'Sort the widget list by name.' } })
    const set = form.querySelector('fieldset')!
    fireEvent.change(set.querySelector('input')!, { target: { value: 'Sort on the server' } })
    fireEvent.change(set.querySelector('textarea')!, { target: { value: 'Edit the list route.' } })
    fireEvent.submit(form)

    await waitFor(() => expect(sent.length).toBe(1), WAIT)
    const [edit] = sent as [Sent]
    expect(edit.url).toBe(`/v1/runs/${RUN_ID}/plan:edit`)
    expect('plan_digest' in edit.body).toBe(false)
    const plan = edit.body.plan as { summary: string; steps: { title: string; prompt: string }[] }
    expect(plan.summary).toBe('Sort the widget list by name.')
    expect(plan.steps.map((s) => [s.title, s.prompt])).toEqual([['Sort on the server', 'Edit the list route.']])
  })

  it('rejects it, sending no digest', async () => {
    const sent: Sent[] = []
    const page = await mount(planless(), sent, '.rn-lint')
    fireEvent.click(Array.from(page.querySelectorAll('button')).find((b) => visible(b) === 'Reject')!)
    const form = await waitFor(() => {
      const f = page.querySelector<HTMLFormElement>('form.rn-reject')
      expect(f).not.toBeNull()
      return f!
    }, WAIT)
    fireEvent.submit(form)

    await waitFor(() => expect(sent.length).toBe(1), WAIT)
    const [reject] = sent as [Sent]
    expect(reject.url).toBe(`/v1/runs/${RUN_ID}/plan:reject`)
    expect('plan_digest' in reject.body).toBe(false)
  })
})

function items(block: Element) {
  return Array.from(block.querySelectorAll('ul')).map((u) => u.textContent ?? '').join('')
}
