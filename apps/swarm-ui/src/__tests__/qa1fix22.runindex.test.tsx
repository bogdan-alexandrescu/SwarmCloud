// QA1-FIX-22 (part of #2596): AN ISSUE RUN'S CONTEXT CARD AND SELECTED-TESTS
// ROW, AS THE 2026-10-28 VISUAL QA PASS FOUND THEM.
//
// WHAT EACH CASE HOLDS:
//   * C-15: a run's page never left "reading the impact of #2584…" and
//     "reading the selection…" after POST .../impact answered 200 in 19.6 s.
//     The read was keyed by the pull request's head sha and dropped any
//     answer that arrived for an earlier key, so a head that moved while the
//     ~20 s query was in flight threw the answer away and asked again, and
//     leaving the Run tab (for Changes) and coming back started a fresh
//     ~20 s ask. An answer that arrives is drawn: an earlier head's until the
//     newer head's lands, and a remount takes the ask already in flight.
//   * runs-submit-27: a context row with nothing read ended in a bare dash
//     ("Issue #512 —", "Open work —"); each dash now carries its reason in
//     words beside it ("not read"), the full reason still on hover; and
//     (QA2-FIX-20, runs2-11) the short reason on the line itself.
//
// MUTATIONS: key the drawn answer by head again (drop the earlier head's
// answer) -- the moved-head case reads "reading the impact" forever; drop
// the in-flight share -- the remount case asks twice and its first answer is
// lost; print the dash without "not read" -- the empty-rows case fails.

import { act, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { issueRun } from './runfixture'
import { repo, sha } from './repofixture'

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
const REPO_ID = 'repo_0a1b2c3d4e5f6071'
const HEAD_A = sha('e7f8a9b')
const HEAD_B = sha('c0ffee1')

const PLAN = {
  plan_id: 'plan_1', pull_request: 57, base_sha: sha('5c4b3a2'), head_sha: HEAD_A, index_sha: sha('a1b2c3d'),
  stale: false, freshness: { state: 'current' }, policy: 'P3', depth: 3, min_confidence: 0.2,
  changed_symbols: 4, affected_callers: 17, targeted: 12, selected: 12, total_tests: 1480, selection: 'targeted',
  full_suite: null, diff: [{ path: 'a.py', status: 'modified', patch: true, added: [], removed: [] }],
  changed: [], affected: [], tests: [], fallback_triggers: [], unindexed: [], low_confidence_cut: [], lists_cut: [],
}

/** Serves the registrations and the index at once; every impact POST waits until the test answers it. */
function serve() {
  const asks: { body: unknown; answer: (body: unknown) => void }[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: JSON_HEADERS })
    if (method === 'GET' && url === '/v1/repositories') return json({ repositories: [repo({ repo_id: REPO_ID, repo: 'infra' })] })
    if (method === 'GET' && url === `/v1/repositories/${REPO_ID}/index?format=json`) return json({ commit_sha: sha('a1b2c3d'), modules: [] })
    if (method === 'POST' && url === `/v1/repositories/${REPO_ID}/impact`) {
      return new Promise<Response>((resolve) => {
        asks.push({ body: init?.body ? JSON.parse(String(init.body)) : null, answer: (body) => resolve(json(body)) })
      })
    }
    return json({ detail: 'Not Found' }, 404)
  }) as unknown as typeof fetch
  return asks
}

async function harness() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunContextCard, SelectedTestsGate, useRunIndex } = await import('../RunIndex')
  function Page({ run }: { run: Parameters<typeof useRunIndex>[0] }) {
    const ctx = useRunIndex(run)
    return (
      <>
        <RunContextCard run={run} ctx={ctx} go={vi.fn()} />
        <div className="rn-ci"><SelectedTestsGate ctx={ctx} go={vi.fn()} /></div>
      </>
    )
  }
  return Page
}

const withPr = (head: string) =>
  issueRun({
    state: 'CHECKING', approved_by: 'operator@example.com', approved_at: '2026-10-02T14:10:00Z',
    pull_request: { number: 57, url: 'https://github.com/example-org/infra/pull/57', head_sha: head, checks: 'pending' },
  }) as never

