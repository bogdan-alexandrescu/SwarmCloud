// THE PLAN EDITOR, MOVED OUT OF Runs.tsx (docs/design/plans-pipeline.md §11,
// lane U5 PLAN-EDITOR-MOVE; frames D:P3b and D:P3c). Red first on main, where
// PlanEditor.tsx does not exist and the decision page has no editor.
//
// WHAT EACH CASE HOLDS:
//   * Remove step is disabled on the last step, with its reason (PlanSpec.steps
//     keeps at least one);
//   * an edit keeps every field the editor does not show: the plan is spread,
//     so `diagrams`, `no_diagram`, `rationale`, `mode` and `overlaps` go back
//     as they came;
//   * a step's rationale is edited in its own field, and a diagram is removed;
//   * the decision bar's Edit opens the editor in place, in the document (the
//     owner's pick on PR 464), and never navigates to the run's page;
//   * Save on the decision page sends the digest of the revision it replaces,
//     and the saved revision is named with its digest and author.
//
// Placeholders only; nothing is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { PlanEditor } from '../PlanEditor'
import type { RunPlan } from '../types'

const WAIT = { timeout: 4000 }
type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null

const RUN = 'run_5e5e0a0b0e0f41a2b3c4'
const ITEM = `run:${RUN}`
const D2 = 'sha256:' + 'ab12'.repeat(16)
const D3 = 'sha256:' + 'ef56'.repeat(16)

const DIAGRAMS = [
  { diagram_id: 'd-flow', kind: 'flow', title: 'The read path', language: 'mermaid', source: 'flowchart TD\n  A-->B', why: 'Shows the route.', explains: { part: 'plan', ref: null } },
  { diagram_id: 'd-state', kind: 'state', title: 'Run states', language: 'mermaid', source: 'stateDiagram-v2\n  [*]-->A', why: 'Shows the states.', explains: { part: 'step', ref: 'read' } },
]

const PLAN = {
  summary: 'Serve the overlaps on the plan read.',
  rationale: 'Plans are approved blind to the merge train.',
  mode: 'workflow',
  estimate: '3 h',
  requirements: ['The read serves overlaps'],
  overlaps: [{ ref: 'example-org/example-api#7', kind: 'issue', action: 'none', note: 'Unrelated.' }],
  diagrams: DIAGRAMS,
  steps: [
    { step_id: 'read', title: 'Serve the read', prompt: 'Add the read.', files: ['api/read.py'], rationale: 'The read is where the page looks.', done_when: ['The read answers'] },
    { step_id: 'page', title: 'Draw it', prompt: 'Draw the overlaps.', rationale: 'The page draws what the read serves.' },
  ],
}

