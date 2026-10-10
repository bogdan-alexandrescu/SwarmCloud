// LANE GR1 OF THE GRAPH RENDERING DESIGN (docs/design/graph-rendering.md §7,
// the GR1 row): the workflow DAG's crossing reduction, critical path and
// vertical map, each held to the row's acceptance figure.
//
// WHAT EACH CASE HOLDS:
//   * the ordering pass inside `layoutOf` keeps every step on its own level,
//     adds none and drops none -- it reorders and does nothing else (§6);
//   * on the 48-step fixture the drawn crossings, counted on `layoutOf`'s own
//     geometry, are at most 30 (129 in listing order, design §3.2);
//   * the pass costs at most 5 ms at 50 steps;
//   * the critical path names only steps with a measured (`ran`) duration,
//     and while any step has none the caption is the `absent` Mark and no
//     edge is drawn as critical;
//   * a canvas taller than the screen gets the vertical map, one that fits
//     does not;
//   * dag.ts and Workflows.tsx import nothing outside this app but React.
//
// MUTATIONS: take `orderLevels` out of `layoutOf` (the crossings case reads
// 129 and turns red); let `criticalPath` skip an unmeasured step instead of
// giving up (the unmeasured case names a path and turns red); count `running`
// as measured (the same); drop the height check from `VerticalMinimap` (the
// short-chain case turns red).

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render } from '@testing-library/react'

import { WorkflowCard, stageKey } from '../Workflows'
import { criticalPath, layoutOf, levelsOf, orderLevels, type DagLayout } from '../dag'
import type { Task, Workflow, WorkflowStep } from '../types'
import { T0, bigSteps, bigWorkflow, ran, running } from './dagfixture'

/**
 * Crossings in what the renderer DRAWS: every pair of edges between the same
 * two adjacent levels whose ends swap sides, on `layoutOf`'s coordinates.
 * Skip-level edges run on lanes of their own and are not counted; edges into
 * one step meet at its join and cannot cross each other. The GFY prototype's
 * `drawnCrossings`, which measured the 129 and the 30 the design quotes.
 */
function drawnCrossings(layout: DagLayout): number {
  const levelOf = new Map(layout.nodes.map((n) => [n.step.step_id, n.level]))
  const adjacent = layout.edges.filter((e) => e.lane === null && levelOf.get(e.to)! - levelOf.get(e.from)! === 1)
  let total = 0
  for (let i = 0; i < adjacent.length; i++) {
    for (let j = i + 1; j < adjacent.length; j++) {
      const a = adjacent[i]!
      const b = adjacent[j]!
      if (levelOf.get(a.from) !== levelOf.get(b.from)) continue
      if ((a.x1 - b.x1) * (a.x2 - b.x2) < 0) total++
    }
  }
  return total
}

/** The same count over ORDERS alone, for levels drawn side by side in the given order. */
function orderCrossings(levels: readonly (readonly WorkflowStep[])[]): number {
  const pos = new Map<string, number>()
  levels.forEach((lv) => lv.forEach((s, i) => pos.set(s.step_id, i)))
  let total = 0
  for (let l = 1; l < levels.length; l++) {
    const above = new Set(levels[l - 1]!.map((s) => s.step_id))
    const pairs: [number, number][] = []
    for (const s of levels[l]!) for (const d of s.depends_on) if (above.has(d)) pairs.push([pos.get(d)!, pos.get(s.step_id)!])
    for (let i = 0; i < pairs.length; i++)
      for (let j = i + 1; j < pairs.length; j++)
        if ((pairs[i]![0] - pairs[j]![0]) * (pairs[i]![1] - pairs[j]![1]) < 0) total++
  }
  return total
}

const allOpen = (steps: readonly WorkflowStep[]) => new Set(levelsOf(steps).map((_, i) => i))
const ids = (levels: readonly (readonly WorkflowStep[])[]) => levels.map((lv) => lv.map((s) => s.step_id).sort())

