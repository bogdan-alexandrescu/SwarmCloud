// The issue-run page's click-only states the fixture build never draws
// (issue 439): the run page of Runs.tsx mounted live on hand-built runs, with
// `fetch` stubbed, so ONE click (or one short act sequence) opens a state.
// Copied from qa1fix24.runs.test.tsx and qa.503.runs.test.tsx;
// `restoreRunStubs` undoes the fetch, env and module stubs after each state.

import type { ReactElement } from 'react'
import { vi } from 'vitest'

import { serve } from './repofixture'
import { FULL_PLAN, digest, issueRun } from './runfixture'

const RUN_ID = 'run_4c1e09d2'

const restores: (() => void)[] = []

/** Undo every stub a mount set: called after each state. */
export function restoreRunStubs(): void {
  for (const undo of restores.splice(0).reverse()) undo()
}

type Json = Record<string, unknown>
type Handler = (method: string, url: string) => { status: number; body: unknown } | null

/** Stub fetch on the live build with `handler`, and render the runs screen at `view` (the run page by default). */
async function mountServed(handler: Handler, view: string | null = `run=${RUN_ID}`): Promise<ReactElement> {
  const realFetch = globalThis.fetch
  serve(handler)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  restores.push(() => {
    globalThis.fetch = realFetch
    vi.unstubAllEnvs()
    vi.resetModules()
  })
  const { RunsScreen } = await import('../Runs')
  return <RunsScreen view={view} go={() => undefined} />
}

/** Stub fetch on the live build, serve the run, and render the run page. */
const mountRun = (run: Json, extra: Handler = () => null): Promise<ReactElement> =>
  mountServed((m, url) => (url === `/v1/runs/${RUN_ID}` && m === 'GET' ? { status: 200, body: { run } } : extra(m, url)))

const SUMMARY = [
  'Sum each step\'s spend into the workflow read so the list can draw a cost column without a second request.',
  'The sum is taken in the route, not the store, because the store keeps no per-step total.',
  'The UI then draws the column beside the state and sorts on it.',
  'A workflow with no priced step shows a dash, never a zero.',
].join(' ')

/** A PLANNED run whose summary is longer than the lead. Click `.rn-more-btn` ("Read more"): it reads "Show less" with `aria-expanded="true"`, and `.rn-summary` holds the whole summary. */
export const mountRunLongSummary = (): Promise<ReactElement> =>
  mountRun(issueRun({ plan: { ...FULL_PLAN, overlaps: [], summary: SUMMARY } }))

/** A PLANNED run with two overlaps, one needing action. Click `details.rn-overlap > summary` (the first): `details.rn-overlap[open]` shows `.rn-overlap-note`. */
export const mountRunOverlaps = (): Promise<ReactElement> =>
  mountRun(issueRun({
    plan: {
      ...FULL_PLAN,
      overlaps: [
        { ref: 'example-org/infra#498', kind: 'pull_request', action: 'required', note: 'Already edits routes/workflows.py, so the two changes will conflict unless this plan waits for it.' },
        { ref: 'example-org/infra#507', kind: 'issue', action: 'none', note: 'Asks for the same column; this plan closes it as a side effect.' },
      ],
    },
  }))

/** A CHECKING run whose pull request serves a check list. Click `details.rn-check-list > summary`: `details.rn-check-list[open]` shows `.rn-failed-checks`. */
export const mountRunChecks = (): Promise<ReactElement> => {
  const head = '7138b1a' + '0f'.repeat(16) + '9'
  const prUrl = 'https://github.com/example-org/infra/pull/564'
  return mountRun(issueRun({
    state: 'CHECKING',
    workflow_id: null,
    approved_by: 'operator@example.com',
    approved_at: '2026-10-02T14:20:00Z',
    approved_digest: digest('a1'),
    pull_request: {
      number: 564, url: prUrl, head_sha: head, checks: 'pending', merged: false, merged_at: null,
      check_counts: { passed: 2, failed: 0, pending: 1, skipped: 0 },
      check_list: [
        { name: 'unit', state: 'passed', url: `${prUrl}/checks/1` },
        { name: 'lint', state: 'passed', url: `${prUrl}/checks/2` },
        { name: 'build images', state: 'pending', url: null },
      ],
      check_list_truncated: false, ci_url: null,
    },
  }))
}

/** A PLANNED run whose approval is refused (500). Click "Approve and run" in `.rn-actions`: `.rn-wait .state.failed` appears (the FailedPanel). */
export const mountRunApproveRefused = (): Promise<ReactElement> =>
  mountRun(issueRun(), (m, url) =>
    url.endsWith('plan:approve') && m === 'POST'
      ? { status: 500, body: { code: 'internal', message: 'the store refused the approval' } }
      : null)

