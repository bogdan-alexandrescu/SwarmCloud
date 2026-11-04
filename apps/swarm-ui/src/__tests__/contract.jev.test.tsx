// JEV IN THE WORK CONTRACT, SHOWN TO THE PERSON WHO DECIDES
// (docs/design/agent-quality/jev-contract.md section 13, lane 7; part of #2688).
//
// WHAT EACH CASE HOLDS:
//   * a 422 whose `detail` is section 4.5's refusal (`reason`
//     work_contract_not_enforceable, `detail.jev.requirements`) is drawn on the
//     task form and on the workflow form with ONE line per refused
//     requirement: its id, Jev's class and reason code in words and its
//     probability, labelled as Jev's review -- never the detail as a JSON dump
//     or as `key value` facts;
//   * a 422 without `detail.jev` is not drawn as Jev's;
//   * the approval screen draws `contract_acceptance` (the record
//     `attemptstate.contract_verdict` and `contractreask` store) as a card
//     labelled as Jev's judgment: each verdict with the item it cites -- a
//     changed file at the judged commit, or a mechanical result -- and its
//     reason and probability; a `not_run` or a `cannot_judge` the control
//     plane re-asked three times says so, and says it is now the person's;
//   * Jev's met never reads as an approval: the card says it is evidence;
//   * the verdict chips take the ok, bad and warn hues in both themes, and the
//     rows stack at 390px and sit in two columns at 1280 and 1440.
//
// The bodies are swarm-api's shapes (contractjudge.refusal_detail,
// contractreask's ACCEPTANCE_FIELD record). Nothing here is token-shaped:
// digests and commits are built at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), USE_FIXTURES: false }))
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import { AcceptanceVerdicts, ContractRefusalPanel, contractAcceptance, contractRefusal } from '../ContractJev'
import { SubmitScreen } from '../Submit'
import { SubmitWorkflowScreen, refusalOf } from '../SubmitWorkflow'
import type { CascadeEnv } from './cssgate'
import { painted, stateHueIn, THEMES } from './marks'
import { serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const PHONE: CascadeEnv = { width: 390 }
const LAPTOP: CascadeEnv = { width: 1280 }
const WIDE: CascadeEnv = { width: 1440 }

type Json = Record<string, unknown>

const DIGEST = 'ab'.repeat(32)
const COMMIT = 'c0ffee1' + '2'.repeat(33)
const CALL = `call-${'7'.repeat(8)}`

const REFUSAL = {
  code: 'validation_failed',
  message: 'contract wc-example is strict, and Jev found requirements no worker could enforce: REQ-2, REQ-4',
  detail: {
    reason: 'work_contract_not_enforceable',
    contract_id: 'wc-example',
    revision: 1,
    digest: DIGEST,
    unenforced: [],
    jev: {
      call_id: CALL,
      backend: 'internal-jev',
      requirements: [
        { id: 'REQ-2', class: 'unenforceable', reason: 'vague_outcome', p: 0.83 },
        { id: 'REQ-4', class: 'unenforceable', reason: 'depends_on_a_later_event', p: 0.71 },
      ],
    },
  },
}

const realFetch = globalThis.fetch

function refuseWrite(path: string, reply: { status: number; body: unknown }) {
  const prior = globalThis.fetch
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if ((init?.method ?? 'GET') === 'POST' && url.startsWith(path)) {
      return new Response(JSON.stringify(reply.body), { status: reply.status, headers: JSON_HEADERS })
    }
    return prior(input, init)
  }) as unknown as typeof fetch
}

