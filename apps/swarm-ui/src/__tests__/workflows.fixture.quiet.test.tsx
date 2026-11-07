// THE FIXTURE BUILD SHOWS A FINISHED WORKFLOW'S QUIET TICK (follow-up of the
// merged "only what still holds work" change, part of #2688). No fixture task
// asked questions, so a render on fixtures never drew the '? N' beside a
// finished workflow's state. One SUCCEEDED workflow's implement step counts
// two questions; its questions read serves both, the first answered.
//
// MUTATIONS: drop the step's `result_summary.questions` -> no tick; drop its
// `owner_answers` -> the tick reads 2; serve the workflow RUNNING -> its
// question is a list '?' and no tick.
//
// Its list row costs Work › Workflows one word, so the list footer's
// "100 per read" moves into the footer's tooltip (the owner's pick, 6 November 2026).

import { cleanup, render, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { loadQuestions, loadTasks, loadWorkflowBoard, loadWorkflows } from '../api'
import type { Workflow } from '../types'
import { finishedQuestionsOf } from '../work/listNeeds'
import { needsOf } from '../workflowlist'

const { WorkflowsScreen } = await import('../Workflows')

afterEach(cleanup)

async function quietWorkflow(): Promise<{ w: Workflow; taskId: string }> {
  const r = await loadWorkflowBoard()
  if (r.status !== 'ok') throw new Error(`the fixture board answered ${r.status}`)
  const found = r.data.workflows.filter((w) => finishedQuestionsOf(w, r.data.taskById).length > 0)
  expect(found.map((w) => w.workflow_id)).toHaveLength(1)
  const w = found[0]!
  expect(w.state).toBe('SUCCEEDED')
  expect(needsOf(w, r.data.taskById)?.questions).toBe(0)
  const quiet = finishedQuestionsOf(w, r.data.taskById)
  const implement = w.steps.find((s) => s.step_id === 'implement')
  expect(implement?.task_id).toBeTruthy()
  expect(quiet.map((q) => [q.taskId, q.index])).toEqual([[implement!.task_id, 1]])
  return { w, taskId: implement!.task_id! }
}

describe("the fixture build's finished workflow with questions", () => {
  it('is on the list, SUCCEEDED, with one question behind its tick and none in Needs you', async () => {
    const { w, taskId } = await quietWorkflow()
    const page = await loadWorkflows()
    if (page.status !== 'ok') throw new Error(`the fixture workflows answered ${page.status}`)
    expect(page.data.workflows.map((x) => x.workflow_id)).toContain(w.workflow_id)
    const tasks = await loadTasks()
    if (tasks.status !== 'ok') throw new Error(`the fixture tasks answered ${tasks.status}`)
    const task = tasks.data.tasks.find((t) => t.id === taskId)
    expect(task?.state).toBe('SUCCEEDED')
    expect(task?.workflow_id).toBe(w.workflow_id)
    expect(task?.step_id).toBe('implement')
    expect(task?.result_summary?.questions).toBe(2)
  })

  it('serves its two questions, the first answered', async () => {
    const { taskId } = await quietWorkflow()
    const r = await loadQuestions(taskId)
    if (r.status !== 'ok') throw new Error(`the fixture questions answered ${r.status}`)
    expect(r.data.status).toBe('ok')
    expect(r.data.count).toBe(2)
    expect(r.data.questions.map((q) => q.answer?.question_index ?? null)).toEqual([0, null])
  })

  it("draws '? 1' beside its state on its page", async () => {
    const { w } = await quietWorkflow()
    render(<WorkflowsScreen view={`wf=${w.workflow_id}`} onView={() => {}} />)
    const head = await waitFor(
      () => {
        const h = document.querySelector<HTMLElement>('.wfp:not(.is-loading) .wfp-head')
        expect(h).toBeTruthy()
        return h!
      },
      { timeout: 5000 },
    )
    const tick = await within(head).findByRole('button', { name: '1 unanswered question' }, { timeout: 5000 })
    expect(tick.textContent?.replace(/\s+/g, '')).toBe('?1')
  })

  it('lists it, with the read size behind the footer tooltip', async () => {
    const { w } = await quietWorkflow()
    render(<WorkflowsScreen view="" onView={() => {}} />)
    const foot = await waitFor(
      () => {
        const f = document.querySelector<HTMLElement>('.wfl-foot')
        expect(f).toBeTruthy()
        return f!
      },
      { timeout: 5000 },
    )
    expect(document.querySelector(`tr.wfg-row[data-workflow="${w.workflow_id}"]`)).toBeTruthy()
    expect(foot.title).toBe('100 per read')
    expect(foot.textContent).not.toMatch(/per read/)
  })
})
