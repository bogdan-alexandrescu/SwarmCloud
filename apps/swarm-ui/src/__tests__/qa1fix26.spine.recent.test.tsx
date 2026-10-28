// QA1-FIX-26 overview-agents-21 (part of #2596): on the Agents pages every
// Recent workflow in the sidebar carried the dashed "not re-read" ring, while
// the Workflows pages drew the same ones running or failed -- only the
// Workflows list offered states. Recent now reads the ids no read of this page
// load gave, once, in one batched read.
//
// MUTATION: drop the `loadWorkflowsById` effect from RecentWorkflows and the
// row keeps `data-mark="stale"` on the Agents page.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'

const reads = vi.hoisted(() => ({ byId: vi.fn() }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, loadWorkflowsById: reads.byId }
})

const Spine = await import('../Spine')
const { STATE_MARK } = await import('../marks')

afterEach(() => {
  cleanup()
  localStorage.clear()
  reads.byId.mockReset()
})

describe('overview-agents-21: Recent reads its own states', () => {
  it('draws the state its own read derived on the Agents page, and asks once', async () => {
    localStorage.setItem(Spine.RECENT_WORKFLOWS_KEY, JSON.stringify([{ id: 'wf_q1a', state: 'RUNNING', name: 'lane-a' }]))
    expect(Spine.workflowReadThisLoad('wf_q1a')).toBe(false)
    const workflow = { workflow_id: 'wf_q1a', state_source: 'derived', rollup: { complete: true, state: 'FAILED' } }
    reads.byId.mockResolvedValue({
      status: 'ok', fetchedAt: Date.now(),
      data: { batched: true, reads: new Map([['wf_q1a', { workflow, tasks: [] }]]), missing: [] },
    })
    render(
      <Spine.SkyShell section="work" tab="agents" title="Agents" go={vi.fn()} foot={null}>
        {null}
      </Spine.SkyShell>,
    )
    const group = screen.getByRole('group', { name: 'Recent workflows' })
    const mark = () => [...group.querySelectorAll<HTMLElement>('a')].find((a) => a.textContent?.includes('lane-a'))!.querySelector('.sk-st')!
    await waitFor(() => expect(mark().getAttribute('data-mark')).toBe(STATE_MARK.FAILED.mark))
    expect(mark().getAttribute('title')).toBe('failed')
    expect(reads.byId).toHaveBeenCalledTimes(1)
    expect(reads.byId.mock.calls[0]![0]).toEqual(['wf_q1a'])
  })

  it('leaves the ring on a workflow the read did not return', async () => {
    localStorage.setItem(Spine.RECENT_WORKFLOWS_KEY, JSON.stringify([{ id: 'wf_q1gone', state: 'RUNNING', name: 'gone' }]))
    reads.byId.mockResolvedValue({ status: 'ok', fetchedAt: Date.now(), data: { batched: true, reads: new Map(), missing: ['wf_q1gone'] } })
    render(
      <Spine.SkyShell section="work" tab="agents" title="Agents" go={vi.fn()} foot={null}>
        {null}
      </Spine.SkyShell>,
    )
    await waitFor(() => expect(reads.byId).toHaveBeenCalledTimes(1))
    const group = screen.getByRole('group', { name: 'Recent workflows' })
    const row = [...group.querySelectorAll<HTMLElement>('a')].find((a) => a.textContent?.includes('gone'))!
    expect(row.querySelector('.sk-st')!.getAttribute('data-mark')).toBe('stale')
  })
})
