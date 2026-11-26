// DIAGRAMS ON THE DECISION PAGE (docs/design/plans-pipeline.md §2.5, §6.2,
// §6.4, §6.5; lane U8 DIAGRAMS-ON-PAGE). Red first: PinnedDiagrams.tsx does
// not exist yet, and the thread draws no diagram entry, no Ask for a diagram
// and no Pin to the plan.
//
// WHAT EACH CASE HOLDS:
//   * a revision with two diagrams draws one at a time on the "Pinned to the
//     plan" strip and names both on its tabs; the planner's opening message
//     names each diagram with a link to its tab;
//   * a revision with none has no strip, and the opening message carries the
//     plan's `no_diagram` line;
//   * a diagram entry in the thread is not on the strip until Pin to the plan
//     returns revision N+1; then it is, and the entry links to its tab;
//   * a member below the edit level sees no Ask for a diagram (Q6) and no Pin;
//   * a run planned with diagrams off shows the off line naming its level and
//     no Ask for a diagram;
//   * at 390 pixels the strip's tabs wrap inside the column and the diagram
//     is drawn in its tall layout;
//   * an ask posts the shown digest and the part's anchor, and its waiting
//     entry says it is drawing; the cap's refusal disables the ask with the
//     server's sentence.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { visible } from './repofixture'
import { painted } from './marks'
import type { CascadeEnv } from './cssgate'
import { D1, D2, PLAN, REV1, WAIT, at, mount, routes, runRead, sent, RUN, type Handler, type Json } from './planthreadfixture'

// Mermaid is not drawn here (diagram.view.test.tsx holds the renderer): the
// strip and the thread are tested on which diagram they hand it.
vi.mock('../DiagramView', () => ({
  default: ({ title }: { title: string }) => <figure data-testid="dv" aria-label={title} />,
}))

const ADD = `/v1/runs/${RUN}/plan:add-diagram`
const ASK = `/v1/runs/${RUN}/plan:diagram`

const FLOW = {
  diagram_id: 'd-overlap-flow',
  kind: 'flow',
  title: 'How the run read gathers overlaps',
  language: 'mermaid',
  source: 'flowchart TD\n  A[run read] --> B[overlap refs]',
  why: 'Shows where the overlap read joins the run read.',
  explains: { part: 'plan' },
}
const SEQ = {
  diagram_id: 'd-bucket-seq',
  kind: 'sequence',
  title: 'Planner reads the train bucket',
  language: 'mermaid',
  source: 'sequenceDiagram\n  planner->>bucket: list',
  why: 'Shows the one read the new binding allows.',
  explains: { part: 'step', ref: 'iam-binding' },
}
const ASKED = {
  diagram_id: 'd-cache-state',
  kind: 'state',
  title: 'Overlap cache per digest',
  language: 'mermaid',
  source: 'stateDiagram-v2\n  [*] --> cold\n  cold --> warm',
  why: 'Shows when the cached overlaps are read again.',
  explains: { part: 'step', ref: 'overlaps-read' },
}

const ON = { diagrams: true, from: 'workspace', read_at: at(-130) }
const PLANNED = { kind: 'event', event: 'planned', at: REV1.at, ref: null, revision: 1, digest: D1, by: 'planner', made_by: 'plan' }
const EDITOR = { approve: true, reject: true, why: null, deciders: ['members'], edit: true, comment: true }
const COMMENTER = { approve: false, reject: false, why: 'approvers only', deciders: ['admins'], edit: false, comment: true }

/** Revision 1 of the run, shown: its plan is `plan`. */
function revisionOne(plan: Json, over: Json = {}): Json {
  return { plan, plan_digest: D1, plan_revision: 1, plan_revisions: [REV1], plan_thread: [PLANNED], plan_settings: ON, ...over }
}

function drawnEntry(over: Json = {}): Json {
  return {
    entry_id: 'pth_diag1',
    kind: 'diagram',
    state: 'drawn',
    anchor: { part: 'step', ref: 'overlaps-read' },
    by: 'review-agent',
    asked_by: 'ana@example.com',
    ask: 'Draw when the cache is read again.',
    text: 'The cache is keyed by the plan digest.',
    on_digest: D1,
    on_revision: 1,
    answered_by_revision: null,
    resolved: false,
    diagram: ASKED,
    cost: { usd: 0.42, task_id: 'task_diag1', model_class: 'sonnet', why_none: null },
    task_id: 'task_diag1',
    diagram_kind: null,
    caps: null,
    at: at(-5),
    ...over,
  }
}

const page = (o: { run: Json; may?: Json; extra?: Handler }) =>
  mount(routes({ item: { digest: D1, may_decide: o.may ?? EDITOR }, run: () => ({ may_decide: o.may ?? EDITOR, ...o.run }), extra: o.extra }))

