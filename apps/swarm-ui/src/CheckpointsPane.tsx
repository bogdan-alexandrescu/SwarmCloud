/**
 * THE CHECKPOINTS TAB (agents.html V1, decided 2026-10-01). The checkpoint
 * browser stops being a panel inside Details and becomes the agent's fourth
 * tab, at `/agents/<tab>/<id>/checkpoints`.
 *
 * Nothing new is read or drawn here: it is `RunFiles` -- the checkpoint
 * listing, the latest-pointer finding and, per row, the `CheckpointBrowser`
 * that opens one -- given the task it needs. `RunFiles` owns its own reads and
 * its own absent states; this owns only the task read in front of it.
 */
import { useCallback } from 'react'
import { loadTask } from './api'
import { RunFiles } from './RunFiles'
import { Screen } from './Shell'

export function CheckpointsPane({ taskId }: { taskId: string }) {
  // One function per agent, so the Screen does not re-read on every render.
  const load = useCallback(() => loadTask(taskId), [taskId])
  return (
    <Screen title="Checkpoints" load={load}>
      {(task, reading) => <RunFiles task={task} readAt={reading.fetchedAt} now={null} />}
    </Screen>
  )
}
