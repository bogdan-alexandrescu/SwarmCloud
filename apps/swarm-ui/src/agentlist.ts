import { useSyncExternalStore } from 'react'

import type { Task, TaskState } from './types'

/**
 * THE AGENT LIST'S ADDRESS: which tab, and on Recent which state (OV-10).
 *
 * The tab was component state the hash could not carry, so the Overview's
 * failed-agents item could only say "agents · Recent tab" and land on whatever
 * the list opened on -- Live, whenever anything ran. The owner's decision
 * (epic #81, OV-10) makes both an address:
 *
 *   #work/running/<live|waiting|recent>
 *   #work/running/recent/<failed|cancelled|succeeded>
 *
 * ONE MODULE, BECAUSE TWO FILES READ IT. `App.tsx` parses and writes the
 * address and `Agents.tsx` draws the tabs and the state segment; a second
 * spelling of either list is how an address comes to name a tab the screen
 * does not have. It is not in `Agents.tsx` because App's route parser needs
 * the values at runtime and `nav.links.test.tsx` replaces that module with a
 * recorder -- a value imported from it would be undefined there.
 */

/** The three tabs, in the order the screen draws them and lands on them. */
export const AGENT_TABS = ['live', 'waiting', 'recent'] as const
export type AgentTab = (typeof AGENT_TABS)[number]

/**
 * The Recent tab's state filter, in the order the segment offers it.
 *
 * DEAD_LETTERED IS NOT HERE, and not by oversight: nothing writes it
 * (`types.ts` NEVER_WRITTEN), so offering it would be a filter that can only
 * ever answer "none". It still shows under "all", where a row in it would be
 * the news.
 */
export const RECENT_STATES = ['failed', 'cancelled', 'succeeded'] as const
export type RecentState = (typeof RECENT_STATES)[number]

/** The terminal state each filter word selects. */
export const RECENT_STATE_OF: Readonly<Record<RecentState, TaskState>> = {
  failed: 'FAILED',
  cancelled: 'CANCELLED',
  succeeded: 'SUCCEEDED',
}

/** A list address: a tab, and a state only when the tab is Recent. */
export interface AgentList {
  tab: AgentTab
  state: RecentState | null
}

function isTab(s: string | undefined): s is AgentTab {
  return (AGENT_TABS as readonly string[]).includes(s ?? '')
}

function isRecentState(s: string | undefined): s is RecentState {
  return (RECENT_STATES as readonly string[]).includes(s ?? '')
}

/**
 * The segments after `running/`, read as a list address -- or null.
 *
 * NULL FOR ANYTHING ELSE, never a best guess. `running/live/failed` names a
 * state on a tab that has no state filter and `running/recent/faild` names a
 * state that does not exist; reading either as its nearest neighbour would
 * make a mistyped link look like a working one, which is worse than landing
 * on the plain list.
 */
export function parseAgentList(rest: readonly string[]): AgentList | null {
  const [tab, state, ...more] = rest
  if (more.length > 0 || !isTab(tab)) return null
  if (state === undefined) return { tab, state: null }
  if (tab === 'recent' && isRecentState(state)) return { tab, state }
  return null
}

/** The segments a list address is written as, after `running/`. */
export function agentListPath(list: AgentList): string {
  return list.tab === 'recent' && list.state !== null ? `${list.tab}/${list.state}` : list.tab
}

// `PHONE_PAGE_LIMIT` lives in `pageLimits.ts`, which the pure check layer
// (checks.ts) imports too; re-exported here for the screens that read it.
export { PHONE_PAGE_LIMIT } from './pageLimits'

/**
 * THE WORD ON THE AGENT PAGE'S BACK LINK: the list tab it returns to.
 *
 * agents.html V1 (decided 2026-10-01): on a phone the list and the agent are
 * two pages, and the agent page leads with `‹ Waiting` -- the tab the reader
 * came from, read off the list address the drawer closes to. An address that
 * names no tab (the list before it has landed anywhere) goes back to Agents.
 */
export function backLabel(listAddress: string): string {
  const m = /running\/(live|waiting|recent)(?:\/|$)/.exec(listAddress)
  const tab = m?.[1]
  return tab === undefined ? 'Agents' : tab.charAt(0).toUpperCase() + tab.slice(1)
}

/** A task id's first eight characters after its `task_` prefix: what a row has room for. */
export function shortTaskId(id: string): string {
  const cut = id.indexOf('_')
  return (cut === -1 ? id : id.slice(cut + 1)).slice(0, 8)
}

/**
 * WHAT A LONE AGENT IS, in words: `browser check` for the browser runner, and
 * `<profile> task` for every other. The profile is the caller's choice by
 * name (invariant 10), so it is the one thing that says what the agent was
 * asked to be.
 */
export function agentKind(profile: string): string {
  return profile === 'browser' ? 'browser check' : `${profile} task`
}

/**
 * WHAT AN AGENT IS CALLED (#94): its step, when a workflow named it. Four
 * running steps of one workflow are `scan-01`, `scan-04`, `scan-05` and
 * `scan-06`, not four `claude-code task_…`.
 *
 * A LONE AGENT IS NAMED FROM WHAT IT IS, then its short id (walkthrough G,
 * owner 2026-10-03): `browser check · 02e9705a`, `claude-code task ·
 * a073aff5`. It was its bare id, and a row of hashes says nothing about what
 * any of them is. The whole id stays one hover or one copy away wherever the
 * name is drawn. One rule for the agent list, Overview, the Timeline and the
 * breadcrumb, so the four never name one agent two ways.
 */
export function agentName(task: Pick<Task, 'id' | 'step_id' | 'runner_profile'>): string {
  return task.step_id ?? `${agentKind(task.runner_profile)} · ${shortTaskId(task.id)}`
}

/**
 * THE NAMES OF AGENTS THIS TAB HAS READ, for the one place that has an id and
 * no task: the breadcrumb (App.tsx `Head`). The open agent's split reads the
 * task and records its name here, and the crumb redraws with it; until then
 * the crumb says the id, which is all anyone knows.
 */
const knownNames = new Map<string, string>()
const nameListeners = new Set<() => void>()

export function rememberAgentName(task: Pick<Task, 'id' | 'step_id' | 'runner_profile'>): void {
  const name = agentName(task)
  if (knownNames.get(task.id) === name) return
  knownNames.set(task.id, name)
  for (const l of nameListeners) l()
}

export function useAgentName(taskId: string | null): string | null {
  return useSyncExternalStore(
    (on) => {
      nameListeners.add(on)
      return () => nameListeners.delete(on)
    },
    () => (taskId === null ? null : (knownNames.get(taskId) ?? null)),
    () => null,
  )
}

/** The address of one workflow's page: App routes it to `/workflows/<id>`. */
export function workflowHref(workflowId: string): string {
  return `#work/workflows?wf=${encodeURIComponent(workflowId)}`
}
