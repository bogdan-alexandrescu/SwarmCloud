// THE JEV AGENTS IN THE FIXTURE BUILD (the owner's pick on PR #3032,
// 2026-11-02: "Add a fixture").
//
// The finding triage chips and the Quality gates line had no task in the
// console's fixture build to draw them, so neither could be seen without live
// data. Two agents a per-task read answers and no list returns, as the budget
// agents are: a review whose verdict.json carries Jev's `p_real`, and the
// step gated on it, whose summary carries `quality_gates`.
//
// These open each id through the real fixture loaders, as the agent page
// does; nothing is mocked.
//
// MUTATIONS, one per block: drop the Jev agents from `fixtureOffList` (their
// reads 404); drop the verdict.json body (no chip is drawn); drop the merge of
// the agent's own summary in `fixtureAgentDetail` (no Decision card, no
// Quality gates line); list them in `fixtureTaskRows` (the list test fails).

import { describe, expect, it } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import { loadAgentDetail, loadArtifactContent, loadTasks, qualityGatesOf, verdictTriage } from '../api'

const { AgentDetailScreen } = await import('../AgentDetail')

const REVIEW = 'task_jev_review'
const GATED = 'task_jev_gated'
const LONG = 20_000

describe('the fixture build answers the two Jev agents by id', () => {
  it('reads both, finished claude-code agents, kept off the list', async () => {
    expect(import.meta.env.DEV, 'this file reads the fixture build').toBeTruthy()
    for (const id of [REVIEW, GATED]) {
      const r = await loadAgentDetail(id)
      expect(r.status, `${id} answered ${r.status}`).toBe('ok')
      if (r.status !== 'ok') continue
      expect([r.data.task.id, r.data.task.state, r.data.task.runner_profile]).toEqual([id, 'SUCCEEDED', 'claude-code'])
      expect(qualityGatesOf(r.data.task)?.kind).toBe('ran')
    }
    const list = await loadTasks()
    const ids = list.status === 'ok' ? list.data.tasks.map((t) => t.id) : []
    expect(ids).not.toContain(REVIEW)
    expect(ids).not.toContain(GATED)
  })

  it("the review's verdict.json carries p_real: one likely real, one to verify, one not judged", async () => {
    const r = await loadArtifactContent(REVIEW, 'verdict.json')
    expect(r.status).toBe('ok')
    const text = r.status === 'ok' ? (r.data.content ?? '') : ''
    expect(verdictTriage(text)).toEqual([{ pReal: 0.62, verify: false }, { pReal: 0.08, verify: true }, null])
  })
})

describe('opening the gated agent draws both features', () => {
  it('its Decision card puts a triage chip on each finding Jev judged', async () => {
    const { container } = render(<AgentDetailScreen taskId={GATED} onClose={() => {}} />)
    const items = () => Array.from(container.querySelectorAll<HTMLElement>('section.dc li.dc-finding'))
    await waitFor(() => expect(items().map((li) => li.textContent).join('|')).toContain('likely real 62%'), { timeout: LONG })
    const [real, low, none] = items()
    expect(items()).toHaveLength(3)
    expect(real?.textContent).toContain('the lease is released twice')
    expect(low?.textContent).toContain('verify before acting')
    expect(low?.textContent).not.toContain('Jev judged this unlikely')
    expect(none?.querySelector('.triage-chip')).toBeNull()
  })

  it('its Details shows the Quality gates line with the counts', async () => {
    const { container } = render(<AgentDetailScreen taskId={GATED} onClose={() => {}} />)
    const line = await waitFor(
      () => {
        const l = container.querySelector<HTMLElement>('[data-testid="quality-gates-body"]')
        expect(l).not.toBeNull()
        return l!
      },
      { timeout: LONG },
    )
    expect(line.textContent).toContain('1 stop blocked')
    expect(line.textContent).toContain('2 rules flagged')
    expect(line.textContent).toContain('12 Jev calls')
  })
})
