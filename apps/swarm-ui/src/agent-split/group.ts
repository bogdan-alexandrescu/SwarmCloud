import { useEffect } from 'react'

import type { AgentTab } from '../agentlist'
import { CONCURRENCY_STATES, TERMINAL_STATES, type Task, type TaskState } from '../types'

// THE LIST TAB THE AGENT IS IN (moved out of AgentSplit.tsx by SPEED-D4A,
// 2026-10-14, unchanged).

/** Tells the router the list tab the task's state puts it in, when `closeTo` names another. */
export function useListGroup(
  task: Task | null,
  taskId: string,
  closeTo: string,
  onGroup: ((tab: AgentTab) => void) | undefined,
): void {
  // THE BACK LINK AND THE LIST FOLLOW THE TASK (QA12-26, live-browser QA p12):
  // an agent opened under Live that parks is a Waiting agent, and it kept
  // `‹ Live` over a list that no longer held it. Only this agent's own read
  // counts, never the previous agent's while the next one loads.
  const group = task !== null && task.id === taskId ? agentTabOf(task.state) : null
  useEffect(() => {
    if (group === null || onGroup === undefined || listTabOf(closeTo) === group) return
    onGroup(group)
  }, [group, closeTo, onGroup])
}

/**
 * The list tab a state puts a task in -- the Agents list's own split
 * (CONTRACT.md invariant 1): an attempt in flight is Live, a terminal state is
 * Recent, and QUEUED, READY and PARKED, which hold nothing, are Waiting.
 */
export function agentTabOf(state: TaskState): AgentTab {
  if (CONCURRENCY_STATES.has(state)) return 'live'
  if (TERMINAL_STATES.has(state)) return 'recent'
  return 'waiting'
}

/** The list tab a list address names, as `backLabel` reads it; null for the plain list. */
function listTabOf(listAddress: string): AgentTab | null {
  const m = /running\/(live|waiting|recent)(?:[/?]|$)/.exec(listAddress)
  return m === null ? null : (m[1] as AgentTab)
}
