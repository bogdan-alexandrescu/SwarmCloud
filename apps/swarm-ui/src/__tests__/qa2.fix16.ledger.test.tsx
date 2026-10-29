// WORK › TIMELINE › OUTCOMES, VISUAL QA PASS 2 (QA2-FIX-16; part of #2694).
//
//   runs2-18  at 1440 Reliability scrolled sideways (1019 of 1005 px), its
//             last column the rate by day; at 390 its overflow and the
//             failed workflows' were signposted by a fade alone. The rate by
//             bucket is folded under the rate, and a line under each table
//             says when columns are off its right edge.
//   runs2-23  the profile rows did not add up to the headline (638 ended
//             against 716) and nothing said where the rest went. A row
//             carries the remainder and the headline sits under the rows.
//   runs2-36  failed workflows were listed by truncated id only. The
//             workflow's name leads, the id under it.
//   runs2-37  "181 of 190" beside Ended 172: the cost's denominator is
//             attempts, and it says so.
//
// MUTATION: put the "Rate by {bucket}" column back -- the first case finds
// it. Drop `MoreRightSaid`'s line -- the second finds no words. Return null
// from `groupRemainder` -- the third finds no "other profiles" row. Print
// `shortId` alone in the workflow cell -- the fifth finds no name. Drop
// `attemptsWord` -- the last finds "181 of 190" bare. Pass `one !== null`
// as `listRowName`'s stepsKnown again (no `stepsRead`) -- the read missing
// its step tasks prints "Untitled workflow" and the sixth finds it.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'

import type { WorkflowRead } from '../api'

const api = vi.hoisted(() => ({ loadWorkflowsById: vi.fn() }))
vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { ReliabilityCard, WorkflowsFailedCard, groupRemainder } = await import('../Ledger')
const { ledgerFixture } = await import('../outcomes.fixture')

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const words = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

function reliability() {
  const d = ledgerFixture()
  const utils = render(<ReliabilityCard data={d} group="runner_profile" platform={false} onGroup={() => {}} />)
  return { d, ...utils }
}

describe('runs2-18: Reliability fits at desktop and says when it does not', () => {
  it('folds the rate by bucket under the rate: no column of its own', () => {
    const { container, d } = reliability()
    const heads = [...container.querySelectorAll('.ol-reliability thead th')].map(words)
    expect(heads).not.toContain(`Rate by ${d.bucket}`)
    expect(heads).toContain(`Success rate · by ${d.bucket}`)
    expect(heads).toHaveLength(7)
    // The line is in the rate's cell, under its figure.
    const row = container.querySelector<HTMLTableRowElement>('.ol-reliability tbody tr[data-key="mock"]')!
    expect(row.cells[2]!.querySelector('.ol-spark'), 'the rate cell holds no line').not.toBeNull()
  })

  it('says in words that columns are off the right edge while they are', async () => {
    // jsdom lays nothing out: a scroller 322 px wide holding 1047 px of table.
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(322)
    vi.spyOn(HTMLElement.prototype, 'scrollWidth', 'get').mockReturnValue(1047)
    const { container } = reliability()
    await waitFor(() => expect(container.querySelector('.ol-reliability .ol-table-more')).not.toBeNull())
    expect(words(container.querySelector('.ol-reliability .ol-table-more'))).toBe(
      'More columns to the right: scroll the table sideways to read all 7.',
    )
  })
})

describe('runs2-23: the rows add up to the headline', () => {
  it('carries what the headline counts and no row does in an "other profiles" row, and the headline under them', () => {
    const { container, d } = reliability()
    const left = groupRemainder(d)
    expect(left, 'the fixture rows already add up: nothing to pin').not.toBeNull()
    const rest = [...container.querySelectorAll<HTMLTableRowElement>('.ol-reliability tbody tr')].find((tr) => words(tr.querySelector('th')) === 'other profiles')
    expect(rest, 'no row carries the remainder').toBeDefined()
    expect(words(rest!.cells[1]!)).toBe(String(left!.ended))
    expect(words(rest!.cells[3]!)).toBe(String(left!.succeeded))
    const all = container.querySelector<HTMLTableRowElement>('.ol-reliability tbody tr.ol-total')!
    expect(words(all.querySelector('th'))).toBe('All')
    expect(words(all.cells[1]!)).toBe(String(d.totals.ended))
    // Every row plus the remainder is the headline.
    const ended = [...container.querySelectorAll('.ol-reliability tbody tr:not(.ol-total)')].reduce((n, tr) => n + Number(words((tr as HTMLTableRowElement).cells[1]!)), 0)
    expect(ended).toBe(d.totals.ended)
  })

  it('draws no remainder when the rows add up', () => {
    const d = ledgerFixture()
    const sum = (f: (r: (typeof d.groups.rows)[number]) => number) => d.groups.rows.reduce((n, r) => n + f(r), 0)
    const t = {
      ...d.totals,
      ended: sum((r) => r.ended),
      succeeded: sum((r) => r.succeeded),
      failed: sum((r) => r.failed),
      dead_lettered: sum((r) => r.dead_lettered),
      cancelled: {
        ...d.totals.cancelled,
        total: sum((r) => r.cancelled.total),
        after_failure: sum((r) => r.cancelled.after_failure),
        workflow_sweep: sum((r) => r.cancelled.workflow_sweep),
      },
    }
    expect(groupRemainder({ ...d, totals: t })).toBeNull()
  })
})

