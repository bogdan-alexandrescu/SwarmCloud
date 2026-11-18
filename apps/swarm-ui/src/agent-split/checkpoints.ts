import { useCallback, useState } from 'react'

import type { CheckpointCount } from '../CheckpointsPane'

// THE CHECKPOINTS TAB'S COUNT (moved out of AgentSplit.tsx by SPEED-D4A,
// 2026-10-14, unchanged).

/** The Checkpoints pane's read for this task, the tab's count, and the pane's `onCount`. */
export function useCheckpointCount(taskId: string): {
  ckpt: CheckpointCount | null
  ckptCount: number | string | null
  onCheckpoints: (c: CheckpointCount | null) => void
} {
  // THE CHECKPOINTS TAB'S COUNT IS ITS PANE'S READ (U10a D21, owner QA
  // 2026-10-04, and again in U11a): the tab said `–` until it was opened,
  // then `0`, while Details said `1 written · 0 in bucket`. The pane is
  // mounted with the split, hidden until its tab is chosen, so its one read
  // is both the count and the content from the start; the count is what the
  // bucket keeps, and its reason says what was written beside it in the
  // words Details uses: `1 written, 0 kept`.
  const [ckpts, setCkpts] = useState<{ taskId: string; c: CheckpointCount } | null>(null)
  const onCheckpoints = useCallback((c: CheckpointCount | null) => setCkpts(c === null ? null : { taskId, c }), [taskId])
  const ckpt = ckpts !== null && ckpts.taskId === taskId ? ckpts.c : null
  // KEPT OF WRITTEN WHEN THE TWO DIFFER (G2-12, QA 2026-10-07): on a
  // succeeded review the tab said `0` while Details and Attempts said 3, the
  // three having been reclaimed once the task finished. `0 of 3` says both.
  // Only when fewer are kept than written: a bucket holding more than the
  // records name (records not yet written, or older ones) keeps its count.
  const ckptCount: number | string | null =
    ckpt === null ? null : ckpt.kept !== null && ckpt.written !== null && ckpt.kept < ckpt.written ? `${ckpt.kept} of ${ckpt.written}` : ckpt.kept
  return { ckpt, ckptCount, onCheckpoints }
}
