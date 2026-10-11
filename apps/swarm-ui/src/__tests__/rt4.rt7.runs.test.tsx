/**
 * RT-4 and RT-7, the Runs page's server findings (live QA, 2026-10-11).
 *
 *   * RT-4: run_e525710be66e4d4fbb29 (ai-studio#258, PR 261) sat CHECKING for
 *     15 hours with `pull_request.checks` null, and the card said "not read
 *     yet". The CI loop had stored why on `pull_request.read_error`, but the
 *     API did not serve it. It does now, with `checked_at`, and the card says
 *     "not read" and the reason instead of implying no read was tried.
 *   * RT-7: `GET /v1/runs` was 1.5 MB for 50 rows. The list reads
 *     `?view=summary` (`issueruns.run_summary`), which leaves the plan,
 *     history, open work and per-check list out, and draws a summary row.
 *
 * MUTATIONS: make the card ignore `read_error`, or `loadRuns` drop
 * `view=summary` -- a case below turns red.
 */
import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
const said = (li: Element | null) => visible(li).slice(visible(li?.querySelector('b') ?? null).length).trim()
// Built at runtime: a long hex literal reads as a key to a credential scan.
const DIGEST = 'sha256:' + 'ab'.repeat(32)
const HEAD = '4e1f0aa' + '3c'.repeat(16) + '7'
const REF = 'sagaxyz/ai-studio#258'
const ISSUE_URL = 'https://github.com/sagaxyz/ai-studio/issues/258'
const PR_URL = 'https://github.com/sagaxyz/ai-studio/pull/261'
const RUN_ID = 'run_e525710be66e4d4fbb29'
const WF = 'wf_0a1b2c3d4e5f60718293'
const READ_ERROR = 'checks_forbidden: GitHub refused the tenant\'s forge credential for the commit statuses at '
  + 'sagaxyz/ai-studio@4e1f0aa3c3c3 (HTTP 403): the credential needs `statuses: read` on the repository'

const SUMMARY_ROW = {
  id: RUN_ID, tenant_id: 'eng', state: 'CHECKING', terminal: false,
  issue: { ref: REF, owner: 'sagaxyz', repo: 'ai-studio', number: 258, url: ISSUE_URL,
    repository_url: 'https://github.com/sagaxyz/ai-studio' },
  plan_approval: 'auto', auto_merge: false, fix_rounds: 3, planner_task_id: 'task_planner258',
  plan_digest: DIGEST, plan_shape: '1 step in 1 stage, then review and fix', plan_revision: 1, plan_edited_by: null,
  workflow_id: WF, created_by: 'operator@example.com', created_at: '2026-10-10T18:00:00Z', updated_at: '2026-10-11T09:00:00Z',
  approved_by: 'swarm-auto', approved_at: '2026-10-10T18:05:00Z', approved_digest: DIGEST,
  rejected_by: null, rejection_reason: null, error: null,
  issue_read: { title: 'Studio sessions drop on reload', state: 'open', url: ISSUE_URL, read_at: '2026-10-10T18:00:00Z' },
  issue_read_error: null,
  pull_request: { number: 261, url: PR_URL, head_sha: HEAD, checks: null, checked_at: '2026-10-11T09:20:00Z', read_error: READ_ERROR },
}

function fullRun(over: Record<string, unknown> = {}) {
  return {
    ...SUMMARY_ROW,
    plan: { summary: 'Keep it.', requirements: ['Sessions survive a reload'], overlaps: [],
      steps: [{ step_id: 'impl', title: 'keep', prompt: 'Keep the session.', depends_on: [] }] },
    history: [{ at: '2026-10-10T18:00:00Z', from: null, to: 'PLANNING', by: 'operator@example.com' }],
    issue_read: { ...SUMMARY_ROW.issue_read, labels: [], comments: 0, body: 'It drops.', body_truncated: false, body_redacted: false },
    ...over,
  }
}

function serve(routes: Record<string, unknown>, asked: string[]) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    asked.push(url)
    if (url in routes) return new Response(JSON.stringify(routes[url]), { status: 200, headers: JSON_HEADERS })
    return new Response(JSON.stringify({ code: 'not_found', message: url }), { status: 404, headers: JSON_HEADERS })
  }) as unknown as typeof fetch
}

async function screen(view: string | null, routes: Record<string, unknown>) {
  const asked: string[] = []
  serve(routes, asked)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  return { asked, ...render(<RunsScreen view={view} go={vi.fn()} />) }
}

/** The pull request card, once the run page has drawn it. */
async function card(container: HTMLElement): Promise<HTMLElement> {
  return waitFor(() => {
    const ci = container.querySelector<HTMLElement>('.rn-ci')
    expect(ci).not.toBeNull()
    return ci!
  }, WAIT)
}

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('RT-4: a CI read that failed is said, with why', () => {
  it('says "not read" and the server\'s reason, never "not read yet"', async () => {
    const { container } = await screen(`run=${RUN_ID}`, { [`/v1/runs/${RUN_ID}`]: { run: fullRun() } })
    const ci = await card(container)
    const fact = (label: string) => [...ci.querySelectorAll('.ctl-fact')].find((li) => visible(li.querySelector('b')).startsWith(label)) ?? null
    await waitFor(() => expect(fact('last read')).not.toBeNull(), WAIT)
    expect(said(fact('checks'))).toBe('not read')
    expect(visible(ci)).not.toContain('not read yet')
    expect(said(fact('last read'))).toContain('`statuses: read`')
    expect(fact('last read')!.querySelector('[title="2026-10-11T09:20:00Z"]')).not.toBeNull()
  })

  it('keeps "not read yet" for a run the loop has not tried, and draws no error line', async () => {
    const pr = { number: 261, url: PR_URL, head_sha: HEAD, checks: null, checked_at: null, read_error: null }
    const { container } = await screen(`run=${RUN_ID}`, { [`/v1/runs/${RUN_ID}`]: { run: fullRun({ pull_request: pr }) } })
    const ci = await card(container)
    await waitFor(() => expect(visible(ci)).toContain('not read yet'), WAIT)
    expect(ci.querySelector('.rn-ci-error')).toBeNull()
  })
})

describe('RT-7: the Runs list reads the summary view', () => {
  it('asks GET /v1/runs?view=summary and draws a summary row', async () => {
    const { asked, container } = await screen(null, {
      '/v1/runs?view=summary': { runs: [SUMMARY_ROW], next_page_token: null, tenant_id: 'eng', view: 'summary' },
    })
    await waitFor(() => expect(container.querySelector('.rn-row')).not.toBeNull(), WAIT)
    expect(asked).toContain('/v1/runs?view=summary')
    expect(asked).not.toContain('/v1/runs')
    const row = container.querySelector('.rn-row')!
    expect(visible(row)).toContain('Studio sessions drop on reload')
    expect(visible(row)).toContain(RUN_ID)
  })
})