const doc = () => screen.findByTestId('pd-doc', undefined, WAIT)
const part = async (name: string) => within(await doc()).getByRole('region', { name })
const planned = async () => {
  const summary = await part('Summary')
  return within(summary).getAllByTestId('pt-entry').find((e) => e.dataset.event === 'planned')!
}

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('the Pinned to the plan strip', () => {
  it('draws one of a revision’s two diagrams, names both on tabs, and the opening message links each to its tab', async () => {
    await page({ run: revisionOne({ ...PLAN, diagrams: [FLOW, SEQ] }) })
    const strip = await screen.findByTestId('pp-strip', undefined, WAIT)
    expect(strip.dataset.count).toBe('2')
    expect(strip.dataset.revision).toBe('1')
    const tabs = within(strip).getAllByRole('tab')
    expect(tabs).toHaveLength(2)
    expect(visible(tabs[0]!)).toContain(FLOW.title)
    expect(visible(tabs[1]!)).toContain(SEQ.title)
    // Each tab names what its diagram explains: the plan, or a step.
    expect(visible(tabs[1]!)).toContain('Step 2')
    expect(within(strip).getAllByTestId('dv')).toHaveLength(1)
    expect(within(strip).getByTestId('dv').getAttribute('aria-label')).toBe(FLOW.title)
    expect(tabs[0]!.getAttribute('aria-selected')).toBe('true')

    fireEvent.click(tabs[1]!)
    expect(within(strip).getAllByTestId('dv')).toHaveLength(1)
    expect(within(strip).getByTestId('dv').getAttribute('aria-label')).toBe(SEQ.title)

    // The planner's opening message names both, each with a link to its tab.
    const opening = await planned()
    const flowLink = within(opening).getByRole('link', { name: FLOW.title })
    expect(within(opening).getByRole('link', { name: SEQ.title })).toBeTruthy()
    expect(flowLink.getAttribute('href')).toBe(`#${tabs[0]!.id}`)
    fireEvent.click(flowLink)
    expect(within(strip).getByTestId('dv').getAttribute('aria-label')).toBe(FLOW.title)
    // Every diagram is drawn once, on the strip: the message only names it.
    expect(within(opening).queryByTestId('dv')).toBeNull()
  })

  it('draws no strip for a revision with no diagram, and the opening message carries its no_diagram line', async () => {
    const why = 'a one-line change to terraform/infra/iam.tf'
    await page({ run: revisionOne({ ...PLAN, no_diagram: why }) })
    const opening = await planned()
    expect(visible(opening)).toContain(why)
    expect(screen.queryByTestId('pp-strip')).toBeNull()
    expect(screen.queryByTestId('pp-off')).toBeNull()
    // Ask for a diagram stays offered beside it, to an editor.
    expect(within(await part('Summary')).getByRole('button', { name: 'Ask for a diagram' })).toBeTruthy()
  })
})

describe('Pin to the plan', () => {
  it('puts a thread diagram on the strip only once the pin returns revision N+1, and the entry then links to its tab', async () => {
    const r2 = { ...PLAN, diagrams: [FLOW, ASKED] }
    const { calls } = await page({
      run: revisionOne({ ...PLAN, diagrams: [FLOW] }, { plan_thread: [PLANNED, drawnEntry()] }),
      extra: (m, url) =>
        m === 'POST' && url.split('?')[0] === ADD
          ? { status: 200, body: { run: runRead({ ...revisionOne(r2), plan_digest: D2, plan_revision: 2, plan_thread: [PLANNED, drawnEntry()], may_decide: EDITOR }) } }
          : null,
    })
    const strip = await screen.findByTestId('pp-strip', undefined, WAIT)
    expect(within(strip).getAllByRole('tab')).toHaveLength(1)
    expect(visible(strip)).not.toContain(ASKED.title)

    const s1 = await part('Step 1')
    const entry = within(s1).getAllByTestId('pt-entry').find((e) => e.dataset.kind === 'diagram')!
    // The answer is drawn in its entry: part of the conversation, not of the plan.
    expect(within(entry).getByTestId('dv').getAttribute('aria-label')).toBe(ASKED.title)
    expect(within(entry).queryByRole('link', { name: ASKED.title })).toBeNull()
    fireEvent.click(within(entry).getByRole('button', { name: 'Pin to the plan' }))
    await waitFor(() => expect(sent(calls, 'POST', ADD)).toHaveLength(1), WAIT)
    expect(sent(calls, 'POST', ADD)[0]!.body).toEqual({ plan_digest: D1, from: { entry_id: 'pth_diag1' } })

    await waitFor(() => expect(within(strip).getAllByRole('tab')).toHaveLength(2), WAIT)
    expect(strip.dataset.revision).toBe('2')
    const tab = within(strip).getAllByRole('tab')[1]!
    expect(visible(tab)).toContain(ASKED.title)
    const link = await within(entry).findByRole('link', { name: ASKED.title }, WAIT)
    expect(link.getAttribute('href')).toBe(`#${tab.id}`)
    expect(within(entry).queryByRole('button', { name: 'Pin to the plan' })).toBeNull()
    fireEvent.click(link)
    expect(within(strip).getByTestId('dv').getAttribute('aria-label')).toBe(ASKED.title)
  })
})

