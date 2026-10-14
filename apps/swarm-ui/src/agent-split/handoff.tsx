import { HandoffParkedSince } from '../AgentDetail'
import { HandoffStrip } from '../Handoff'
import type { LiveSession } from '../api'
import { LiveControl } from '../LiveBrowser'
import type { Task } from '../types'
import { Button } from '../components'

// THE HAND-OFF BANNER OVER THE PANE (moved out of AgentSplit.tsx by
// SPEED-D4A, 2026-10-14, unchanged; AgentSplit.tsx says where it is drawn).

/** The banner, with Take control and Open in theater while the agent's browser session is open. */
export function AgHandoff({
  task,
  taskId,
  sessionOpen,
  liveSession,
  reloadLive,
  onReady,
  onTheater,
}: {
  task: Task
  taskId: string
  sessionOpen: boolean
  liveSession: LiveSession | null
  reloadLive: () => void
  onReady: () => void
  onTheater: () => void
}) {
  return (
    <HandoffStrip task={task} onReady={onReady}>
      <HandoffParkedSince task={task} />
      {sessionOpen && (
        <>
          <LiveControl taskId={taskId} session={liveSession} reload={reloadLive} takeOnly />
          <Button size="sm" onClick={onTheater}>
            Open in theater
          </Button>
        </>
      )}
    </HandoffStrip>
  )
}
