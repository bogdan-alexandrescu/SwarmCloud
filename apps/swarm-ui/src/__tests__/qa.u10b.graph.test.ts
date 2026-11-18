/**
 * BROWSER QA U10b (owner, 2026-10-04; live console at 1440x900): the
 * workflow Graph.
 *
 *   D29  Graph nodes had 40-65px of empty space at the bottom: the 42px stop
 *        strip was reserved on every card, including a finished step's, which
 *        can never draw a Stop control again. And Auto picked Details for a
 *        three-step chain but Figures for a ten-step run: once no tier fits the
 *        screen, Auto fell back to the TALLEST tier. Now a settled step's card
 *        gives the strip back (`settled`), and Auto takes Figures only when the
 *        whole graph fits at Figures -- otherwise Details whenever its row fits,
 *        Names when not even that does.
 *
 * MUTATIONS: drop the `settled` saving from `heightOf`, or return 'figures'
 * from either of `autoTier`'s fallbacks -- each turns a case red.
 */
import { describe, expect, it } from 'vitest'

import { autoTier, heightOf, layoutOf, nodeHeightAt, stripSavingAt } from '../dag'
import type { WorkflowStep } from '../types'
import { painted } from './marks'

function step(step_id: string, depends_on: string[]): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on, input_from: {}, task_id: null }
}

const chainOf = (n: number): WorkflowStep[] => Array.from({ length: n }, (_, i) => step(`s${i}`, i === 0 ? [] : [`s${i - 1}`]))
const fanOf = (n: number): WorkflowStep[] => [step('plan', []), ...Array.from({ length: n }, (_, i) => step(`impl-${i}`, ['plan'])), step('review', Array.from({ length: n }, (_, i) => `impl-${i}`))]

describe('D29: a finished step gives back the stop strip', () => {
  it('saves the strip on a settled card at every tier', () => {
    for (const tier of ['figures', 'details', 'names'] as const) {
      const s = step('plan', [])
      expect(stripSavingAt(tier), tier).toBeGreaterThanOrEqual(28)
      expect(heightOf(s, tier, undefined, false, true), tier).toBe(nodeHeightAt(tier) - stripSavingAt(tier))
      expect(heightOf(s, tier, undefined, false, false), tier).toBe(nodeHeightAt(tier))
    }
  })

  it('lays a level of settled steps out shorter, and a level with one live step at full height', () => {
    const steps = chainOf(3)
    const all = new Set(steps.map((s) => s.step_id))
    const open = layoutOf(steps, undefined, 'details')
    const done = layoutOf(steps, undefined, 'details', undefined, undefined, all)
    expect(done.height).toBeLessThan(open.height)
    const mixed = layoutOf(steps, undefined, 'details', undefined, undefined, new Set(['s0', 's1']))
    const h = (l: typeof open, id: string) => l.nodes.find((n) => n.step.step_id === id)!.h
    expect(h(mixed, 's2')).toBe(h(open, 's2'))
    expect(h(mixed, 's0')).toBe(h(open, 's0') - stripSavingAt('details'))
  })
})

describe('D29: the settled card is drawn as short as it is laid out', () => {
  it('pads a settled card under its last row by its top padding, not the strip', () => {
    const host = document.createElement('div')
    host.innerHTML =
      '<div class="wf-canvas"><div class="node-slot"><button class="node zoom-details is-settled"></button></div>' +
      '<div class="node-slot"><button class="node zoom-names is-settled"></button></div>' +
      '<div class="node-slot"><button class="node zoom-details"></button></div></div>'
    document.body.appendChild(host)
    const [details, names, live] = [...host.querySelectorAll('.node')]
    expect(painted(details!, ['padding-bottom', 'padding'], { width: 1440 })).toBe(`${42 - stripSavingAt('details')}px`)
    expect(painted(names!, ['padding-bottom', 'padding'], { width: 1440 })).toBe(`${34 - stripSavingAt('names')}px`)
    expect(painted(live!, ['padding-bottom', 'padding'], { width: 1440 })).toBe('10px 12px 42px')
    host.remove()
  })
})

describe('D29: Auto chooses the same way for a short and a long run', () => {
  it('never falls back to Figures when the graph does not fit at Figures', () => {
    expect(autoTier(chainOf(3))).toBe('details')
    expect(autoTier(chainOf(10))).not.toBe('figures')
    // Ten parallel steps fit no row at any tier: a band, drawn at Details.
    expect(autoTier(fanOf(10))).toBe('details')
    expect(autoTier(fanOf(40))).toBe('details')
  })

  it('still takes Figures when the whole graph fits there', () => {
    expect(autoTier([step('only', [])])).toBe('figures')
  })
})