describe('who may ask', () => {
  it('offers a member below the edit level no Ask for a diagram and no Pin, only Comment', async () => {
    await page({ run: revisionOne({ ...PLAN, diagrams: [FLOW] }, { plan_thread: [PLANNED, drawnEntry()] }), may: COMMENTER })
    const s1 = await part('Step 1')
    expect(within(s1).getByRole('button', { name: 'Comment' })).toBeTruthy()
    expect(screen.queryAllByRole('button', { name: 'Ask for a diagram' })).toHaveLength(0)
    expect(screen.queryAllByRole('button', { name: 'Pin to the plan' })).toHaveLength(0)
  })

  it('asks with the shown digest and the part’s anchor, and the waiting entry says it is drawing', async () => {
    const waiting = drawnEntry({ entry_id: 'pth_diag2', state: 'drawing', diagram: null, text: '', cost: null, at: at(0) })
    const { calls } = await page({
      run: revisionOne({ ...PLAN, no_diagram: 'a small read change' }),
      extra: (m, url) => (m === 'POST' && url.split('?')[0] === ASK ? { status: 202, body: { entry: waiting } } : null),
    })
    const s1 = await part('Step 1')
    fireEvent.click(within(s1).getByRole('button', { name: 'Ask for a diagram' }))
    fireEvent.change(within(s1).getByRole('textbox', { name: 'Ask for a diagram of Step 1' }), { target: { value: 'Draw when the cache is read again.' } })
    fireEvent.click(within(s1).getByRole('button', { name: 'Ask' }))
    await waitFor(() => expect(sent(calls, 'POST', ASK)).toHaveLength(1), WAIT)
    expect(sent(calls, 'POST', ASK)[0]!.body).toEqual({ plan_digest: D1, anchor: { part: 'step', ref: 'overlaps-read' }, ask: 'Draw when the cache is read again.' })
    const entry = await waitFor(() => {
      const e = within(s1).getAllByTestId('pt-entry').find((x) => x.dataset.kind === 'diagram')
      expect(e).toBeTruthy()
      return e!
    }, WAIT)
    expect(entry.dataset.state).toBe('drawing')
    expect(visible(entry)).toContain('drawing')
  })

  it('disables the ask with the cap’s sentence once the server refuses it', async () => {
    const sentence = 'This run has had its 20 diagram asks.'
    await page({
      run: revisionOne({ ...PLAN, no_diagram: 'a small read change' }),
      extra: (m, url) => (m === 'POST' && url.split('?')[0] === ASK ? { status: 429, body: { code: 'diagram_cap', message: sentence } } : null),
    })
    const s1 = await part('Step 1')
    fireEvent.click(within(s1).getByRole('button', { name: 'Ask for a diagram' }))
    fireEvent.change(within(s1).getByRole('textbox', { name: 'Ask for a diagram of Step 1' }), { target: { value: 'Draw it.' } })
    fireEvent.click(within(s1).getByRole('button', { name: 'Ask' }))
    const alert = await within(s1).findByRole('alert', undefined, WAIT)
    expect(visible(alert)).toContain(sentence)
    expect(within(s1).getByRole('button', { name: 'Ask' })).toHaveProperty('disabled', true)
  })
})

describe('this run’s diagram setting', () => {
  it('shows the off line naming the level that turned diagrams off, with no strip and no ask', async () => {
    await page({ run: revisionOne(PLAN, { plan_settings: { diagrams: false, from: 'schedule', read_at: at(-130) } }) })
    const off = await screen.findByTestId('pp-off', undefined, WAIT)
    expect(off.dataset.from).toBe('schedule')
    expect(visible(off)).toContain('schedule')
    expect(screen.queryByTestId('pp-strip')).toBeNull()
    await part('Step 1')
    expect(screen.queryAllByRole('button', { name: 'Ask for a diagram' })).toHaveLength(0)
  })
})

describe('at 390 pixels', () => {
  const PHONE: CascadeEnv = { width: 390 }
  it('wraps the strip’s tabs inside the column and draws the diagram in its tall layout', async () => {
    await page({ run: revisionOne({ ...PLAN, diagrams: [FLOW, SEQ] }) })
    const strip = await screen.findByTestId('pp-strip', undefined, WAIT)
    expect(painted(within(strip).getByRole('tablist'), 'flex-wrap', PHONE)).toBe('wrap')
    const tab = within(strip).getAllByRole('tab')[0]!
    expect(painted(tab, 'max-width', PHONE)).toBe('100%')
    expect(painted(within(tab).getByText(FLOW.title), 'overflow-wrap', PHONE)).toBe('anywhere')
    expect(painted(within(strip).getByRole('tabpanel'), 'max-width', PHONE)).toBe('100%')
  })
})
