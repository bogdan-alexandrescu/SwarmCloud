// JEV'S JUDGMENT ON THE APPROVAL SCREEN (docs/design/agent-quality/jev-contract.md
// section 13, lane 7; part of #2688). Its own file because the approvals
// screen is mounted as automate.approvals.test.tsx mounts it, against the live
// client with fresh modules, and contract.jev.test.tsx turns the fixture
// build's submit off with a module mock that a fresh import does not keep.
//
// WHAT EACH CASE HOLDS:
//   * `contract_acceptance` (the record `attemptstate.contract_verdict` and
//     `contractreask` store) is drawn as a card labelled as Jev's judgment:
//     each verdict with the item it cites -- the hunk of a changed file at the
//     judged commit, or a mechanical result -- its reason and probability;
//   * a `not_run` or a `cannot_judge` the control plane re-asked three times
//     says so, and says it is now the person's; a step still being re-asked
//     says which try it is on;
//   * Jev's met never reads as an approval: the card says it is evidence;
//   * a detail with no record draws no card;
//   * the cited hunk's TEXT, as the record stored it, is drawn under its path
//     (the owner's pick on PR #3255), and a hunk with no stored text says so;
//   * a `contract:<task_id>` item -- a step waiting on its owner, projected
//     from its task -- draws the card and no Approve or Reject, only the
//     step's page; the task detail draws the same card.
//
// Nothing here is token-shaped: digests and commits are built at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'

import { ContractAcceptanceCard, contractAcceptance } from '../ContractJev'
import { serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }
type Json = Record<string, unknown>

const DIGEST = 'ab'.repeat(32)
const COMMIT = 'c0ffee1' + '2'.repeat(33)
const CALL = `call-${'7'.repeat(8)}`

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

const hoursFromNow = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()

function acceptance(over: Json = {}): Json {
  return {
    status: 'judgment_pending', waiting_on: 'owner', owner_reason: 'send_back_cap',
    attempt_id: 'att_1', generation: 3, contract_id: 'wc-example', revision: 2, digest: DIGEST,
    commit: COMMIT, outcome: 'judged', reason: null, call_id: CALL,
    verdicts: [
      { requirement_id: 'REQ-1', verdict: 'met', reason: 'satisfied', evidence_item: 'i1', p: 0.91 },
      { requirement_id: 'REQ-2', verdict: 'unmet', reason: 'partially_done', evidence_item: 'i0', p: 0.78 },
      { requirement_id: 'REQ-3', verdict: 'met', reason: 'satisfied', evidence_item: 'm0', p: 0.88 },
    ],
    changed: ['apps/swarm-api/swarm_api/leases.py', 'tests/unit/control_plane/test_leases.py'],
    mechanical: [{ kind: 'unit_tests', passed: true, named: ['tests/unit/control_plane/test_leases.py'] }, { kind: 'ci', passed: null }],
    reask: null, at: hoursFromNow(-1),
    ...over,
  }
}

const REASKED = {
  tries: 3, first_at: hoursFromNow(-1), next_at: hoursFromNow(-0.2), deadline: hoursFromNow(-0.5),
  last: { try: 3, at: hoursFromNow(-0.6), outcome: 'not_run', reason: 'skipped:budget_monthly', call_id: null },
}

function detail(acc: Json | null): Json {
  return {
    approval: {
      approval_id: 'apr_contract', tenant_id: 'eng', kind: 'merge',
      subject: { run_id: 'run_8f2a', issue: 'example-org/example-api#2364' },
      digest: { head_sha: COMMIT, verdict: 'MERGE' },
      summary: 'example-org/example-api#2364: pull request #2392 is green',
      approvers: 'owner_only', state: 'pending', requested_at: hoursFromNow(-2), expires_at: hoursFromNow(60),
      may_decide: { approve: true, reject: true, why: null, deciders: ['owner_only'] },
    },
    merge: { green_sha: COMMIT, pull_request: { number: 2392, url: 'https://github.com/example-org/example-api/pull/2392' }, requirements_met: true, requirements_unmet: [], checks: { passed: 4, failed: 0, pending: 0 } },
    ...(acc === null ? {} : { contract_acceptance: acc }),
  }
}

const HUNK = '@@ -88,3 +88,4 @@ def cancel(lease):\n     if lease.released_at is not None:\n+    capacity.release(lease.reservation)'

function contractDetail(acc: Json): Json {
  return {
    approval: {
      approval_id: 'contract:task_7', tenant_id: 'eng', kind: 'contract',
      subject: { task_id: 'task_7', step_id: 'implement', contract_id: 'wc-example', revision: 2, commit: COMMIT, owner_reason: 'send_back_cap' },
      digest: DIGEST, summary: 'implement: Release the lease on cancel.',
      approvers: ['alice@example.com'], state: 'pending', requested_at: hoursFromNow(-1), expires_at: null, projected: true,
    },
    contract_acceptance: acc,
  }
}

