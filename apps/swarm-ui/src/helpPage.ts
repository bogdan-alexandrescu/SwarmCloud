// The Help page a route tail opens (HelpSection.tsx), apart from the page so
// App.tsx lights the panel's entry without loading it.
import { HELP, HELP_GROUPS, TOPIC_IDS, topicFor, type HelpGroupId } from './help'

/**
 * The group page a Help route tail opens: the group itself, a topic's group,
 * or -- for `/help` and for a topic this build lacks -- the first group, so a
 * page is always one group and the panel always marks it. App.tsx reads this
 * to light the panel's entry.
 */
export function helpPageOf(tail: string): HelpGroupId {
  const named = HELP_GROUPS.find((g) => g.id === tail)
  if (named !== undefined) return named.id
  const t = topicFor(tail)
  if (t !== null) return t.group
  return HELP_GROUPS.find((g) => TOPIC_IDS.some((id) => HELP[id].group === g.id))!.id
}
