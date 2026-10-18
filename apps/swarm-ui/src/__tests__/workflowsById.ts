// A `loadWorkflowsById` double over a test's `loadWorkflow` double (live QA
// CH-6): the Timeline reads the workflows its lanes name 50 to a read, and a
// test that answers per workflow keeps doing so. An id whose single answer is
// not data is one the batch did not serve: `missing`, as the route says it.
import type { WorkflowBoard, WorkflowRead, WorkflowReads } from '../api'
import type { Result } from '../fetch'
import type { Task } from '../types'

export function byIdVia(
  loadWorkflow: (id: string) => Promise<Result<WorkflowRead>>,
): (ids: readonly string[]) => Promise<Result<WorkflowReads>> {
  return async (ids) => {
    const reads = new Map<string, WorkflowRead>()
    const missing: string[] = []
    for (const id of ids) {
      const r = await loadWorkflow(id)
      if (r.status === 'ok' || r.status === 'stale') reads.set(id, r.data)
      else missing.push(id)
    }
    return { status: 'ok', fetchedAt: Date.now(), data: { batched: true, reads, missing } }
  }
}

/**
 * A `loadWorkflow` double over a test's `loadWorkflowBoard` double (live QA
 * CH-21): a workflow's page reads its own workflow first, and the API serves
 * any workflow the board holds -- the row and its steps' tasks -- and a 404
 * only for one it does not. It waits on the board double, so a test that
 * holds the board back to see the skeleton holds the page's own read too.
 */
export function ownFromBoard(
  loadWorkflowBoard: () => Promise<Result<WorkflowBoard>>,
): (id: string) => Promise<Result<WorkflowRead>> {
  return async (id) => {
    const b = await loadWorkflowBoard()
    const row = b.status === 'ok' || b.status === 'stale' ? b.data.workflows.find((w) => w.workflow_id === id) : undefined
    if (row === undefined || (b.status !== 'ok' && b.status !== 'stale')) {
      return { status: 'error', error: { kind: 'not_found', httpStatus: 404, code: null, message: 'no such workflow' } }
    }
    const tasks = row.steps
      .map((st) => (st.task_id ? b.data.taskById?.get(st.task_id) : undefined))
      .filter((t): t is Task => t !== undefined)
    return { status: 'ok', fetchedAt: b.fetchedAt, data: { workflow: row, tasks } }
  }
}