describe('GR1: crossing reduction inside layoutOf', () => {
  const steps = bigSteps()

  it('is the 48-step fixture the design measured', () => {
    expect(steps).toHaveLength(48)
    // Listing order crosses: the braid this lane exists to undo.
    expect(orderCrossings(levelsOf(steps))).toBeGreaterThan(100)
  })

  it('keeps every step on its own level, adding none and dropping none', () => {
    const layout = layoutOf(steps, allOpen(steps), 'names')
    expect(ids(layout.levels)).toEqual(ids(levelsOf(steps)))
    expect(layout.nodes).toHaveLength(steps.length)
    expect(new Set(layout.nodes.map((n) => n.step.step_id)).size).toBe(steps.length)
    for (const n of layout.nodes) expect(levelsOf(steps)[n.level]!.some((s) => s.step_id === n.step.step_id)).toBe(true)
  })

  it('draws at most 30 crossings on the 48-step fixture with every stage open (129 in listing order)', () => {
    const drawn = drawnCrossings(layoutOf(steps, allOpen(steps), 'names'))
    expect(drawn).toBeLessThanOrEqual(30)
    // And the same at the full tier, where the bands are drawn too.
    expect(drawnCrossings(layoutOf(steps, allOpen(steps), 'figures'))).toBeLessThanOrEqual(30)
  })

  it('leaves a level order that already crosses nothing as it was listed', () => {
    const plain = bigSteps(1).filter((s) => s.step_id === 'plan' || s.step_id.startsWith('spec-'))
    expect(layoutOf(plain).levels.map((lv) => lv.map((s) => s.step_id))).toEqual(levelsOf(plain).map((lv) => lv.map((s) => s.step_id)))
  })

  it('orders 50 steps in at most 5 ms', () => {
    const fifty = [...steps, { ...steps[0]!, step_id: 'notify-1', depends_on: ['release'] }, { ...steps[0]!, step_id: 'notify-2', depends_on: ['release'] }]
    expect(fifty).toHaveLength(50)
    const levels = levelsOf(fifty)
    orderLevels(levels) // warm the JIT: the figure is the pass, not the first compile
    const runs: number[] = []
    for (let i = 0; i < 7; i++) {
      const t0 = performance.now()
      orderLevels(levels)
      runs.push(performance.now() - t0)
    }
    runs.sort((a, b) => a - b)
    expect(runs[3]!).toBeLessThanOrEqual(5)
  })
})

describe('GR1: the critical path', () => {
  // a -> (b 10s | c 30s) -> d: the path is a, c, d.
  const s = (step_id: string, depends_on: string[]): WorkflowStep => ({
    step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on, input_from: {}, task_id: null,
  })
  const diamond = [s('a', []), s('b', ['a']), s('c', ['a']), s('d', ['b', 'c'])]

  it('is the chain whose measured durations sum longest', () => {
    const took: Record<string, number> = { a: 5, b: 10, c: 30, d: 1 }
    expect(criticalPath(diamond, (x) => took[x.step_id] ?? null)).toEqual({ kind: 'measured', steps: ['a', 'c', 'd'], seconds: 36 })
  })

  it('keeps a measured zero as a measurement', () => {
    expect(criticalPath(diamond, () => 0)).toEqual({ kind: 'measured', steps: ['a', 'b', 'd'], seconds: 0 })
  })

  it('names no path while any step has no measured duration', () => {
    const took: Record<string, number | null> = { a: 5, b: 10, c: null, d: 1 }
    expect(criticalPath(diamond, (x) => took[x.step_id] ?? null)).toEqual({ kind: 'unmeasured', missing: ['c'] })
  })
})

