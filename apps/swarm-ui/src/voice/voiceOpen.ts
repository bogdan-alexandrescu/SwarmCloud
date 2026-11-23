// Who Voice it is open to (Section.tsx), apart from the panes so the spine
// reads it without loading them.
import type { Me } from '../types'

/**
 * WHO VOICE IT IS OPEN TO. Version one is one person's (§1: one budget, one
 * key, one history), and the person is the platform's owner. swarm-api's
 * session routes (§10, "swarm-api starts and ends a Voice it session for owner
 * only") will hold the allowed people in its settings and refuse anyone else
 * with a 403; until they land, the console's own identity read is all it has,
 * and the one thing it can say about the owner is that they are a platform
 * admin. THE SWITCH IS THIS FUNCTION: when the session routes serve whether
 * the caller may use Voice it, this reads that answer instead, and the spine
 * (Spine.tsx) and the panes below follow without another change.
 *
 * Unread is not "no": null, so nothing is drawn and then withdrawn.
 */
export function voiceItOpen(me: Me | null): boolean | null {
  return me === null ? null : me.principal.is_admin === true
}
