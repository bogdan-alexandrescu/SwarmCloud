import { phoneWidth } from './HelpCard'

// THE AGENT PANEL'S HAND-OFF TO THE LIST, apart from Agents.tsx: AgentSplit
// calls it, and the drawer tests replace '../Agents' with a mock of
// AgentsScreen alone, so AgentSplit must not import from Agents.tsx (as
// agent-split/head.tsx does not, qa1fix32.head.test.tsx).

/**
 * OPENING ONE AGENT READS THAT AGENT FIRST (load-speed investigation, the
 * owner's pick 2026-11-01). `/agents/live/{task}` mounted the list and the
 * agent together, so the agent's task, attempts and events queued behind a
 * 200-row page read that took 5.3 s. Now the list's first read waits until the
 * agent's panel has rendered (`agentPanelShown`, called by AgentSplit once its
 * task and attempt reads have answered) and starts then; on a phone, where an
 * open agent covers the list, it waits until the agent is closed. While it
 * waits the list is `Screen`'s loading skeleton, never an empty list. Only the
 * FIRST read waits: a list already on screen when an agent opens stays.
 */
let panelOpening: string | null = null
let panelShownFor: string | null = null
export const panelWaiters = new Set<() => void>()

export function wakePanelWaiters(): void {
  for (const wake of [...panelWaiters]) wake()
}

/**
 * An agent panel for `taskId` is being drawn (AgentSplit, DURING ITS RENDER, so
 * it is known before any effect of the commit -- the list's first read among
 * them -- runs). A list with no panel coming beside it reads at once.
 */
export function agentPanelOpening(taskId: string): void {
  if (panelOpening === taskId) return
  panelOpening = taskId
  panelShownFor = null
}

/** The agent panel for `taskId` has rendered with its task and attempts read (AgentSplit). */
export function agentPanelShown(taskId: string): void {
  panelOpening = taskId
  panelShownFor = taskId
  wakePanelWaiters()
}

/** The agent panel for `taskId` is gone (AgentSplit unmounted or moved to another agent). */
export function agentPanelGone(taskId: string): void {
  if (panelOpening === taskId) panelOpening = null
  if (panelShownFor === taskId) panelShownFor = null
  wakePanelWaiters()
}

/** Whether the list may start its first read with `open` the agent in the address. */
export function listMayStart(open: string | null): boolean {
  if (open === null || panelOpening !== open) return true
  if (phoneWidth()) return false
  return panelShownFor === open
}