describe('GR1: the critical path on the canvas', () => {
  beforeEach(() => {
    vi.spyOn(Date, 'now').mockReturnValue(T0)
  })
  const draw = (workflow: Workflow, taskById: Map<string, Task>, open = false) =>
    render(
      <WorkflowCard
        workflow={workflow}
        taskById={taskById}
        usage={{ kind: 'ready', usage: null }}
        openStages={open ? Object.fromEntries(levelsOf(workflow.steps).map((_, i) => [stageKey(workflow.workflow_id, i), true])) : {}}
        onToggleStage={() => undefined}
        reload={() => undefined}
      />,
    )

  it('draws a finished run’s path heavier and names its steps, every one measured', () => {
    // Every step ran 60s, except one implementer chain that ran long.
    const long = new Set(['impl-ui-2', 'test-ui-2'])
    const { workflow, taskById } = bigWorkflow(bigSteps(), (st) => ran(long.has(st.step_id) ? 900 : 60))
    const { container } = draw(workflow, taskById, true)
    const caption = container.querySelector('.wf-critical')
    expect(caption, 'no critical-path caption').toBeTruthy()
    expect(caption!.querySelector('.ctl-mark')).toBeNull()
    const chain = caption!.querySelector('.wf-critical-chain')!.textContent!.split(' → ')
    expect(chain).toEqual(['plan', 'spec-ui', 'impl-ui-2', 'test-ui-2', 'review-ui', 'docs-ui', 'integrate', 'e2e-1', 'canary', 'release'])
    expect(caption!.querySelector('.wf-critical-h')!.textContent).toBe('critical path · 38m 0s')
    // Every named step is one the workflow has, with a task that finished.
    for (const id of chain) {
      const st = workflow.steps.find((x) => x.step_id === id)!
      expect(taskById.get(st.task_id!)!.completed_at).not.toBeNull()
    }
    // The heavier edges are exactly the consecutive pairs of the chain.
    const critical = [...container.querySelectorAll('.wf-edges .wf-link.is-critical')].map((g) => g.getAttribute('data-edge'))
    expect(critical.sort()).toEqual(chain.slice(1).map((id, i) => `${chain[i]}->${id}`).sort())
  })

  it('says “not measured” with the absent Mark while a step is still running, and draws no path', () => {
    const { workflow, taskById } = bigWorkflow(bigSteps(), (st) =>
      st.step_id.startsWith('impl-') || st.step_id === 'plan' || st.step_id.startsWith('spec-')
        ? st.step_id === 'impl-api-1' ? running(120) : ran(60)
        : null,
    )
    const { container } = draw(workflow, taskById)
    const mark = container.querySelector('.wf-critical .ctl-mark.is-absent')
    expect(mark, 'no absent Mark on the critical-path caption').toBeTruthy()
    expect(mark!.textContent).toBe('not measured')
    expect(mark!.getAttribute('aria-label')).toMatch(/^Critical path not measured: 32 of 48 steps have no measured duration \(impl-api-1, /)
    expect(container.querySelector('.wf-critical-chain')).toBeNull()
    expect(container.querySelectorAll('.wf-link.is-critical')).toHaveLength(0)
  })
})

describe('GR1: the vertical map', () => {
  const draw = (workflow: Workflow, open: boolean) =>
    render(
      <WorkflowCard
        workflow={workflow}
        taskById={new Map()}
        usage={{ kind: 'ready', usage: null }}
        openStages={open ? Object.fromEntries(levelsOf(workflow.steps).map((_, i) => [stageKey(workflow.workflow_id, i), true])) : {}}
        onToggleStage={() => undefined}
        reload={() => undefined}
      />,
    )

  it('maps a canvas taller than the screen, top to bottom, with its height in words', () => {
    const { workflow } = bigWorkflow(bigSteps(), () => null)
    const { container } = draw(workflow, true)
    const map = container.querySelector<HTMLElement>('.wf-vmap')
    expect(map, 'no vertical map beside a 48-step canvas').toBeTruthy()
    expect(map!.getAttribute('role')).toBe('img')
    const tall = Number(container.querySelector<HTMLElement>('.wf-canvas')!.style.height.replace('px', ''))
    expect(tall).toBeGreaterThan(window.innerHeight)
    expect(map!.getAttribute('aria-label')).toContain(`It is ${Math.round(tall)} pixels tall`)
    // One map rectangle per drawn node: the map draws the layout, nothing else.
    expect(map!.querySelectorAll('.wf-mini-node')).toHaveLength(container.querySelectorAll('.wf-canvas .node-slot').length)
  })

  it('draws no map beside a canvas that fits the screen', () => {
    const { workflow } = bigWorkflow(bigSteps(1).filter((s) => s.step_id === 'plan' || s.step_id === 'spec-api'), () => null)
    const { container } = draw(workflow, false)
    expect(container.querySelector('.wf-canvas')).toBeTruthy()
    expect(container.querySelector('.wf-vmap')).toBeNull()
  })
})

describe('GR1: no new dependency', () => {
  it('dag.ts and Workflows.tsx import nothing from outside this app but React', () => {
    for (const file of ['../dag.ts', '../Workflows.tsx']) {
      const src = readFileSync(fileURLToPath(new URL(file, import.meta.url)), 'utf8')
      const from = [...src.matchAll(/^import[^'"]*?from\s+['"]([^'"]+)['"]|^import\s+['"]([^'"]+)['"]/gms)].map((m) => m[1] ?? m[2]!)
      expect(from.length, `${file}: no imports read`).toBeGreaterThan(3)
      expect(from.filter((m) => !m.startsWith('./') && m !== 'react'), file).toEqual([])
    }
  })
})
