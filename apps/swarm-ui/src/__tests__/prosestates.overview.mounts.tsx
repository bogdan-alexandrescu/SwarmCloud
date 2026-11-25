// The overview section's click-only states the fixture build never draws
// (issue 439): each screen mounted on hand-built reads, so one click opens
// the state. Copied from the tests that build the same reads:
// qa.vqa2.r12.overview (Running now past eight rows), overview.failures.age
// (Recent failures' rows) and interviewPage (a question with designs).

import { useState, type ReactElement } from 'react'
import { vi } from 'vitest'

import type { Result } from '../fetch'
import type { TaskPage } from '../types'
import { task } from './runfixture'

const MIN = 60_000
const ago = (m: number): string => new Date(Date.now() - m * MIN).toISOString()

function page(tasks: TaskPage['tasks']): Result<TaskPage> {
  return { status: 'ok', data: { tasks, tenant_id: 'eng', next_page_token: null }, fetchedAt: Date.now() }
}

/** The fixture build's example-plans switch (api.ts reads it on import): its PLANNED runs are the approval inbox. */
const EXAMPLE_PLANS = 'swarm_fixture_example_plans'
/** Its proposing-sources switch: adds the one schedule whose merges a person approves. */
const PROPOSING = 'swarm_fixture_proposing'
const MERGING = 'sch_6e2d0a3b8c51'

/** Undo the mounts' module mocks: called after each state. */
export function restoreOverviewStubs(): void {
  for (const c of [EXAMPLE_PLANS, PROPOSING]) document.cookie = `${c}=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT`
  vi.doUnmock('../api')
  vi.resetModules()
}

/** Overview with 12 agents holding capacity: Running now lists eight, then "4 more". */
export async function mountRunningMore(): Promise<ReactElement> {
  vi.resetModules()
  const tasks = Array.from({ length: 12 }, (_, i) => task({ id: `tsk_h${i}`, state: 'RUNNING', started_at: ago(i + 1), step_id: `s${i}` }))
  vi.doMock('../api', async (importOriginal) => ({
    ...(await importOriginal<typeof import('../api')>()),
    loadTasks: () => Promise.resolve(page(tasks)),
  }))
  const { OverviewScreen } = await import('../Overview')
  return <OverviewScreen />
}

/** Recent failures with nine failed agents and two cancelled: six rows, then "3 more" and "2 cancelled". */
export async function mountFailures(): Promise<ReactElement> {
  const { RecentFailures } = await import('../OverviewRegions')
  const failed = Array.from({ length: 9 }, (_, i) => task({ id: `tsk_f${i}`, state: 'FAILED', updated_at: ago(i + 1), completed_at: ago(i + 1), step_id: `s${i}` }))
  const cancelled = Array.from({ length: 2 }, (_, i) => task({ id: `tsk_c${i}`, state: 'CANCELLED', updated_at: ago(i + 20), completed_at: ago(i + 20) }))
  return <RecentFailures tasks={page([...failed, ...cancelled])} />
}

const design = (word: string): string => `<!doctype html><html><body><h1>${word}</h1></body></html>`

/** A designer's question with two designs drawn small: a click opens one full size. */
export async function mountInterviewDesign(): Promise<ReactElement> {
  const { InterviewView } = await import('../InterviewPage')
  const questions = {
    task_id: 't_design',
    count: 1,
    status: 'ok',
    redacted: false,
    redaction_count: 0,
    may_answer: true,
    may_answer_because: null,
    questions: [
      {
        question: 'Where does the timer sit?',
        context: null,
        recommended: 'Summary',
        options: [
          { label: 'Summary', description: 'In the order summary', preview: design('summary') },
          { label: 'Header', description: 'In the page header', preview: design('header') },
        ],
        answer: null,
        blocking: true,
      },
    ],
    waiting: { open: true, blocking: [0], unanswered: [0], asked_at: ago(30), expires_at: null, issue_comment_url: null, skill: 'design-html' },
  } as unknown as import('../InterviewPage').InterviewQuestions
  const designTask = task({
    id: 't_design',
    state: 'PARKED',
    workflow_id: 'wf_checkout',
    step_id: 'design',
    metadata: { work_contract: { capabilities: { plugins: { gstack: { mode: 'designer', skills: ['design-html'] } } } } },
  })
  return (
    <InterviewView
      taskId="t_design"
      go={() => undefined}
      read={{ questions, task: designTask, catalogue: [{ name: 'gstack', version: '1.92.55', runner_profiles: [], modes: {} }] }}
    />
  )
}

/** The fixture's merging schedule, read on a fresh api module (the page's fake clock holds the read's 90 ms). */
async function merging(): Promise<import('../api').Schedule> {
  const api = await import('../api')
  const read = api.loadSchedule(MERGING)
  await vi.advanceTimersByTimeAsync(200)
  const r = await read
  if (r.status !== 'ok') throw new Error(`the fixture build serves no schedule ${MERGING}`)
  return r.data.schedule
}

/** A schedule's delete refused by the server: the fixture build deletes every schedule. */
export async function mountDeleteRefused(): Promise<ReactElement> {
  document.cookie = `${PROPOSING}=on; path=/`
  vi.resetModules()
  vi.doMock('../api', async (importOriginal) => ({
    ...(await importOriginal<typeof import('../api')>()),
    deleteSchedule: () =>
      Promise.resolve({ status: 'error', error: { kind: 'conflict', httpStatus: 409, code: 'schedule_busy', message: 'schedule is firing' } }),
  }))
  const s = await merging()
  const { ScheduleActions } = await import('../Schedules')
  return <ScheduleActions s={s} onDone={() => undefined} />
}

/** The merge switch of a schedule whose merges a person approves. */
export async function mountMergeSwitch(): Promise<ReactElement> {
  document.cookie = `${PROPOSING}=on; path=/`
  vi.resetModules()
  // Its type may merge unattended: the switch is offered (the fixture's types never merge unattended).
  vi.doMock('../api', async (importOriginal) => {
    const actual = await importOriginal<typeof import('../api')>()
    return {
      ...actual,
      loadScheduleTypes: async () => {
        const r = await actual.loadScheduleTypes()
        if (r.status !== 'ok') return r
        return { ...r, data: { types: r.data.types.map((t) => ({ ...t, floor_gate: { ...t.floor_gate, merge: 'auto' } })) } }
      },
    }
  })
  const s = await merging()
  const { MergeSwitch } = await import('../ScheduleDetail')
  return <MergeSwitch s={s} onChanged={() => undefined} go={() => undefined} />
}

/**
 * One approval's decision page on the fixture build's example plans
 * (approvals.fixture-plans), at its address: Approve, Reject and the folds
 * are the clicks.
 */
export async function mountApproval(): Promise<ReactElement> {
  document.cookie = `${EXAMPLE_PLANS}=on; path=/`
  vi.resetModules()
  const { ApprovalsScreen } = await import('../Approvals')
  function Inbox(): ReactElement {
    const [view, setView] = useState<string | null>('approval=run:run_5a1d3e70')
    return <ApprovalsScreen view={view} go={(to) => setView(to.includes('?') ? to.slice(to.indexOf('?') + 1) : null)} />
  }
  return <Inbox />
}
