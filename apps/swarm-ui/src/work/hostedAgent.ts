import { createContext, type ReactNode } from 'react'

/**
 * THE OPEN AGENT, HOSTED IN THE AGENTS PAGE'S DOCKED INSPECTOR (the owner's
 * pick on #3146, 2026-11-03: "Host it in the inspector"). App keeps the route
 * and builds the agent's view (`AgentSplit`) with the props the address gives
 * it; on a page that hosts it, App hands the view to the page through this
 * context instead of mounting it as a column beside the page, and the page
 * puts it in its inspector -- the inspector's drag handle, fold and phone
 * sheet are the agent's, and the list is the left pane that never moves.
 *
 * Null wherever no agent is open or the page does not host it.
 */
export interface HostedAgentView {
  /** The open agent's id. */
  taskId: string
  /** The agent's view, `AgentSplit` with `hosted`. */
  view: ReactNode
  /** Close the agent: back to the list address it was opened from. */
  close: () => void
}

export const HostedAgent = createContext<HostedAgentView | null>(null)

/** Whether the page at this section and tab hosts an open agent in its inspector: the Agents page. */
export function hostsAgent(sectionId: string, tab: string): boolean {
  return `${sectionId}/${tab}` === 'work/running'
}
