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
import { useCallback, useEffect } from 'react'
import { loadAttempts, loadTask } from './api'
import type { Result } from './fetch'
import { RunFiles, useCheckpointListing } from './RunFiles'
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

/**
 * `onCount` hears the listing's checkpoint count, for the tab's badge (U10a
 * D21): the tab said `–` beside a pane that said `found 1 of 1`. Only a
 * listing read to its end is a count; a cut or paged one, a failed one and
 * one still reading are null, which the tab draws as a dash with its reason.
 *
 * `ag-ckpts` scopes the stacked table's layout in this tab (agents.css):
 * Size and Age side by side, and the objects button beside the size.
 */
export function CheckpointsPane({ taskId, onCount }: { taskId: string; onCount?: (n: number | null) => void }) {
  // One function per agent, so the Screen does not re-read on every render.
  const load = useCallback(() => loadCheckpointsRead(taskId), [taskId])
  return (
    <div className="ag-ckpts">
      <Screen title="Checkpoints" load={load}>
        {(read, reading) => <Listed read={read} readAt={reading.fetchedAt} onCount={onCount} />}
      </Screen>
    </div>
  )
}

/**
 * The listing, read here so its count can reach the tab; `RunFiles` draws
 * this same read (`listing`), so the badge and the pane are one answer. The
 * attempt records go with it, as they always have.
 */
function Listed({
  read,
  readAt,
  onCount,
}: {
  read: CheckpointsRead
  readAt: number
  onCount: ((n: number | null) => void) | undefined
}) {
  const listing = useCheckpointListing(read.task, read.attempts, readAt)
  const page = listing.state.status === 'ok' || listing.state.status === 'stale' ? listing.state.data : null
  const whole = page !== null && page.listed && !page.truncated && page.next_page_token === null
  const n = whole ? page.count : null
  useEffect(() => {
    onCount?.(n)
  }, [n, onCount])
  return <RunFiles task={read.task} attempts={read.attempts} listing={listing} now={null} />
}