/** A PLANNED run whose rejection is accepted. Acts: click "Reject" in `.rn-actions`; type a reason into `.rn-reject textarea`; click "Reject this plan" (submit): `.rn-decided` shows "Rejected.". */
export const mountRunRejected = (): Promise<ReactElement> =>
  mountRun(issueRun(), (m, url) =>
    url.endsWith('plan:reject') && m === 'POST'
      ? { status: 200, body: { run: issueRun({ state: 'REJECTED', terminal: true, rejected_by: 'operator@example.com', rejection_reason: 'Out of scope' }) } }
      : null)

/** A PLANNED run whose rejection is refused (500). Acts: click "Reject"; type a reason; click "Reject this plan": `.state.failed` appears beside the form. */
export const mountRunRejectRefused = (): Promise<ReactElement> =>
  mountRun(issueRun(), (m, url) =>
    url.endsWith('plan:reject') && m === 'POST'
      ? { status: 500, body: { code: 'internal', message: 'the store refused the rejection' } }
      : null)

/** A PLANNED run whose plan edit is refused (500). Acts: click "Edit plan"; change the summary; click "Save the plan": `.state.failed` appears under the editor. */
export const mountRunEditRefused = (): Promise<ReactElement> =>
  mountRun(issueRun(), (m, url) =>
    url.endsWith('plan:edit') && m === 'POST'
      ? { status: 500, body: { code: 'internal', message: 'the store refused the edit' } }
      : null)

/** The run as a second read serves it: someone else edited the plan. */
const EDITED = (): Json =>
  issueRun({
    plan_digest: digest('b2'),
    plan_revision: 2,
    plan_edited_by: 'someone@example.com',
    plan: { ...FULL_PLAN, overlaps: [], summary: 'An edited plan.' },
  })

/** The first GET serves the run, every later one `then()`: a re-read or a poll reads the edited plan. */
function servedTwice(then: () => Json, extra: Handler): Handler {
  let reads = 0
  return (m, url) => {
    if (url === `/v1/runs/${RUN_ID}` && m === 'GET') {
      reads += 1
      return { status: 200, body: { run: reads === 1 ? issueRun() : then() } }
    }
    return extra(m, url)
  }
}

/** A PLANNED run whose approval is refused with 409 `plan_changed`. Click "Approve and run": the run is read again and `.rn-notice` says why. */
export const mountRunPlanChanged = (): Promise<ReactElement> =>
  mountServed(servedTwice(EDITED, (m, url) =>
    url.endsWith('plan:approve') && m === 'POST'
      ? { status: 409, body: { code: 'plan_changed', message: 'the plan has changed since it was shown', detail: { plan_digest: digest('b2') } } }
      : null))

/** A PLANNED run whose approval is refused with 409 `invalid_run_transition`. Click "Approve and run": the run is read again and `.rn-notice` says why. */
export const mountRunMovedOn = (): Promise<ReactElement> =>
  mountServed(servedTwice(() => issueRun({ state: 'REJECTED', terminal: true, rejected_by: 'operator@example.com', rejection_reason: 'Out of scope' }), (m, url) =>
    url.endsWith('plan:approve') && m === 'POST'
      ? { status: 409, body: { code: 'invalid_run_transition', message: 'the run is REJECTED' } }
      : null))

/** A PLANNED run whose next poll serves someone else's plan. Let the poll land: `.rn-held` offers it. */
export const mountRunHeld = (): Promise<ReactElement> => mountServed(servedTwice(EDITED, () => null))

/** The runs list's first page says older runs exist; the older page answers `older`. */
function listPages(older: { status: number; body: unknown }): Handler {
  return (_m, url) => {
    if (!url.startsWith('/v1/runs')) return null
    const token = new URL(url, 'http://x').searchParams.get('page_token')
    return token === null ? { status: 200, body: { runs: [issueRun({ id: 'run_a' })], next_page_token: 'tok1', tenant_id: 'eng' } } : older
  }
}

/** The runs list with an older page. Click "Show older runs": a second `.rn-row` is drawn. */
export const mountRunsOlder = (): Promise<ReactElement> =>
  mountServed(listPages({ status: 200, body: { runs: [issueRun({ id: 'run_b' })], next_page_token: null, tenant_id: 'eng' } }), null)

/** The runs list whose older page is refused (500). Click "Show older runs": `.rn-list .state.failed` appears. */
export const mountRunsOlderRefused = (): Promise<ReactElement> =>
  mountServed(listPages({ status: 500, body: { code: 'internal', message: 'the store refused the page' } }), null)
