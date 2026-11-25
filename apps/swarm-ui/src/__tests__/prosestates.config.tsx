// The config section's click-only states (issue 439; the harness and the rule
// are in prosestates.tsx). GitHub access: a rule's How fold opened.
// The section's own step registers the rest and takes its files out of PENDING.

import type { ProseState } from './prosestates'
import { click } from './prosestates.shell'

export const CONFIG: readonly ProseState[] = [
  {
    key: 'config/github-how',
    group: 'config',
    files: ['Access.tsx'],
    shows: 'details.ac-rule-how[open]',
    walk: { open: 'config/github', then: () => click('details.ac-rule-how > summary') },
  },
]