describe('runs2-36: a failed workflow by its name', () => {
  it('leads with the name read off its steps, the id under it', async () => {
    const d = ledgerFixture()
    const id = d.workflows_failed.rows[0]!.workflow_id
    const read = {
      workflow: { workflow_id: id, steps: [{ step_id: 'synthesis', task_id: 'task_n1' }] },
      tasks: [{ id: 'task_n1', metadata: { title: 'Research, draft and review the restore runbook' } }],
    } as unknown as WorkflowRead
    api.loadWorkflowsById.mockResolvedValue({
      status: 'ok',
      fetchedAt: Date.now(),
      data: { batched: true, reads: new Map([[id, read]]), missing: [] },
    })
    const { container } = render(<WorkflowsFailedCard data={d} spanLabel="14d" />)
    const cell = await waitFor(() => {
      const th = container.querySelector('.ol-workflows tbody tr th')!
      expect(words(th)).toContain('Research, draft and review the restore runbook')
      return th
    })
    // The name first, the id on the line under it, whole in its title.
    const [name, short] = [...cell.querySelectorAll(':scope > span')]
    expect(words(name!)).toBe('Research, draft and review the restore runbook')
    expect(name!.nextElementSibling!.tagName).toBe('BR')
    expect(words(short!)).toBe('wf_bcdc…9f31')
    expect(short!.getAttribute('title')).toBe(id)
    // A workflow whose name was not served keeps its id alone: never "Untitled" as a guess.
    const second = container.querySelectorAll('.ol-workflows tbody tr th')[1]!
    expect(words(second)).toBe('wf_71aa…02c1')
    expect(api.loadWorkflowsById).toHaveBeenCalledTimes(1)
    expect(screen.queryByText(/Untitled workflow/)).toBeNull()
  })
})

describe('runs2-36 (review): a read without its step tasks is not a guess', () => {
  // The fixture console's read: the workflow came back, its step tasks did
  // not, and Outcomes called it "Untitled workflow" while Work › Workflows
  // named it. Untitled only once every step's task is read, as there.
  function serve(id: string, read: WorkflowRead) {
    api.loadWorkflowsById.mockResolvedValue({
      status: 'ok',
      fetchedAt: Date.now(),
      data: { batched: true, reads: new Map([[id, read]]), missing: [] },
    })
  }
  const steps = [
    { step_id: 'research', task_id: 'task_r1' },
    { step_id: 'draft', task_id: 'task_d1' },
  ]

  it('keeps the id alone when no step task came back, or only some did', async () => {
    const d = ledgerFixture()
    const id = d.workflows_failed.rows[0]!.workflow_id
    for (const tasks of [[], [{ id: 'task_r1', metadata: {} }]]) {
      serve(id, { workflow: { workflow_id: id, steps }, tasks } as unknown as WorkflowRead)
      const { container, unmount } = render(<WorkflowsFailedCard data={d} spanLabel="14d" />)
      await waitFor(() => expect(api.loadWorkflowsById).toHaveBeenCalled())
      await new Promise((r) => setTimeout(r, 0))
      const th = container.querySelector('.ol-workflows tbody tr th')!
      expect(words(th), `with ${tasks.length} of 2 step tasks read`).toBe('wf_bcdc…9f31')
      expect(screen.queryByText(/Untitled workflow/)).toBeNull()
      unmount()
      api.loadWorkflowsById.mockReset()
    }
  })

  it('says "Untitled workflow" once every step task is read and none has a name', async () => {
    const d = ledgerFixture()
    const id = d.workflows_failed.rows[0]!.workflow_id
    const tasks = [
      { id: 'task_r1', metadata: {} },
      { id: 'task_d1', metadata: {} },
    ]
    serve(id, { workflow: { workflow_id: id, steps }, tasks } as unknown as WorkflowRead)
    const { container } = render(<WorkflowsFailedCard data={d} spanLabel="14d" />)
    await waitFor(() => expect(words(container.querySelector('.ol-workflows tbody tr th'))).toContain('Untitled workflow'))
  })
})

describe('runs2-37: the cost names its denominator', () => {
  it('says "181 of 190 attempts" beside a row that counts agents', () => {
    const { container } = reliability()
    const row = container.querySelector<HTMLTableRowElement>('.ol-reliability tbody tr[data-key="claude-code"]')!
    expect(words(row.cells[6]!)).toContain('181 of 190 attempts')
  })
})