const asPlan = (p: Json) => p as unknown as RunPlan

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('the editor', () => {
  it('disables Remove on the last step and says why', () => {
    render(<PlanEditor plan={asPlan({ ...PLAN, steps: [PLAN.steps[0]] })} busy={false} onCancel={() => undefined} onSave={() => undefined} />)
    const remove = screen.getByRole<HTMLButtonElement>('button', { name: 'Remove step 1' })
    expect(remove.disabled).toBe(true)
    // The reason is the button's description, not a visible sentence.
    const why = screen.getByText(/at least one step/)
    expect(why.hidden).toBe(true)
    expect(remove.getAttribute('aria-describedby')).toBe(why.id)
  })

  it('removes a step when there are two, and keeps every field it does not show', () => {
    const onSave = vi.fn()
    render(<PlanEditor plan={asPlan(PLAN)} busy={false} onCancel={() => undefined} onSave={onSave} />)
    const remove = screen.getByRole<HTMLButtonElement>('button', { name: 'Remove step 2' })
    expect(remove.disabled).toBe(false)
    fireEvent.click(remove)
    // With one step left, its Remove is off.
    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Remove step 1' }).disabled).toBe(true)
    fireEvent.change(screen.getByRole('textbox', { name: 'Summary' }), { target: { value: 'Serve the overlaps, edited.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save the plan' }))
    expect(onSave).toHaveBeenCalledTimes(1)
    const saved = onSave.mock.calls[0]![0] as Json
    expect(saved.summary).toBe('Serve the overlaps, edited.')
    // MUTATION: build the plan from the edited fields alone, without `...plan`.
    expect(saved.diagrams).toEqual(DIAGRAMS)
    expect(saved.rationale).toBe(PLAN.rationale)
    expect(saved.mode).toBe('workflow')
    expect(saved.overlaps).toEqual(PLAN.overlaps)
    const steps = saved.steps as Json[]
    expect(steps.map((s) => s.step_id)).toEqual(['read'])
    expect(steps[0]!.rationale).toBe(PLAN.steps[0]!.rationale)
    expect(steps[0]!.done_when).toEqual(['The read answers'])
  })

  it('edits a step’s rationale in its own field', () => {
    const onSave = vi.fn()
    render(<PlanEditor plan={asPlan(PLAN)} busy={false} onCancel={() => undefined} onSave={onSave} />)
    const step2 = screen.getByRole('group', { name: /2 · page/ })
    fireEvent.change(within(step2).getByRole('textbox', { name: 'Rationale' }), { target: { value: 'Because the page draws it.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save the plan' }))
    const steps = (onSave.mock.calls[0]![0] as Json).steps as Json[]
    expect(steps[1]!.rationale).toBe('Because the page draws it.')
    expect(steps[0]!.rationale).toBe(PLAN.steps[0]!.rationale)
  })

  it('removes a diagram and keeps the others', () => {
    const onSave = vi.fn()
    render(<PlanEditor plan={asPlan(PLAN)} busy={false} onCancel={() => undefined} onSave={onSave} />)
    fireEvent.click(screen.getByRole('button', { name: 'Remove diagram The read path' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save the plan' }))
    expect((onSave.mock.calls[0]![0] as Json).diagrams).toEqual([DIAGRAMS[1]])
  })

  it('saves a plan with no diagrams exactly as it came', () => {
    const onSave = vi.fn()
    const bare = Object.fromEntries(Object.entries(PLAN).filter(([k]) => k !== 'diagrams'))
    render(<PlanEditor plan={asPlan({ ...bare, no_diagram: 'a one-line change' })} busy={false} onCancel={() => undefined} onSave={onSave} />)
    expect(screen.queryByRole('list', { name: 'Diagrams' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Save the plan' }))
    const saved = onSave.mock.calls[0]![0] as Json
    expect('diagrams' in saved).toBe(false)
    expect(saved.no_diagram).toBe('a one-line change')
  })
})

// ---------------------------------------------------------------------------
// On the decision page
// ---------------------------------------------------------------------------

function item(over: Json = {}): Json {
  return {
    approval_id: ITEM,
    tenant_id: 'eng',
    kind: 'plan',
    subject: { run_id: RUN, issue: 'example-org/example-api#12' },
    digest: D2,
    summary: 'example-org/example-api#12: serve overlaps on the plan read',
    approvers: 'members',
    state: 'pending',
    requested_at: new Date(Date.now() - 3_600_000).toISOString(),
    expires_at: new Date(Date.now() + 47 * 3_600_000).toISOString(),
    may_decide: { approve: true, reject: true, edit: true, comment: true, why: null, deciders: ['members'] },
    ...over,
  }
}

function runRead(over: Json = {}): Json {
  return {
    id: RUN,
    state: 'PLANNED',
    plan: PLAN,
    plan_digest: D2,
    plan_revision: 2,
    plan_edited_by: null,
    plan_revisions: [{ revision: 2, digest: D2, replaces: null, author: { kind: 'planner', by: 'planner' }, made_by: 'plan', changed: [], at: new Date().toISOString() }],
    plan_history_complete: true,
    plan_thread: [],
    may_decide: { approve: true, reject: true, edit: true, comment: true, why: null, deciders: ['members'] },
    ...over,
  }
}

const EDIT = `/v1/runs/${RUN}/plan:edit`

function routes(extra?: (m: string, url: string, body: unknown) => Reply) {
  return (m: string, url: string, body: unknown): Reply => {
    const hit = extra?.(m, url, body) ?? null
    if (hit !== null) return hit
    const path = url.split('?')[0]!
    if (m === 'GET' && path === '/v1/approvals') return { status: 200, body: { approvals: [item()], tenant_id: 'eng' } }
    if (m === 'GET' && path === `/v1/approvals/${encodeURIComponent(ITEM)}`) return { status: 200, body: { approval: item(), run: { id: RUN, state: 'PLANNED', plan: PLAN } } }
    if (m === 'GET' && path === `/v1/runs/${RUN}`) return { status: 200, body: { run: runRead() } }
    return null
  }
}

const go = vi.fn()

async function mount(handler: (m: string, url: string, body: unknown) => Reply) {
  const calls = serve(handler)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  go.mockReset()
  const { ApprovalsScreen } = await import('../Approvals')
  render(<ApprovalsScreen view={`approval=${encodeURIComponent(ITEM)}`} go={go} />)
  await screen.findByRole('heading', { name: 'Approvals', level: 1 }, WAIT)
  return calls
}

/** The decision bar's Edit. */
const barEdit = async () => within(await screen.findByTestId('pd-bar', undefined, WAIT)).findByRole('button', { name: 'Edit' }, WAIT)

describe('on the decision page', () => {
  it('opens the editor in place from the bar’s Edit, and Save sends the digest it replaces', async () => {
    const saved = runRead({
      plan: { ...PLAN, summary: 'Serve the overlaps, edited.' },
      plan_digest: D3,
      plan_revision: 3,
      plan_edited_by: 'a@example.com',
    })
    const calls = await mount(routes((m, url) => (m === 'POST' && url.split('?')[0] === EDIT ? { status: 200, body: { run: saved } } : null)))
    const doc = await screen.findByTestId('pd-doc', undefined, WAIT)
    // Closed, the document offers no second button: the bar's Edit is the way in.
    expect(within(doc).queryByRole('form', { name: 'Edit the plan' })).toBeNull()
    fireEvent.click(await barEdit())
    // MUTATION: Edit links to the run's page again (go called, no form here).
    const form = await within(doc).findByRole('form', { name: 'Edit the plan' }, WAIT)
    expect(go).not.toHaveBeenCalled()
    fireEvent.change(within(form).getByRole('textbox', { name: 'Summary' }), { target: { value: 'Serve the overlaps, edited.' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Save the plan' }))
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST' && c.url.split('?')[0] === EDIT)).toHaveLength(1), WAIT)
    const body = calls.find((c) => c.method === 'POST' && c.url.split('?')[0] === EDIT)!.body as Json
    // MUTATION: send the run's newest digest, or none.
    expect(body.plan_digest).toBe(D2)
    const plan = body.plan as Json
    expect(plan.summary).toBe('Serve the overlaps, edited.')
    expect(plan.diagrams).toEqual(DIAGRAMS)
    expect(plan.rationale).toBe(PLAN.rationale)
    // D:P3c: the saved revision is named with its digest and author.
    const tick = await screen.findByTestId('pe-saved', undefined, WAIT)
    expect(visible(tick)).toContain('Revision 3')
    expect(within(tick).getByTitle(D3).getAttribute('data-digest')).toBe(D3)
    expect(visible(tick)).toContain('a@example.com')
  })

  it('offers no editor to a member who may not edit', async () => {
    await mount(
      routes((m, url) =>
        m === 'GET' && url.split('?')[0] === `/v1/runs/${RUN}`
          ? { status: 200, body: { run: runRead({ may_decide: { approve: false, reject: false, edit: false, comment: true, why: 'not a decider', deciders: ['owners'] } }) } }
          : null,
      ),
    )
    const doc = await screen.findByTestId('pd-doc', undefined, WAIT)
    await screen.findByTestId('pd-summary', undefined, WAIT)
    const bar = await screen.findByTestId('pd-bar', undefined, WAIT)
    expect(within(bar).getByRole('button', { name: 'Approve' })).toBeTruthy()
    expect(within(bar).queryByRole('button', { name: 'Edit' })).toBeNull()
    expect(within(doc).queryByRole('form', { name: 'Edit the plan' })).toBeNull()
  })
})