async function openApproval(body: Json) {
  serve((m, url) => {
    const path = url.split('?')[0]!
    if (m === 'GET' && path === '/v1/approvals') return { status: 200, body: { approvals: [body.approval], tenant_id: 'eng' } }
    if (m === 'GET' && path.startsWith('/v1/approvals/')) return { status: 200, body }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { ApprovalsScreen } = await import('../Approvals')
  const id = String((body.approval as Json).approval_id)
  render(<ApprovalsScreen view={`approval=${id}`} go={vi.fn()} />)
  return waitFor(() => {
    const d = screen.getByTestId('ap-detail')
    expect(d.tagName, visible(d)).toBe('SECTION')
    return d
  }, WAIT)
}

describe('the approval screen', () => {
  it('draws each verdict with the hunk it cites, labelled as Jev’s judgment', async () => {
    const d = await openApproval(detail(acceptance()))
    const card = await within(d).findByRole('region', { name: 'Jev’s judgment of the work contract' }, WAIT)
    const rows = [...card.querySelectorAll<HTMLElement>('[data-requirement]')]
    expect(rows.map((r) => [r.getAttribute('data-requirement'), r.getAttribute('data-verdict')])).toEqual([
      ['REQ-1', 'met'], ['REQ-2', 'unmet'], ['REQ-3', 'met'],
    ])
    // MUTATION: drop the cited item, and the person cannot check what the verdict rests on.
    expect(visible(rows[0]!)).toContain('tests/unit/control_plane/test_leases.py')
    expect(visible(rows[1]!)).toContain('apps/swarm-api/swarm_api/leases.py')
    expect(visible(rows[1]!)).toContain(COMMIT.slice(0, 7))
    expect(visible(rows[1]!)).toContain('it is partly done')
    expect(visible(rows[1]!)).toContain('78%')
    expect(visible(rows[2]!)).toContain('unit tests passed')
    for (const r of rows) expect(visible(r)).toContain('Jev')
    // The owner reason, in words: the send-back cap handed it to the person.
    expect(visible(card)).toContain('sent back twice')
    // Rule 1: a model judgment is not an authorization.
    expect(visible(card)).toContain('Jev’s verdicts are evidence for your decision; they approve nothing')
    expect(visible(card)).toContain(CALL)
  })

  it('says a not_run after the control plane’s three re-asks as such', async () => {
    const d = await openApproval(detail(acceptance({ outcome: 'not_run', reason: 'skipped:budget_calls', verdicts: [], owner_reason: 'reask_exhausted', reask: REASKED, call_id: null })))
    const card = await within(d).findByRole('region', { name: 'Jev’s judgment of the work contract' }, WAIT)
    const status = within(card).getByTestId('cj-status')
    expect(status.getAttribute('data-status')).toBe('not_run')
    expect(visible(status)).toContain('Jev did not judge this step')
    expect(visible(status)).toContain('three re-asks')
    expect(visible(card)).toContain('skipped:budget_monthly')
    expect(visible(card)).toContain('skipped:budget_calls')
    // A not_run is never drawn as a verdict, met or unmet.
    expect(card.querySelectorAll('[data-verdict]')).toHaveLength(0)
  })

  it('says a cannot_judge after the three re-asks as such, per requirement', async () => {
    const acc = acceptance({
      owner_reason: 'reask_exhausted', reask: { ...REASKED, last: { ...REASKED.last, outcome: 'judged', reason: null } },
      verdicts: [
        { requirement_id: 'REQ-1', verdict: 'met', reason: 'satisfied', evidence_item: 'i1', p: 0.91 },
        { requirement_id: 'REQ-2', verdict: 'cannot_judge', reason: 'evidence_missing', evidence_item: null, p: 0.4 },
      ],
    })
    const d = await openApproval(detail(acc))
    const card = await within(d).findByRole('region', { name: 'Jev’s judgment of the work contract' }, WAIT)
    const row = card.querySelector<HTMLElement>('[data-requirement="REQ-2"]')!
    expect(row.getAttribute('data-verdict')).toBe('cannot_judge')
    expect(visible(row)).toContain('Jev could not judge it')
    expect(visible(row)).toContain('after the control plane’s three re-asks')
    expect(visible(row)).toContain('cites nothing')
  })

  it('says a step still waiting on a re-ask, with the try it is on', async () => {
    const d = await openApproval(detail(acceptance({
      waiting_on: 'reask', owner_reason: null, outcome: 'not_run', reason: 'timeout', verdicts: [],
      reask: { tries: 1, first_at: hoursFromNow(-0.2), next_at: hoursFromNow(0.1), deadline: hoursFromNow(0.3), last: null },
    })))
    const card = await within(d).findByRole('region', { name: 'Jev’s judgment of the work contract' }, WAIT)
    expect(visible(within(card).getByTestId('cj-status'))).toContain('re-asked 1 of 3 times')
  })

  it('draws the text of each cited hunk under its path, as the record stored it', async () => {
    const d = await openApproval(detail(acceptance({ hunks: { i0: HUNK } })))
    const card = await within(d).findByRole('region', { name: 'Jev’s judgment of the work contract' }, WAIT)
    const cited = card.querySelector<HTMLElement>('[data-requirement="REQ-2"]')!
    // MUTATION: drawing the path alone, so the person cannot read what Jev read.
    const hunk = within(cited).getByTestId('cj-hunk')
    expect(hunk.tagName).toBe('PRE')
    expect(hunk.textContent).toBe(HUNK)
    expect(hunk.getAttribute('aria-label')).toContain('apps/swarm-api/swarm_api/leases.py')
    // A cited hunk the record keeps no text for says so, never another's text.
    const bare = card.querySelector<HTMLElement>('[data-requirement="REQ-1"]')!
    expect(within(bare).queryByTestId('cj-hunk')).toBeNull()
    expect(visible(bare)).toContain('the record keeps no text for it')
    expect(contractAcceptance(acceptance({ hunks: { i0: HUNK, x9: 'no', i1: 7 } }))!.hunks).toEqual({ i0: HUNK })
  })

  it('draws a step waiting on its owner with its judgment, no verb, and the step to open', async () => {
    const d = await openApproval(contractDetail(acceptance({ hunks: { i0: HUNK } })))
    const card = await within(d).findByRole('region', { name: 'Jev’s judgment of the work contract' }, WAIT)
    expect([...card.querySelectorAll('[data-requirement]')]).toHaveLength(3)
    // MUTATION: offering Approve or Reject on an id the inbox answers 404.
    expect(within(d).queryByRole('button', { name: /approve/i })).toBeNull()
    expect(within(d).queryByRole('button', { name: /reject/i })).toBeNull()
    const bar = within(d).getByTestId('ap-contract-step')
    expect(visible(bar)).toContain('Not decided in this inbox')
    expect(within(bar).getByRole('link', { name: /open the step/i }).getAttribute('href')).toContain('task_7')
    expect(visible(d)).toMatch(/it waits for its owner/i)
  })

  it('draws no card where the detail carries no acceptance record', async () => {
    const d = await openApproval(detail(null))
    await within(d).findByRole('region', { name: 'The request' }, WAIT)
    expect(within(d).queryByRole('region', { name: 'Jev’s judgment of the work contract' })).toBeNull()
    expect(contractAcceptance({ status: 'met' })).toBeNull()
    expect(contractAcceptance('met')).toBeNull()
  })
})

describe('the task detail', () => {
  it('draws the same card from the task’s contract_acceptance, and nothing without one', () => {
    const view = render(<ContractAcceptanceCard raw={acceptance({ hunks: { i0: HUNK } })} />)
    const card = screen.getByRole('region', { name: 'Jev’s judgment of the work contract' })
    // MUTATION: the task detail not drawing the record it is served.
    expect([...card.querySelectorAll('[data-verdict]')].map((r) => r.getAttribute('data-verdict'))).toEqual(['met', 'unmet', 'met'])
    expect(within(card).getByTestId('cj-hunk').textContent).toBe(HUNK)
    view.unmount()
    const none = render(<ContractAcceptanceCard raw={undefined} />)
    expect(none.container.innerHTML).toBe('')
  })
})

describe('the fixture build', () => {
  it('serves each Jev judgment the review renders, by address, with the inbox still empty', async () => {
    vi.resetModules()
    const { ApprovalsScreen } = await import('../Approvals')
    const { FIXTURE_CONTRACT_APPROVALS } = await import('../api')
    // What tells the three apart: the verdicts each row carries, and whether Jev ran.
    const want: Record<string, { status: string; verdicts: string[] }> = {
      apr_fixture_contract_judged: { status: 'judgment_pending', verdicts: ['met', 'unmet', 'met'] },
      apr_fixture_contract_not_run: { status: 'not_run', verdicts: [] },
      apr_fixture_contract_cannot_judge: { status: 'judgment_pending', verdicts: ['met', 'cannot_judge'] },
    }
    for (const id of FIXTURE_CONTRACT_APPROVALS) {
      const view = render(<ApprovalsScreen view={`approval=${id}`} go={vi.fn()} />)
      const card = await screen.findByRole('region', { name: 'Jev’s judgment of the work contract' }, WAIT)
      // MUTATION: serve one record for all three, and the review measures one case.
      expect(within(card).getByTestId('cj-status').getAttribute('data-status')).toBe(want[id]!.status)
      expect([...card.querySelectorAll('[data-verdict]')].map((r) => r.getAttribute('data-verdict'))).toEqual(want[id]!.verdicts)
      expect(visible(card)).toContain('they approve nothing')
      view.unmount()
    }
  })
})

