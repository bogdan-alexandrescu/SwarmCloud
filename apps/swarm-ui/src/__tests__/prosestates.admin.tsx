// The admin section's click-only states (issue 439; the harness and the rule
// are in prosestates.tsx). People: the recent admin actions opened.
// The section's own step registers the rest and takes its files out of PENDING.

import type { ProseState } from './prosestates'
import { click } from './prosestates.shell'

export const ADMIN: readonly ProseState[] = [
  {
    key: 'admin/people-audit',
    group: 'admin',
    files: ['People.tsx'],
    shows: '.pp-audit.is-open',
    walk: { open: 'admin/people', persona: 'platform-admin', then: () => click('.pp-fold') },
  },
]
