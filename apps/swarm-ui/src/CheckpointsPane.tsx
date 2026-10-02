/**
 * THE CHECKPOINTS TAB (agents.html V1, decided 2026-10-01). The checkpoint
 * browser stops being a panel inside Details and becomes the agent's fourth
 * tab, at `/agents/<tab>/<id>/checkpoints`.
 *
 * It is `RunFiles` -- the checkpoint listing, the latest-pointer finding and,
 * per row, the `CheckpointBrowser` that opens one -- given the task AND the
 * attempt records. The attempt records are what let the listing tell "written,
 * then reclaimed" from a real zero; Details used to hand them over, and since
 * this tab is now the only checkpoint panel it reads them itself. A failed
 * attempts read is passed as `null`, which `RunFiles` already words as "could
 * not be read" -- true here, because the read was made and failed.
 */
import { useCallback } from 'react'
import { loadAttempts, loadTask } from './api'
import type { Result } from './fetch'
import { RunFiles } from './RunFiles'
import { Screen } from './Shell'
import type { AttemptRow, Task } from './types'

interface CheckpointsRead {
  task: Task
  attempts: AttemptRow[] | null
}

export async function loadCheckpointsRead(taskId: string): Promise<Result<CheckpointsRead>> {
  const [task, attempts] = await Promise.all([loadTask(taskId), loadAttempts(taskId)])
  const rows = attempts.status === 'ok' || attempts.status === 'stale' ? attempts.data.attempts : attempts.status === 'empty' ? [] : null
  switch (task.status) {
    case 'ok':
      return { ...task, data: { task: task.data, attempts: rows } }
    case 'stale':
      return { ...task, data: { task: task.data, attempts: rows } }
    default:
      return task
  }
}

export function CheckpointsPane({ taskId }: { taskId: string }) {
  // One function per agent, so the Screen does not re-read on every render.
  const load = useCallback(() => loadCheckpointsRead(taskId), [taskId])
  return (
    <Screen title="Checkpoints" load={load}>
      {(read, reading) => <RunFiles task={read.task} attempts={read.attempts} readAt={reading.fetchedAt} now={null} />}
    </Screen>
  )
}
