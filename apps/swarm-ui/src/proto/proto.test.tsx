// THE GRAPH-RENDERING PROTOTYPE, HELD (docs/design/graph-rendering.md).
//
// WHAT EACH CASE HOLDS:
//   * the barycentric pass (option D) returns the same steps on the same
//     levels -- it invents no node, drops none and moves none to another stage;
//   * on the 48-step fixture it draws strictly fewer, and at most half as many,
//     crossings as today's order-of-listing layout (counted on layoutOf's own
//     geometry, so the count is what the reader sees);
//   * a canvas renderer (vis-network, Graphify's) cannot draw in jsdom: the
//     prototype says so in place instead of rendering an empty box, which is
//     what any adoption would have to test around;
//   * the measurements the design doc quotes are printed, not asserted: jsdom
//     timings vary by machine and a timing gate here would be a flaky gate.
//
// MUTATIONS: return `steps` unchanged from barycentricOrder (the reduction
// case turns red); drop a step from it (the same-steps case turns red); let
// VisCanvas swallow the error without its message (the jsdom case turns red).

import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'
import { levelsOf } from '../dag'
import { forceLayout, graphView, normModuleGraph } from '../RepoGraphData'
import { WorkflowCard, stageKey } from '../Workflows'
import { GraphifyWorkflow } from './GraphifyWorkflow'
import { protoGraphBody, protoWorkflow } from './fixtures'
import { barycentricOrder, drawnCrossings, orderCrossings } from './layered'

describe('option D: the barycentric ordering pass', () => {
  const { workflow } = protoWorkflow()

  it('returns the same steps on the same levels', () => {
    const before = levelsOf(workflow.steps).map((lv) => lv.map((s) => s.step_id).sort())
    const after = levelsOf(barycentricOrder(workflow.steps)).map((lv) => lv.map((s) => s.step_id).sort())
    expect(after).toEqual(before)
    expect(barycentricOrder(workflow.steps)).toHaveLength(workflow.steps.length)
  })

  it('at least halves the crossings today’s layout draws on the 48-step fixture', () => {
    const today = drawnCrossings(workflow.steps)
    const ordered = drawnCrossings(barycentricOrder(workflow.steps))
    console.info(`[graph-proto] 48 steps: ${today} crossings drawn today, ${ordered} after the barycentric pass (order-only count ${orderCrossings(levelsOf(workflow.steps))} -> ${orderCrossings(levelsOf(barycentricOrder(workflow.steps)))})`)
    expect(today).toBeGreaterThan(0)
    expect(ordered * 2).toBeLessThanOrEqual(today)
  })
})

describe('a canvas renderer in jsdom', () => {
  it('says in place that it has no canvas to draw on', () => {
    const { workflow, taskById } = protoWorkflow()
    const { container } = render(<GraphifyWorkflow workflow={workflow} taskById={taskById} height={400} />)
    expect(container.querySelector('.gfy-canvas')?.getAttribute('data-failed')).toBe('true')
    expect(container.querySelector('.gfy-failed')?.textContent).toMatch(/needs a 2D canvas/)
    // The accessible fallback still lists every step.
    expect(container.querySelectorAll('.gfy-sr li')).toHaveLength(workflow.steps.length)
  })
})

describe('measurements quoted by the design doc', () => {
  const time = (f: () => void) => {
    const t0 = performance.now()
    f()
    return Math.round(performance.now() - t0)
  }

  it('times today’s workflow card in jsdom at 48 and 200 steps', () => {
    for (const rounds of [3, 22]) {
      const { workflow, taskById } = protoWorkflow(rounds)
      const all = Object.fromEntries(levelsOf(workflow.steps).map((_, i) => [stageKey(workflow.workflow_id, i), true]))
      let nodes = 0
      const ms = time(() => {
        const { container, unmount } = render(
          <WorkflowCard workflow={workflow} taskById={taskById} usage={{ kind: 'ready', usage: null }} openStages={all} onToggleStage={() => undefined} reload={() => undefined} />,
        )
        nodes = container.querySelectorAll('.node-slot').length
        unmount()
      })
      const layered = time(() => barycentricOrder(workflow.steps))
      console.info(`[graph-proto] WorkflowCard ${workflow.steps.length} steps: first render ${ms} ms in jsdom, ${nodes} node slots; barycentric pass ${layered} ms; crossings ${drawnCrossings(workflow.steps)} -> ${drawnCrossings(barycentricOrder(workflow.steps))}`)
      expect(nodes).toBeGreaterThan(0)
    }
  })

  it('times the repository force layout at 48, 200, 500 and 2000 modules', () => {
    for (const n of [48, 200, 500, 2000]) {
      const g = normModuleGraph(protoGraphBody(n))!
      const open = graphView(g, new Set(), true)
      const flat = graphView(g, new Set(), false)
      const clustered = time(() => forceLayout(open))
      const unclustered = time(() => forceLayout(flat))
      console.info(`[graph-proto] forceLayout ${n} modules: ${open.nodes.length} drawn when clustered (${clustered} ms), ${flat.nodes.length} unclustered (${unclustered} ms)`)
      expect(flat.nodes.length).toBe(n)
    }
  })
})
