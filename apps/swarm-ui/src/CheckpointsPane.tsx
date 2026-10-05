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
import { TERMINAL_STATES, type AttemptRow, type Task } from './types'

/**
 * WHILE THE AGENT RUNS, THE PANE RE-READS (owner QA D21, 2026-10-04): it read
 * once, so the tab said `2 of 2` while the agent went on writing. It re-reads
 * at the detail's cadence (10s, `AgentDetail.DRAWER_POLL_MS`) until the task
 * is finished, and the Details tile draws this same read (AgentSplit).
 */
export const CHECKPOINTS_POLL_MS = 10_000

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
 * WHAT THE TAB AND THE PANE SAY ABOUT ONE READ (U10a D21, U11a): how many the
 * attempt records say were WRITTEN and how many the bucket listing KEEPS.
 * `kept` is null for a listing that was cut, paged, failed or is still
 * reading -- never a count; `written` is null when the attempt records could
 * not be read.
 */
export interface CheckpointCount {
  kept: number | null
  written: number | null
}

/** `1 written, 0 kept`: the words Details, the tab and the pane all use. */
export function checkpointsLine(c: CheckpointCount): string | null {
  if (c.kept === null || c.written === null) return null
  return `${c.written} written, ${c.kept} kept`
}

/** The Checkpoints tab count's reason: why it is a dash, or what it counts. */
export function checkpointsSay(c: CheckpointCount | null): string {
  if (c === null) return 'The checkpoint listing has not answered yet, so the count is not known.'
  if (c.kept === null) return 'The bucket listing was cut, paged or not read, so how many checkpoints it keeps is not known.'
  const line = checkpointsLine(c)
  if (line === null) return `${c.kept} kept in the bucket. The attempt records were not read, so how many were written is not known.`
  return c.written === c.kept
    ? `${line}: the count is what the bucket keeps.`
    : `${line}: the count is what the bucket keeps. The attempt records name ${c.written} written; a checkpoint the platform reclaimed is no longer kept.`
}

/**
 * `onCount` hears the read's count for the tab's badge (U10a D21): the tab
 * said `–` beside a pane that said `found 1 of 1`. Only a listing read to its
 * end is a kept count; a cut or paged one, a failed one and one still
 * reading are null, which the tab draws as a dash with its reason.
 *
 * `ag-ckpts` scopes the stacked table's layout in this tab (agents.css):
 * Size and Age side by side, and the objects button beside the size.
 */
export function CheckpointsPane({ taskId, onCount }: { taskId: string; onCount?: (c: CheckpointCount | null) => void }) {
  // One function per agent, so the Screen does not re-read on every render.
  const load = useCallback(() => loadCheckpointsRead(taskId), [taskId])
  return (
    <div className="ag-ckpts">
      <Screen title="Checkpoints" load={load} pollMs={(d) => (d !== null && TERMINAL_STATES.has(d.task.state) ? null : CHECKPOINTS_POLL_MS)}>
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
  onCount: ((c: CheckpointCount | null) => void) | undefined
}) {
  const listing = useCheckpointListing(read.task, read.attempts, readAt)
  const page = listing.state.status === 'ok' || listing.state.status === 'stale' ? listing.state.data : null
  const whole = page !== null && page.listed && !page.truncated && page.next_page_token === null
  const kept = whole ? page.count : null
  const written = read.attempts === null ? null : read.attempts.reduce((t, a) => t + a.checkpoints.length, 0)
  const answered = listing.state.status !== 'loading'
  useEffect(() => {
    onCount?.(answered ? { kept, written } : null)
  }, [answered, kept, written, onCount])
  const line = answered ? checkpointsLine({ kept, written }) : null
  return (
    <>
      {line !== null && (
        <p className="ctl-sub ag-ckpts-sum" title={checkpointsSay({ kept, written })}>
          {line}
        </p>
      )}
      <RunFiles task={read.task} attempts={read.attempts} listing={listing} now={null} />
    </>
  )
}
