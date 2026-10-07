/**
 * QA G2-25 (2026-10-07) on the run page: `from PLANNING` in a run's Progress
 * and `Why: the run's workflow … ended FAILED`, the API's capitals in prose.
 * Mounted as qa.g2.runs.test.tsx mounts the run, with no api mock: the run
 * read goes through `fetch`. The inspector's half is qa.g2.words.test.tsx.
 *
 * MUTATION: print `h.from` or `run.error` verbatim again (Runs.tsx). The case
 * turns red.
 */
import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

afterEach(() => {
  vi.unstubAllEnvs()
})

// The run page's Why and Progress (Runs.tsx), mounted as qa.g2.runs.test.tsx does.
const RUN_ID = 'run_c6o0a1b2c3d4e5f60718'
const WF = 'wf_c6o1b2c3d4e5f6071829'
const JSON_HEADERS = { 'content-type': 'application/json' }

function issueRunDoc() {
  const owner = 'bogdan-alexandrescu'
  return {
    id: RUN_ID, tenant_id: 'eng', state: 'FAILED', terminal: true,
    issue: { ref: `${owner}/SwarmCloud#72`, owner, repo: 'SwarmCloud', number: 72, url: `https://github.com/${owner}/SwarmCloud/issues/72`,
      repository_url: `https://github.com/${owner}/SwarmCloud` },
    plan_approval: 'required', auto_merge: true, fix_rounds: 3, planner_task_id: 'task_planner72',
    plan: { summary: 'Fix it.', requirements: [], overlaps: [], steps: [{ step_id: 'impl', title: 'impl', prompt: 'Build it.', depends_on: [] }] },
    plan_digest: 'sha256:' + 'cd'.repeat(32), plan_revision: 1, plan_edited_by: null, workflow_id: WF,
    created_by: 'operator@example.com', created_at: '2026-10-04T20:00:00Z', updated_at: '2026-10-04T21:00:00Z',
    approved_by: 'operator@example.com', approved_at: '2026-10-04T20:09:00Z', approved_digest: 'sha256:' + 'cd'.repeat(32),
    rejected_by: null, rejection_reason: null, error: `the run's workflow ${WF} ended FAILED`, green_sha: null, requirements_met: null,
    pull_request: null,
    history: [
      { at: '2026-10-04T20:00:00Z', from: null, to: 'PLANNING', by: 'operator@example.com' },
      { at: '2026-10-04T20:05:00Z', from: 'PLANNING', to: 'PLANNED', by: 'swarm-api' },
      { at: '2026-10-04T20:30:00Z', from: 'RUNNING', to: 'FAILED', by: 'swarm-api' },
    ],
    issue_read: null,
    issue_read_error: null,
  }
}

describe('G2-25: the run page words its states', () => {
  it('prints `from planning` in Progress and `ended failed` in Why', async () => {
    const routes: Record<string, unknown> = { [`/v1/runs/${RUN_ID}`]: { run: issueRunDoc() } }
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url in routes) return new Response(JSON.stringify(routes[url]), { status: 200, headers: JSON_HEADERS })
      return new Response(JSON.stringify({ code: 'not_found', message: url }), { status: 404, headers: JSON_HEADERS })
    }) as unknown as typeof fetch
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { RunsScreen } = await import('../Runs')
    const { container } = render(<RunsScreen view={`run=${RUN_ID}`} go={vi.fn()} />)
    await waitFor(() => expect(container.querySelector('.rn-history')).not.toBeNull(), { timeout: 4000 })
    const history = visible(container.querySelector('.rn-history'))
    expect(history).toContain('from planning')
    expect(history).toContain('from running')
    expect(history).not.toMatch(/from [A-Z]{2,}/)
    expect(visible(container.querySelector('.rn-error'))).toBe(`Why: the run's workflow ${WF} ended failed`)
  })
})
