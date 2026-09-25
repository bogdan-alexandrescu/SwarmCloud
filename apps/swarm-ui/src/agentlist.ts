import type { TaskState } from './types'

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

/**
 * THE PAGE THE LIST READS AT PHONE WIDTH -- AND SO THE PAGE THE OVERVIEW
 * COUNTS OVER THERE (OV-10). Why 50 and why only on a phone is `Agents.tsx`'s
 * to say (docs/web-ui/03-agents-and-workflows.md §2.5: the payload of a
 * 200-row page on a 5-second poll).
 *
 * IT IS HERE, NOT IN `Agents.tsx`, BECAUSE THE OVERVIEW READS IT TOO. OV-10's
 * guarantee is that the failures item's figure and the list its link opens
 * describe one population, because the list filters client-side over the same
 * task read the check counts. When the list started reading 50 rows at phone
 * width and the Overview kept reading 200, "7 failed among the 200 most
 * recent" could open a list that said nothing failed. Both read this value at
 * `phoneWidth()` and the api's full page otherwise; one value, so the two
 * cannot drift apart again.
 */
export const PHONE_PAGE_LIMIT = 50
