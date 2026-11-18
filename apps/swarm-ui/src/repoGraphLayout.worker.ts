/**
 * The module canvas's layout, off the main thread (graph-rendering.md §3.2
 * defect 9, lane GR3). RepoGraph.tsx `usePlan` starts this as a module worker
 * for a view too big to lay out in place, sends it the view and draws the
 * plan it answers. It runs the same pure `layoutPlan` the page runs for a
 * small view, so the picture does not depend on where it was computed.
 */
import { layoutPlan, type LayoutRequest } from './RepoGraphLayout'

self.onmessage = (e: MessageEvent<LayoutRequest>) => {
  const { view, width, height } = e.data
  self.postMessage({ plan: layoutPlan(view, width, height) })
}
