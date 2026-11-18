/**
 * THE PLANNER'S CHECK ON THE PLAN APPROVAL PAGE (CLD's Jev report: plan
 * coverage; owner 2026-11-02). The planner's worker judges its own plan:
 * which requirements no step covers (J2) and whether the plan likely
 * duplicates work in flight (J5). swarm-api serves that on the run read as
 * `plan_advisory`; until now nobody saw it. The page draws a short block
 * above the steps -- each flagged requirement with "no step covers this" or
 * "weakly covered", the duplicate with its title, link and how likely, and
 * why the plan waits -- and nothing at all when there is no advisory.
 */
import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
// Built at runtime: a 64-hex literal reads as a key to a credential scan.
const DIGEST = 'sha256:' + 'ab'.repeat(32)
const RUN_ID = 'run_jev0advisory0001'

const ADVISORY = {
  coverage: { requirements: 3, flagged: [
    { index: 1, requirement: 'The sort survives a reload', coverage: 'none' },
    { index: 2, requirement: 'Empty names sort last', coverage: 'weak' },
  ] },
  duplicate: { title: 'Sort widgets by name', ref: 'acme/widgets#41', url: 'https://github.com/acme/widgets/issues/41', p: 0.91 },
  hold_for_human: true,
  judged_revision: 1,
}

function run(extra: Record<string, unknown> = {}) {
  return {
    id: RUN_ID, tenant_id: 'eng', state: 'PLANNED', terminal: false,
    issue: { ref: 'acme/widgets#40', owner: 'acme', repo: 'widgets', number: 40,
      url: 'https://github.com/acme/widgets/issues/40', repository_url: 'https://github.com/acme/widgets' },
    plan_approval: 'required', auto_merge: false, fix_rounds: 3, planner_task_id: 'task_planner0001',
    plan: {
      summary: 'Sort the widget list by name.',
      requirements: ['The Name header sorts the list', 'The sort survives a reload', 'Empty names sort last'],
      steps: [{ step_id: 'impl', title: 'Sort the list', prompt: 'Edit the list.' }],
      overlaps: [],
    },
    plan_digest: DIGEST, plan_revision: 1, plan_edited_by: null, workflow_id: null,
    created_by: 'operator@example.com', created_at: '2026-11-02T04:00:00Z', updated_at: '2026-11-02T04:05:00Z',
    approved_by: null, approved_at: null, approved_digest: null, rejected_by: null, rejection_reason: null,
    error: null,
    history: [{ at: '2026-11-02T04:00:00Z', from: null, to: 'PLANNING', by: 'operator@example.com' }],
    issue_read: null,
    issue_read_error: null,
    ...extra,
  }
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
  const utils = render(<RunsScreen view={`run=${RUN_ID}`} go={vi.fn()} />)
  await waitFor(() => expect(utils.container.querySelector('.rn-steps')).not.toBeNull(), WAIT)
  return utils.container
}

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('the planner check on the plan approval page', () => {
  it('draws each flag and the duplicate above the steps', async () => {
    const page = await mount(run({ plan_advisory: ADVISORY }))
    const block = page.querySelector('.rn-advisory')
    expect(block).not.toBeNull()
    const text = visible(block)
    expect(text).toContain('2. The sort survives a reload · no step covers this')
    expect(text).toContain('3. Empty names sort last · weakly covered')
    expect(text).toContain('Sort widgets by name')
    expect(text).toContain('acme/widgets#41')
    expect(text).toContain('91% likely')
    expect(text).toContain('waits for a person')
    const link = block!.querySelector('a')
    expect(link?.getAttribute('href')).toBe('https://github.com/acme/widgets/issues/41')
    // Above the steps.
    const steps = page.querySelector('.rn-steps')!
    expect(block!.compareDocumentPosition(steps) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('draws nothing when the run carries no advisory', async () => {
    for (const served of [run(), run({ plan_advisory: null })]) {
      const page = await mount(served)
      expect(page.querySelector('.rn-advisory')).toBeNull()
      expect(visible(page)).not.toContain('no step covers this')
    }
  })

  it('draws nothing when the advisory flagged nothing', async () => {
    const page = await mount(run({ plan_advisory: { coverage: { requirements: 3, flagged: [] }, duplicate: null, hold_for_human: false, judged_revision: 1 } }))
    expect(page.querySelector('.rn-advisory')).toBeNull()
  })

  it('links a duplicate only on github.com, and says when the plan was edited after it was judged', async () => {
    const page = await mount(run({
      plan_revision: 2,
      plan_advisory: { ...ADVISORY, hold_for_human: false, duplicate: { title: 'Same work', ref: 'swarm/sort', url: 'javascript:alert(1)', p: 0.5 } },
    }))
    const block = page.querySelector('.rn-advisory')!
    expect(block.querySelector('a')).toBeNull()
    expect(visible(block)).toContain('swarm/sort')
    expect(visible(block)).not.toContain('waits for a person')
    expect(visible(block)).toContain('revision 1')
  })
})
