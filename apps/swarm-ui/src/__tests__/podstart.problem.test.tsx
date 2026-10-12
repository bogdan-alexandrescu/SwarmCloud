// LANE F1-POD: A TASK WHOSE POD CANNOT START SAYS WHY.
//
// Measured 2026-10-11 20:58-21:05 PDT: every claude-code-browser pod's sidecar
// crash-looped ("No module named agent_worker.livegate.__main__") for about
// three minutes while the task page read only "dispatched" / "account not
// assigned yet" -- and after the cancel, "Elapsed: never ran", although the
// pod had started. The reconciler now records `start_problem` on the task and
// the API serves it; this is what the console makes of it:
//
//   * one line on the agent's header, "Pod not starting: <container> <reason>
//     · restarted N× · <message>", while the task waits to run;
//   * nothing once the task runs, and nothing without a record;
//   * after a cancel, a task whose pod started reads "started, did not reach
//     running" -- not "never ran";
//   * AgentSplit mounts it in one line.
//
// MUTATION: drop the `pod_started` branch from `elapsed()`. The cancel test fails.
// MUTATION: drop the mount from AgentSplit. The mount test fails.

import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'

import SPLIT from '../AgentSplit.tsx?raw'
import { PodStartProblem, podStartLine } from '../PodStartProblem'
import { elapsed, STARTED_NOT_RUNNING, type StartProblem, type Task, type TaskState } from '../types'
import { at, task as baseTask } from './runfixture'

const SIDECAR: StartProblem = {
  container: 'livegate',
  reason: 'CrashLoopBackOff',
  restarts: 4,
  message: 'back-off 40s restarting failed container=livegate',
  pod_started: true,
  seen_at: at(3),
}

function task(state: TaskState, over: Partial<Task> = {}): Task {
  return baseTask({ id: 'task_f1pod0000000000001', state, start_problem: SIDECAR, ...over })
}

describe('the pod start problem line', () => {
  it('says which container, why, how often and what kubelet said', () => {
    expect(podStartLine(task('DISPATCHED'))).toBe(
      'Pod not starting: livegate CrashLoopBackOff · restarted 4× · back-off 40s restarting failed container=livegate',
    )
  })

  it('renders as one warning on a DISPATCHED or STARTING task', () => {
    for (const state of ['DISPATCHED', 'STARTING'] as const) {
      const { container, unmount } = render(<PodStartProblem task={task(state)} />)
      const line = container.querySelector('[role="status"]')
      expect(line, `${state}: no warning`).not.toBeNull()
      expect(line!.textContent).toContain('Pod not starting: livegate CrashLoopBackOff')
      expect(line!.textContent).toContain('restarted 4×')
      unmount()
    }
  })

  it('leaves the restart count out when nothing restarted', () => {
    const pull = { ...SIDECAR, container: 'worker', reason: 'ImagePullBackOff', restarts: 0, message: 'pull failed' }
    expect(podStartLine(task('DISPATCHED', { start_problem: pull }))).toBe(
      'Pod not starting: worker ImagePullBackOff · pull failed',
    )
  })

  it('draws nothing without a record, or once the task runs', () => {
    expect(podStartLine(task('DISPATCHED', { start_problem: null }))).toBeNull()
    expect(podStartLine(task('DISPATCHED', { start_problem: undefined }))).toBeNull()
    expect(podStartLine(task('RUNNING', { started_at: at(5) }))).toBeNull()
    const { container } = render(<PodStartProblem task={task('RUNNING', { started_at: at(5) })} />)
    expect(container.textContent).toBe('')
  })

  it('says, in the past tense, why a cancelled task never ran its agent', () => {
    expect(podStartLine(task('CANCELLED', { completed_at: at(7) }))).toBe(
      'Pod did not start: livegate CrashLoopBackOff · restarted 4× · back-off 40s restarting failed container=livegate',
    )
  })
})

describe('the elapsed figure of a task whose pod started', () => {
  it('is "started, did not reach running" after a cancel, not "never ran"', () => {
    const el = elapsed(task('CANCELLED', { completed_at: at(7) }), Date.parse(at(8)))
    expect(STARTED_NOT_RUNNING).toBe('started, did not reach running')
    expect(el.text).toBe(STARTED_NOT_RUNNING)
    expect(el.ticking).toBe(false)
  })

  it('is still "never ran" when no pod started', () => {
    const noPod = { ...SIDECAR, pod_started: false }
    expect(elapsed(task('CANCELLED', { completed_at: at(7), start_problem: noPod }), Date.parse(at(8))).text).toBe('never ran')
    expect(elapsed(task('CANCELLED', { completed_at: at(7), start_problem: null }), Date.parse(at(8))).text).toBe('never ran')
  })
})

describe('the mount', () => {
  it('is one line in AgentSplit, beside the hand-off banner', () => {
    const mounts = SPLIT.split('\n').filter((l) => l.includes('<PodStartProblem'))
    expect(mounts).toHaveLength(1)
  })
})