afterEach(() => {
  globalThis.fetch = realFetch
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

function expectJevRefusal(root: HTMLElement) {
  const panel = root.querySelector<HTMLElement>('[data-testid="cj-refusal"]')
  expect(panel, 'no Jev refusal panel').not.toBeNull()
  expect(visible(panel)).toContain('Jev’s review')
  const rows = [...panel!.querySelectorAll<HTMLElement>('[data-requirement]')]
  // MUTATION: draw only the message, and the person cannot tell which requirement to rewrite.
  expect(rows.map((r) => r.getAttribute('data-requirement'))).toEqual(['REQ-2', 'REQ-4'])
  expect(visible(rows[0]!)).toContain('nothing observable when the step ends decides it')
  expect(visible(rows[0]!)).toContain('the outcome it asks for is vague')
  expect(visible(rows[0]!)).toContain('83%')
  expect(visible(rows[1]!)).toContain('it depends on something that happens after the step')
  expect(visible(rows[1]!)).toContain('71%')
  // Never the detail as JSON or as raw facts.
  expect(root.textContent).not.toContain('"requirements"')
  expect(root.textContent).not.toContain('call id')
  expect(root.textContent).not.toContain(DIGEST)
  expect(root.textContent).toContain('Nothing was created')
}

describe('the submission refusal', () => {
  it('reads section 4.5’s detail, and nothing that lacks Jev’s part', () => {
    const r = contractRefusal(REFUSAL.detail)
    expect(r?.contractId).toBe('wc-example')
    expect(r?.requirements.map((q) => [q.id, q.cls, q.reason, q.p])).toEqual([
      ['REQ-2', 'unenforceable', 'vague_outcome', 0.83],
      ['REQ-4', 'unenforceable', 'depends_on_a_later_event', 0.71],
    ])
    // The mechanical compile's refusal is the platform's, not Jev's.
    expect(contractRefusal({ reason: 'work_contract_not_enforceable', unenforced: ['judgment'] })).toBeNull()
    expect(contractRefusal({ field: 'metadata.work_contract', work_contract: 'work_contract_invalid' })).toBeNull()
    expect(contractRefusal(null)).toBeNull()
  })

  it('keeps an unknown class or reason code verbatim, never as a known one', () => {
    const r = contractRefusal({ ...REFUSAL.detail, jev: { requirements: [{ id: 'REQ-9', class: 'novel', reason: 'later_code', p: 2 }] } })!
    const { container } = render(<ContractRefusalPanel refusal={r} />)
    const row = container.querySelector<HTMLElement>('[data-requirement="REQ-9"]')!
    expect(visible(row)).toContain('novel')
    expect(visible(row)).toContain('later_code')
    // A probability outside [0, 1] is not drawn as a percentage.
    expect(visible(row)).not.toContain('%')
  })

  it('draws Jev’s reasons per requirement on the task form', async () => {
    const { container } = render(<SubmitScreen />)
    const go = (await screen.findByRole('button', { name: 'Start one agent' }, WAIT)) as HTMLButtonElement
    fireEvent.click(container.querySelector<HTMLInputElement>('input[name="runner-profile"][value="mock"]')!)
    refuseWrite('/v1/tasks', { status: 422, body: REFUSAL })
    fireEvent.click(go)
    await waitFor(() => expectJevRefusal(container), WAIT)
    // The panel names the contract; the "did not say which field" line would send the person to a field.
    expect(container.textContent).not.toContain('It did not say which field')
  })

  it('draws Jev’s reasons per requirement on the workflow form', async () => {
    const { container } = render(<SubmitWorkflowScreen />)
    const go = (await screen.findByRole('button', { name: 'Submit this workflow' }, WAIT)) as HTMLButtonElement
    const select = within(container.querySelector<HTMLElement>('.wfb-step')!).getByRole('combobox', { name: 'step-1 runner' })
    fireEvent.change(select, { target: { value: 'mock' } })
    refuseWrite('/v1/workflows', { status: 422, body: REFUSAL })
    fireEvent.click(go)
    await waitFor(() => expectJevRefusal(container), WAIT)
  })

  it('leaves Jev’s part out of the workflow refusal’s facts', () => {
    const facts = refusalOf({ kind: 'invalid', httpStatus: 422, code: 'validation_failed', message: REFUSAL.message, detail: REFUSAL.detail }, ['step-1']).facts
    // MUTATION: keep `jev` and `digest` as facts, and the panel's lines are said again as a dump.
    expect(facts.map((f) => f.key)).not.toContain('jev')
    expect(facts.map((f) => f.key)).not.toContain('digest')
  })
})

// --- the approval screen -------------------------------------------------------

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
  render(<ApprovalsScreen view="approval=apr_contract" go={vi.fn()} />)
  return waitFor(() => {
    const d = screen.getByTestId('ap-detail')
    expect(d.tagName).toBe('SECTION')
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

  it('draws no card where the detail carries no acceptance record', async () => {
    const d = await openApproval(detail(null))
    await within(d).findByRole('region', { name: 'What asked for it' }, WAIT)
    expect(within(d).queryByRole('region', { name: 'Jev’s judgment of the work contract' })).toBeNull()
    expect(contractAcceptance({ status: 'met' })).toBeNull()
    expect(contractAcceptance('met')).toBeNull()
  })
})

describe('the look', () => {
  function verdicts() {
    const acc = contractAcceptance(acceptance({
      verdicts: [
        { requirement_id: 'REQ-1', verdict: 'met', reason: 'satisfied', evidence_item: 'i1', p: 0.91 },
        { requirement_id: 'REQ-2', verdict: 'unmet', reason: 'partially_done', evidence_item: 'i0', p: 0.78 },
        { requirement_id: 'REQ-3', verdict: 'cannot_judge', reason: null, evidence_item: null, p: 0 },
      ],
    }))!
    return render(<AcceptanceVerdicts acceptance={acc} />).container
  }

  it('gives met, unmet and cannot_judge the ok, bad and warn hues in both themes', () => {
    const root = verdicts()
    const chip = (id: string) => root.querySelector<HTMLElement>(`[data-requirement="${id}"] .cj-chip`)!
    for (const theme of THEMES) {
      expect(stateHueIn(painted(chip('REQ-1'), 'color', WIDE), theme)).toBe('--ok')
      expect(stateHueIn(painted(chip('REQ-2'), 'color', WIDE), theme)).toBe('--bad')
      expect(stateHueIn(painted(chip('REQ-3'), 'color', WIDE), theme)).toBe('--warn')
    }
  })

  it('stacks a verdict row at 390px and keeps two columns at 1280 and 1440', () => {
    const row = verdicts().querySelector<HTMLElement>('.cj-row')!
    expect(painted(row, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
    expect(painted(row, 'grid-template-columns', LAPTOP)).toBe('minmax(7rem, max-content) minmax(0, 1fr)')
    expect(painted(row, 'grid-template-columns', WIDE)).toBe('minmax(7rem, max-content) minmax(0, 1fr)')
    // A long path wraps rather than pushing the phone sideways.
    expect(painted(row.querySelector<HTMLElement>('.cj-cite')!, 'overflow-wrap', PHONE)).toBe('anywhere')
  })
})
