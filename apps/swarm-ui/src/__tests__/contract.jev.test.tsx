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
//   * (the approval screen's cases are in contract.jev.approval.test.tsx);
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
import { visible } from './repofixture'

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const PHONE: CascadeEnv = { width: 390 }
const LAPTOP: CascadeEnv = { width: 1280 }
const WIDE: CascadeEnv = { width: 1440 }

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

  it('has the fixture build refuse the one prompt the review submits, and only that one', async () => {
    const { FIXTURE_CONTRACT_REFUSAL_PROMPT, fixtureContractRefusal } = await import('../api')
    const refused = fixtureContractRefusal({ runner_profile: 'mock', input: { prompt: FIXTURE_CONTRACT_REFUSAL_PROMPT } })
    expect(contractRefusal(refused?.detail)?.requirements.map((r) => r.id)).toEqual(['REQ-2', 'REQ-4'])
    expect(fixtureContractRefusal({ runner_profile: 'mock', input: { prompt: 'Fix the lease release path' } })).toBeNull()
  })

  it('leaves Jev’s part out of the workflow refusal’s facts', () => {
    const facts = refusalOf({ kind: 'invalid', httpStatus: 422, code: 'validation_failed', message: REFUSAL.message, detail: REFUSAL.detail }, ['step-1']).facts
    // MUTATION: keep `jev` and `digest` as facts, and the panel's lines are said again as a dump.
    expect(facts.map((f) => f.key)).not.toContain('jev')
    expect(facts.map((f) => f.key)).not.toContain('digest')
  })
})

describe('the look', () => {
  function verdicts() {
    const acc = contractAcceptance({
      status: 'judgment_pending', waiting_on: 'owner', owner_reason: 'send_back_cap', outcome: 'judged',
      commit: COMMIT, revision: 2, call_id: CALL, reask: null,
      changed: ['apps/swarm-api/swarm_api/leases.py', 'tests/unit/control_plane/test_leases.py'],
      mechanical: [{ kind: 'unit_tests', passed: true }],
      verdicts: [
        { requirement_id: 'REQ-1', verdict: 'met', reason: 'satisfied', evidence_item: 'i1', p: 0.91 },
        { requirement_id: 'REQ-2', verdict: 'unmet', reason: 'partially_done', evidence_item: 'i0', p: 0.78 },
        { requirement_id: 'REQ-3', verdict: 'cannot_judge', reason: null, evidence_item: null, p: 0 },
      ],
    })!
    return render(<AcceptanceVerdicts acceptance={acc} />).container
  }

  it('gives met, unmet and cannot_judge the ok, bad and warn hues in both themes', () => {
    const root = verdicts()
    const chip = (id: string) => root.querySelector<HTMLElement>(`[data-requirement="${id}"] .cj-chip`)!
    for (const theme of THEMES) {
      expect(stateHueIn(painted(chip('REQ-1'), 'background-color', WIDE), theme)).toBe('--ok')
      expect(stateHueIn(painted(chip('REQ-2'), 'background-color', WIDE), theme)).toBe('--bad')
      expect(stateHueIn(painted(chip('REQ-3'), 'background-color', WIDE), theme)).toBe('--warn')
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
