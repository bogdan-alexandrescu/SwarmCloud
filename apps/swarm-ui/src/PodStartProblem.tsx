// F1-POD: A TASK WHOSE POD CANNOT START SAYS WHY.
//
// On 2026-10-11 between 20:58 and 21:05 PDT every claude-code-browser pod's
// sidecar crash-looped ("No module named agent_worker.livegate.__main__") for
// about three minutes, while this page read only "dispatched" / "account not
// assigned yet". Nobody could tell why the task was stuck. The reconciler now
// reads the pod of a DISPATCHED or STARTING task and records the worst
// container on the task (`reconciler.startproblem`); the API serves it as
// `start_problem`. This draws it as one line over whichever pane is open,
// beside the hand-off banner.
//
// A RECORD, NOT A STATE. The task is still DISPATCHED or STARTING and still
// holds its slot; this only says why it has not moved. It is drawn while the
// task waits to run, and on a finished task that never ran (cancelled while
// its pod crash-looped), where it is the only account of what happened. Once
// the task runs the reconciler clears it, and a task that started is not
// described by its earlier attempt's pod.

import type { Task } from './types'
import { TERMINAL_STATES } from './types'

/** The states in which a pod exists and its worker has not yet run. */
const STARTING_STATES: ReadonlySet<string> = new Set(['DISPATCHED', 'STARTING'])

/**
 * The one line, or null when there is nothing to say: no record, or a task
 * that is running or ran.
 * `Pod not starting: <container> <reason> · restarted N× · <message>`, the
 * restart count left out when nothing restarted (an image pull failure has
 * none), and in the past tense on a finished task.
 */
export function podStartLine(task: Task): string | null {
  const p = task.start_problem
  if (p === null || p === undefined) return null
  const waiting = STARTING_STATES.has(task.state)
  const ended = TERMINAL_STATES.has(task.state) && task.started_at === null
  if (!waiting && !ended) return null
  const head = [p.container, p.reason].filter((x): x is string => typeof x === 'string' && x !== '').join(' ')
  const parts = [`${waiting ? 'Pod not starting' : 'Pod did not start'}: ${head || 'a container'}`]
  if (typeof p.restarts === 'number' && p.restarts > 0) parts.push(`restarted ${p.restarts}×`)
  if (typeof p.message === 'string' && p.message.trim() !== '') parts.push(p.message.trim())
  return parts.join(' · ')
}

/** The warning on the agent's header. Renders nothing when `podStartLine` has nothing to say. */
export function PodStartProblem({ task }: { task: Task }) {
  const line = podStartLine(task)
  if (line === null) return null
  return (
    <p className="warn-text" role="status" title={line}>
      {line}
    </p>
  )
}