const impactLine = () => document.querySelector('[data-ctx="impact"]')
const gate = () => document.querySelector('.rc-gate[data-gate="selected"]')

afterEach(() => vi.unstubAllEnvs())

describe('C-15: the impact a 200 answered is drawn', () => {
  it('draws an earlier head’s answer while the moved head’s ask is still out, then the newer one', async () => {
    const asks = serve()
    const Page = await harness()
    const view = render(<Page run={withPr(HEAD_A)} />)
    await waitFor(() => expect(asks).toHaveLength(1), WAIT)
    // The workflow pushed: the next poll serves a new head while the first ask is in flight.
    view.rerender(<Page run={withPr(HEAD_B)} />)
    await waitFor(() => expect(asks).toHaveLength(2), WAIT)
    await act(async () => asks[0]!.answer(PLAN))
    await waitFor(() => expect(visible(impactLine())).toContain('pull request #57: 1 file · 4 changed symbols'), WAIT)
    expect(visible(impactLine())).not.toContain('reading')
    expect(visible(gate())).toContain('12 of 1,480 selected')
    // The newer head's answer replaces it when it lands, and is never overwritten by an older one.
    await act(async () => asks[1]!.answer({ ...PLAN, head_sha: HEAD_B, targeted: 9, changed_symbols: 2 }))
    await waitFor(() => expect(visible(gate())).toContain('9 of 1,480 selected'), WAIT)
    expect(visible(impactLine())).toContain('2 changed symbols')
  })

  it('takes the ask already in flight when the Run tab is left and opened again, asking once', async () => {
    const asks = serve()
    const Page = await harness()
    const first = render(<Page run={withPr(HEAD_A)} />)
    await waitFor(() => expect(asks).toHaveLength(1), WAIT)
    first.unmount()
    render(<Page run={withPr(HEAD_A)} />)
    await waitFor(() => expect(visible(impactLine())).toContain('reading the impact of #57'), WAIT)
    await act(async () => asks[0]!.answer(PLAN))
    await waitFor(() => expect(visible(gate())).toContain('12 of 1,480 selected'), WAIT)
    expect(visible(impactLine())).not.toContain('reading')
    expect(asks).toHaveLength(1)
  })
})

describe('runs-submit-27: a context row with nothing read says why in words', () => {
  // QA2-FIX-20 (runs2-03, runs2-11): `— not read` alone still left the why to
  // the hover. The line now says it: a run that kept no reading, or a read
  // that failed, the failure's message on hover.
  // MUTATION: draw `— not read` without `say` and both cases fail.
  it('reads "Issue #512 — not read · this run kept no reading of it", the full reason on hover', async () => {
    serve()
    const Page = await harness()
    render(<Page run={issueRun({ issue_read: null, issue_read_error: null, open_work: null }) as never} />)
    const issue = document.querySelector('[data-ctx="issue"]')!
    const work = document.querySelector('[data-ctx="open-work"]')!
    expect(visible(issue)).toBe('Issue #512 — not read · this run kept no reading of it')
    expect(visible(work)).toBe('Open work — not read · this run kept no reading of it')
    expect(issue.querySelector('[title]')?.getAttribute('title')).toBe('This run was created before runs kept the issue they read.')
    expect(work.querySelector('[title]')?.getAttribute('title')).toMatch(/created before the open-work read/)
  })

  it('says the issue read failed, and why on hover, when the run kept the failure', async () => {
    serve()
    const Page = await harness()
    const message = "tenant 'eng' has no forge credential"
    render(<Page run={issueRun({ issue_read: null, issue_read_error: { code: 'no_forge_credential', message }, open_work: null }) as never} />)
    const issue = document.querySelector('[data-ctx="issue"]')!
    // MUTATION: say 'this run kept no reading of it' whatever the run kept and this fails.
    expect(visible(issue)).toBe('Issue #512 — not read · its read failed')
    expect(issue.querySelector('[title]')?.getAttribute('title')).toBe(`The issue could not be read when the run was created: ${message}`)
  })
})
